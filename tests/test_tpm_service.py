"""Isolated orchestration/HTTP checks: no paid target or production credentials."""
import asyncio
import copy
import http.client
import json
import tempfile
import threading
import time
from pathlib import Path
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch

import ops
import route_test_ui as ui
from tpm_service import FrozenRun, TpmError, TpmManager
from tpm_config import preview


class SyntheticRun:
    def __init__(self, config, *, task_id, output_path):
        self.id, self.config, self.output_path = task_id, config, output_path
        self.status, self.phase = 'draft', 'awaiting_calibration'
        self.created = time.time()
        self.started_at = self.finished_at = self.stopped_at = None
        self.calibrations = self.executions = 0
        self.calibration = {'p95_seconds': 40, 'mean_input_tokens': 49500, 'mean_output_tokens': 500}
        self.plan = preview(config, self.calibration)

    async def calibrate(self):
        self.calibrations += 1
        self.status, self.phase = 'running', 'awaiting_run'

    async def run(self):
        self.executions += 1
        self.phase = 'running'
        await asyncio.sleep(.06)
        self.phase, self.status = 'finished', 'finished'

    def stop(self, reason='manual_stop'):
        self.phase, self.status = 'finished', 'stopped'

    def snap(self):
        return {'id': self.id, 'name': 'synthetic', 'status': self.status, 'phase': self.phase,
                'created': self.created, 'config': {k: v for k, v in self.config.items() if k != 'api_key'},
                'plan': copy.deepcopy(self.plan), 'fees': {'reserved_usd': 5}}


class TpmServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.limiter = ops.ConcurrencyLimiter(4)
        self.persist = Mock()
        self.manager = TpmManager(self.temp.name, loop=lambda: self.loop,
                                  acquire=self.limiter.acquire_exclusive, release=self.limiter.release,
                                  persist=self.persist, resolve=lambda c: c, run_factory=SyntheticRun)

    def tearDown(self):
        self.manager.close()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(2)
        self.loop.close()
        self.temp.cleanup()

    def body(self):
        return {'calibration_confirmed': True, 'request_id': 'synthetic-1234', 'config': {
            'url': 'http://127.0.0.1:9/v1', 'api_key': 'synthetic-key', 'model': 'mock',
            'cache_write_price_mode': 'input'}}

    def ready(self, run):
        limit = time.monotonic() + 2
        while run.id in self.manager.futures and time.monotonic() < limit:
            time.sleep(.005)
        self.assertEqual(run.phase, 'awaiting_run')

    def test_two_explicit_confirmations_idempotency_and_freeze(self):
        body = self.body()
        with self.assertRaises(TpmError):
            self.manager.create({**body, 'calibration_confirmed': False}, 'user')
        run = self.manager.create(body, 'user')
        self.ready(run)
        self.assertIs(self.manager.create(body, 'user'), run)
        self.assertEqual(run.calibrations, 1)
        self.assertFalse(self.limiter.acquire('other'))
        with self.assertRaises(TpmError):
            self.manager.start(run.id, {'confirmed': True, 'plan_id': 'stale'})
        consent = {'confirmed': True, 'plan_id': self.manager.snapshot(run)['plan_id']}
        self.manager.start(run.id, consent)
        with self.assertRaises(TpmError):
            self.manager.start(run.id, consent)
        time.sleep(.1)
        self.assertEqual(run.executions, 1)
        self.assertIsNone(self.manager.active_id)
        self.assertNotIn('api_key', run.config)
        self.assertTrue(self.limiter.acquire('other'))

    def test_stop_waiting_confirmation_releases_and_no_autoresume(self):
        run = self.manager.create(self.body(), 'user')
        self.ready(run)
        snapshot = self.manager.snapshot(run)
        self.manager.stop(run.id)
        self.assertIsNone(self.manager.active_id)
        self.manager.restore([snapshot])
        frozen = self.manager.get(run.id)
        self.assertIsInstance(frozen, FrozenRun)
        self.assertEqual(frozen.snap()['phase'], 'interrupted')
        self.assertEqual(frozen.snap()['fees']['reserved_usd'], 5)
        with self.assertRaises(TpmError):
            self.manager.start(run.id, {'confirmed': True, 'plan_id': self.manager.snapshot(frozen)['plan_id']})
        self.assertEqual(run.executions, 0)

    def test_recovery_records_detection_without_inventing_end_or_first_token(self):
        run = FrozenRun({'id': 'd' * 32, 'status': 'running', 'created': 1700000000,
            'started_at': 1700000001, 'finished_at': None, 'config': {},
            'recent_requests': [{'id': 'd' * 32 + '-1', 'started_at': 1700000002,
                                 'ended_at': 1700000003, 'latency_seconds': 1.0, 'ttft_seconds': .1}]})
        data = run.snap()
        self.assertIsNone(data['finished_at'])
        self.assertIsNone(data['timing']['ended_at'])
        self.assertIsNone(data['timing']['elapsed_ms'])
        self.assertIsNotNone(data['timing']['interruption_detected_at'])
        row = data['recent_requests'][0]['timing']
        self.assertEqual(row['source'], 'legacy')
        self.assertEqual(row['total_ms'], 1000)
        self.assertEqual(row['ttft_ms'], 100)
        self.assertIsNone(row['first_token_at'])
        self.assertIsNone(row['enqueued_at'])
        self.assertEqual(data['report']['timing'], data['timing'])

    def test_running_history_with_null_report_recovers_without_billing(self):
        run = FrozenRun({'id': 'a' * 32, 'status': 'running', 'phase': 'calibrating',
                         'config': {}, 'report': None, 'fees': {'reserved_cost_usd': 5}})
        snapshot = run.snap()
        self.assertEqual(snapshot['status'], 'error')
        self.assertEqual(snapshot['fees']['reserved_cost_usd'], 5)
        self.assertFalse(snapshot['report']['stable'])
        self.assertTrue(snapshot['report']['unknown_bill_risk'])

    def test_legacy_frozen_config_and_plan_are_not_automatically_replanned(self):
        legacy = {'id': 'c' * 32, 'status': 'finished', 'phase': 'finished',
                  'config': {'concurrency': 17, 'pool_connections': 23},
                  'plan': {'required_concurrency': 1000, 'pool_connections': 23},
                  'report': {'stable': False}}
        original = copy.deepcopy(legacy)
        frozen = FrozenRun(legacy)
        self.assertEqual(frozen.snap()['config'], original['config'])
        self.assertEqual(frozen.snap()['plan'], original['plan'])
        self.assertNotIn('execution_mode', frozen.snap()['config'])

    def test_summary_does_not_traverse_long_evidence_and_keeps_copies_isolated(self):
        class EvidenceMustNotBeCopied:
            def __deepcopy__(self, memo):
                raise AssertionError('list polling copied full evidence')

        run = FrozenRun({'id': 'e' * 32, 'status': 'finished', 'phase': 'finished',
                         'created': 1700000000, 'name': 'historical', 'model': 'mock',
                         'config': {'target_tpm': 200000000, 'cache_mode': 'prefer_cache'},
                         'metrics': {'started': 8000, 'errors': {}},
                         'fees': {'reserved_cost_usd': 2}, 'request_count': 8000,
                         'request_id': 'historical-request', 'actor': 'test'})
        for key in ('series', 'recent_requests', 'requests', 'plan', 'calibration', 'report'):
            run.snapshot[key] = EvidenceMustNotBeCopied()
        self.manager.runs[run.id] = run
        with patch.object(run, 'snap', wraps=run.snap) as snapshot:
            summary = self.manager.ordered(summary=True)[0]
            snapshot.assert_called_once_with(summary=True)
        self.assertEqual(summary['metrics']['started'], 8000)
        self.assertEqual(summary['fees']['reserved_cost_usd'], 2)
        self.assertEqual(summary['timing']['timezone'], 'Asia/Singapore')
        self.assertFalse(set(summary) & {'series', 'recent_requests', 'requests', 'plan',
                                         'calibration', 'report', 'config', 'request_count'})
        summary['metrics']['errors']['modified'] = 1
        summary['fees']['reserved_cost_usd'] = 0
        summary['timing']['timezone'] = 'modified'
        self.assertEqual(run.snapshot['metrics']['errors'], {})
        self.assertEqual(run.snapshot['fees']['reserved_cost_usd'], 2)
        self.assertEqual(run.snapshot['timing']['timezone'], 'Asia/Singapore')
        metadata = self.manager.snapshot(run, summary=True)
        self.assertEqual(metadata['request_count'], 8000)
        self.assertEqual(metadata['request_id'], 'historical-request')
        self.assertEqual(metadata['actor'], 'test')

    def test_summary_preserves_list_fields_order_and_pending_drain_state(self):
        for index, created in enumerate((1700000000, 1700000001)):
            identifier = str(index) * 32
            self.manager.runs[identifier] = FrozenRun({
                'id': identifier, 'name': 'history', 'status': 'finished', 'phase': 'finished',
                'created': created, 'finished_at': created + 1, 'stop_reason': None,
                'metrics': {'started': 3}, 'fees': {'reserved_cost_usd': 0}, 'model': 'mock',
                'config': {'target_tpm': 200000000, 'cache_mode': 'prefer_cache'}})
        pending_id = '1' * 32
        self.manager.futures[pending_id] = None
        with patch('tpm_service.plan_id', side_effect=AssertionError('summary hashed a plan')):
            rows = self.manager.ordered(summary=True)
        self.assertEqual([row['id'] for row in rows], [pending_id, '0' * 32])
        self.assertEqual((rows[0]['phase'], rows[0]['status']), ('draining', 'stopping'))
        self.assertEqual((rows[1]['phase'], rows[1]['status']), ('finished', 'finished'))
        self.assertEqual(set(rows[0]), {
            'id', 'name', 'status', 'phase', 'created', 'finished_at', 'stop_reason',
            'metrics', 'fees', 'resource_exclusive', 'model', 'target_tpm', 'cache_mode', 'timing'})

    def test_export_detects_valid_json_truncation_not_only_damaged_lines(self):
        identifier = 'b' * 32
        self.manager.restore([{'id': identifier, 'status': 'finished', 'phase': 'finished',
                               'config': {}, 'request_count': 2, 'report': {'stable': True}}])
        path = Path(self.temp.name) / f'tpm_run_{identifier}.jsonl'
        path.write_text(json.dumps({'id': identifier + '-1'}) + '\n')
        result = self.manager.export(identifier)
        self.assertFalse(result['evidence_complete'])
        self.assertEqual(result['read_rows'], 1)
        self.assertEqual(result['expected_rows'], 2)
        self.assertEqual(result['damaged_rows'], 0)
        self.assertFalse(result['report']['stable'])

    def test_same_request_id_rejects_different_parameters(self):
        body = self.body()
        run = self.manager.create(body, 'user')
        self.ready(run)
        body['config']['target_tpm'] = 1000000
        with self.assertRaises(TpmError):
            self.manager.create(body, 'user')

    def test_duration_revision_reprices_and_invalidates_old_consent(self):
        run = self.manager.create(self.body(), 'user')
        self.ready(run)
        previous = self.manager.snapshot(run)
        budget = run.config['budget_usd']
        self.manager.revise_plan(run.id, {'target_duration_seconds': 100})
        revised = self.manager.snapshot(run)
        self.assertNotEqual(previous['plan_id'], revised['plan_id'])
        self.assertGreater(revised['plan']['estimated_cost_usd'], previous['plan']['estimated_cost_usd'])
        self.assertGreaterEqual(revised['plan']['request_reserve_usd'], previous['plan']['request_reserve_usd'])
        self.assertEqual(run.config['budget_usd'], budget)
        self.assertEqual(run.config['execution_mode'], 'auto')
        self.assertEqual(revised['plan']['execution_plan']['source'], 'calibration_p95')
        self.assertEqual((run.config['concurrency'], run.config['pool_connections']), (4000, 4000))
        self.assertEqual((revised['plan']['concurrency'], revised['plan']['pool_connections']), (4000, 4000))
        self.assertEqual(run.calibrations, 1)
        with self.assertRaises(TpmError):
            self.manager.start(run.id, {'confirmed': True, 'plan_id': previous['plan_id']})
        with self.assertRaises(TpmError):
            self.manager.revise_plan(run.id, {'target_duration_seconds': 100, 'budget_usd': 1000})

    def test_exclusive_survives_admin_limit_increase(self):
        self.assertTrue(self.limiter.acquire('existing'))
        self.assertFalse(self.limiter.acquire_exclusive('tpm'))
        self.limiter.release('existing')
        self.assertTrue(self.limiter.acquire_exclusive('tpm'))
        self.limiter.set_limit(16)
        self.assertFalse(self.limiter.acquire('new'))
        self.limiter.release('tpm')
        self.assertTrue(self.limiter.acquire('new'))

    def test_initial_history_write_failure_never_starts_billable_calibration(self):
        self.persist.return_value = False
        with self.assertRaises(TpmError) as error:
            self.manager.create(self.body(), 'user')
        self.assertEqual(error.exception.status, 503)
        run = next(iter(self.manager.runs.values()))
        self.assertEqual(run.calibrations, 0)
        self.assertIsNone(self.manager.active_id)
        self.assertEqual(self.limiter.active(), [])


