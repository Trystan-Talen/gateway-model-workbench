"""Explicit display projections never replace complete TPM evidence or pricing."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from time_record import request_timing
from tpm_engine import TpmRun
from tpm_records import CompletedRecords, project_request_summary
from tpm_service import FrozenRun, TpmError, TpmManager, get_response
from tpm_usage import normalize_usage


def record(index=0):
    started = 101 + index * .01
    return {'id': 'synthetic-display-' + str(index), 'phase': 'formal', 'stage_index': 1,
            'started_at': started, 'ended_at': started + .005, 'success': True,
            'error': None, 'output_limited': False, 'latency_seconds': .005,
            'ttft_seconds': .002, 'schedule_delay_seconds': .0001,
            'pool_wait_seconds': .00001,
            'signed_schedule_delay_seconds': .0001, 'stage_deadline_margin_seconds': 1,
            'usage': normalize_usage({'prompt_tokens': 10, 'completion_tokens': 0,
                                      'prompt_tokens_details': {'cached_tokens': 0}}),
            'timing': request_timing(scheduled_at=started - .0001, enqueued_at=started,
                                     started_at=started, first_token_at=started + .002,
                                     ended_at=started + .005, total_ms=5,
                                     queue_ms=0, schedule_lag_ms=.1, ttft_ms=2,
                                     source='recorded'),
            'settlement': {'known_cost_usd': .00001, 'reserved_cost_usd': 0}}


def make_run(count=3, hot_limit=100):
    run = TpmRun({'url': 'http://127.0.0.1:1/v1', 'model': 'synthetic-display',
                  'api_key': 'synthetic-offline-key', 'cache_write_price_mode': 'input'})
    run.requests = CompletedRecords(hot_limit=hot_limit)
    for index in range(count):
        run.requests.append(record(index))
    run.created = run.formal_started_at = 100
    run.finished_at = 110
    run.phase, run.status = 'finished', 'finished'
    run._planned = count
    return run


class RequestProjectionTests(unittest.TestCase):
    def test_only_display_fields_selected_without_changing_full_record(self):
        original = record()
        frozen = copy.deepcopy(original)
        projected = project_request_summary(original)
        self.assertEqual(set(projected), {'id', 'phase', 'stage_index', 'success', 'error',
                                           'output_limited', 'timing', 'usage'})
        self.assertEqual(set(projected['usage']), {'full_input_tokens', 'output_tokens',
                                                   'complete', 'completeness'})
        self.assertEqual(projected['timing'], original['timing'])
        self.assertEqual(projected['usage']['output_tokens'], 0)
        self.assertEqual(original, frozen)

    def test_missing_none_and_zero_are_not_conflated(self):
        original = record()
        original.pop('error')
        original['usage'] = {'output_tokens': 0, 'complete': False, 'completeness': None}
        projected = project_request_summary(original)
        self.assertNotIn('error', projected)
        self.assertNotIn('full_input_tokens', projected['usage'])
        self.assertEqual(projected['usage'], original['usage'])
        original['usage']['full_input_tokens'] = None
        self.assertIsNone(project_request_summary(original)['usage']['full_input_tokens'])

    def test_error_output_limit_and_all_recorded_timestamps_preserved(self):
        original = record()
        original.update(success=False, error='stream_error', output_limited=True)
        projected = project_request_summary(original)
        for field in ('success', 'error', 'output_limited', 'timing'):
            self.assertEqual(projected[field], original[field])

    def test_legacy_or_extended_rows_keep_every_field(self):
        for field, value in (('timing', None), ('usage', None), ('attempts', []), ('truncated', True),
                             ('request_id', 'old-id'), ('full_input_tokens', 0), ('output_tokens', None)):
            with self.subTest(field=field):
                original = record()
                original[field] = value
                self.assertIs(project_request_summary(original), original)
        self.assertIsNone(project_request_summary(None))


class DisplaySnapshotTests(unittest.TestCase):
    def test_full_default_unchanged_and_display_has_explicit_marker(self):
        run = make_run()
        full = run.snap()
        display = run.snap(display=True)
        self.assertNotIn('recent_requests_view', full)
        self.assertEqual(display.pop('recent_requests_view'), 'display-v1')
        self.assertEqual(display.pop('recent_requests'),
                         [project_request_summary(row) for row in full['recent_requests']])
        full.pop('recent_requests')
        self.assertEqual(display, full)

    def test_display_is_independent_for_hot_and_cold_records(self):
        run = make_run(5, hot_limit=1)
        original = list(run.requests)
        display = run.snap(display=True)
        for row in display['recent_requests']:
            row['timing']['ended_at'] = None
            row['usage']['full_input_tokens'] = -1
        self.assertEqual(list(run.requests), original)
        self.assertEqual(run.snap()['recent_requests'], original)

    def test_full_export_wins_even_when_display_requested(self):
        run = make_run()
        complete = run.snap(include_requests=True, display=True)
        self.assertNotIn('recent_requests_view', complete)
        self.assertEqual(complete['requests'], list(run.requests))
        self.assertIn('settlement', complete['recent_requests'][0])
        self.assertIn('raw', complete['recent_requests'][0]['usage'])
        self.assertEqual(run.export()['requests'], complete['requests'])

    def test_summary_ignores_display_without_adding_or_traversing_recent_rows(self):
        run = make_run()
        self.assertEqual(run.snap(summary=True, display=True), run.snap(summary=True))
        self.assertNotIn('recent_requests', run.snap(summary=True, display=True))

    def test_frozen_display_is_independent_and_default_still_complete(self):
        frozen = FrozenRun(make_run().snap())
        full = frozen.snap()
        display = frozen.snap(display=True)
        self.assertEqual(display['recent_requests_view'], 'display-v1')
        self.assertEqual(display['recent_requests'][0], project_request_summary(full['recent_requests'][0]))
        display['recent_requests'][0]['timing']['ended_at'] = None
        display['recent_requests'][0]['usage']['full_input_tokens'] = -1
        self.assertEqual(frozen.snap(), full)
        self.assertNotIn('recent_requests_view', frozen.snap(include_requests=True, display=True))
        self.assertNotIn('recent_requests_view', frozen.snap(summary=True, display=True))

    def test_legacy_frozen_display_keeps_full_original_evidence(self):
        original = record()
        original.pop('timing')
        original['attempts'] = [{'timing': {'started_at': '2026-10-16T16:00:00+08:00'}}]
        frozen = FrozenRun({'id': 'f' * 32, 'status': 'finished', 'phase': 'finished',
                            'config': {}, 'recent_requests': [original]})
        full, display = frozen.snap(), frozen.snap(display=True)
        self.assertEqual(display['recent_requests'], full['recent_requests'])
        self.assertIn('settlement', display['recent_requests'][0])

    def test_restored_legacy_time_without_attempts_still_keeps_complete_row(self):
        original = record()
        original.pop('timing')
        frozen = FrozenRun({'id': 'f' * 32, 'status': 'finished', 'phase': 'finished',
                            'config': {}, 'recent_requests': [original]})
        full, display = frozen.snap(), frozen.snap(display=True)
        self.assertEqual(full['recent_requests'][0]['timing']['source'], 'legacy')
        self.assertEqual(display['recent_requests'], full['recent_requests'])
        self.assertIn('raw', display['recent_requests'][0]['usage'])

    def test_manager_routes_display_separately_from_full_history_and_export(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = TpmManager(directory, loop=lambda: None, acquire=lambda *_: None,
                                 release=lambda *_: None, persist=lambda: True, resolve=lambda *_: None)
            run = make_run()
            manager.runs[run.id] = run
            full, status = get_response(manager, '/tpm/runs/' + run.id)
            display, live_status = get_response(manager, '/tpm/runs/' + run.id + '/live')
            self.assertEqual((status, live_status), (200, 200))
            self.assertNotIn('recent_requests_view', full)
            self.assertEqual(display['recent_requests_view'], 'display-v1')
            self.assertIn('settlement', manager.ordered()[0]['recent_requests'][0])
            self.assertNotIn('settlement', manager.ordered(display=True)[0]['recent_requests'][0])
            path = Path(directory) / ('tpm_run_' + run.id + '.jsonl')
            rows = [dict(row, id=run.id + '-' + str(index)) for index, row in enumerate(run.requests)]
            path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
            exported, _ = get_response(manager, '/tpm/runs/' + run.id + '/export')
            self.assertTrue(exported['evidence_complete'])
            self.assertEqual(exported['requests'], rows)
            self.assertIn('settlement', exported['recent_requests'][0])
            self.assertNotIn('recent_requests_view', exported)

    def test_live_path_is_explicit_and_unknown_paths_still_fail(self):
        manager = Mock()
        manager.get.return_value = object()
        manager.snapshot.return_value = {'recent_requests_view': 'display-v1'}
        self.assertEqual(get_response(manager, '/tpm/runs/synthetic/live')[1], 200)
        manager.snapshot.assert_called_once_with(manager.get.return_value, display=True)
        with self.assertRaises(TpmError) as caught:
            get_response(manager, '/tpm/runs/synthetic/unrecognized')
        self.assertEqual(caught.exception.status, 404)


if __name__ == '__main__':
    unittest.main()