class TpmHttpTests(unittest.TestCase):
    def setUp(self):
        self.manager = Mock(active_id=None)
        self.patches = [patch.object(ui, 'TPM', self.manager),
                        patch.object(ui, '_current_user', lambda h: {'username': 'test'} if h.headers.get('X-Test-Auth') else None),
                        patch.object(ui.Handler, '_audit'), patch.object(ui.Handler, '_note')]
        for item in self.patches:
            item.start()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), ui.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        for item in reversed(self.patches):
            item.stop()

    def request(self, path, body=None, auth=True, origin=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        headers = {'Content-Type': 'application/json'}
        if auth:
            headers['X-Test-Auth'] = 'yes'
        if origin:
            headers['Origin'] = origin
        conn.request('POST' if body is not None else 'GET', path,
                     body=None if body is None else json.dumps(body), headers=headers)
        response = conn.getresponse()
        data = json.loads(response.read())
        conn.close()
        return response.status, data

    def test_preview_is_local_and_routes_require_auth_and_same_origin(self):
        self.assertEqual(self.request('/tpm/defaults', auth=False)[0], 401)
        self.assertEqual(self.request('/tpm/runs/synthetic/live', auth=False)[0], 401)
        self.assertEqual(self.request('/tpm/runs', {}, auth=False)[0], 401)
        self.assertEqual(self.request('/tpm/preview', {}, origin='https://untrusted.invalid')[0], 403)
        code, defaults = self.request('/tpm/defaults')
        self.assertEqual(code, 200)
        self.assertIn('preview', defaults)
        self.assertEqual(defaults['config']['execution_mode'], 'auto')
        self.assertEqual(defaults['config']['concurrency'], defaults['preview']['concurrency'])
        self.assertEqual(defaults['config']['pool_connections'], defaults['preview']['pool_connections'])
        code, preview = self.request('/tpm/preview', {'config': {}})
        self.assertEqual(code, 200)
        self.manager.resolve.assert_not_called()
        self.manager.create.assert_not_called()

    def test_live_view_routes_through_existing_authenticated_handler(self):
        self.manager.snapshot.return_value = {'recent_requests_view': 'display-v1',
                                              'recent_requests': [{'id': 'synthetic-only', 'usage': {'output_tokens': 0}}]}
        code, data = self.request('/tpm/runs/synthetic/live')
        self.assertEqual(code, 200)
        self.assertEqual(data['recent_requests_view'], 'display-v1')
        self.assertEqual(data['recent_requests'][0]['usage']['output_tokens'], 0)
        self.manager.snapshot.assert_called_once_with(self.manager.get.return_value, display=True)

    def test_existing_module_creation_rejected_during_exclusive_run(self):
        self.manager.active_id = 'active-tpm'
        for path in ('/tasks', '/comparisons', '/routing-comparisons', '/quality-tests', '/fingerprints', '/repeats', '/task/old?retry',
                     '/repeats?anything', '/tasks?ignored', '/fingerprints?ignored'):
            self.assertEqual(self.request(path, {})[0], 409, path)
        self.assertEqual(self.request('/tasks', {}, auth=False)[0], 401)


if __name__ == '__main__':
    unittest.main()
