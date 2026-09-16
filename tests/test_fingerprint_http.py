"""Actual HTTP routing/auth/round-trip checks with an in-memory synthetic upstream."""
import asyncio
import copy
import http.client
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import httpx
import route_test_ui as ui
from fingerprint_service import FingerprintManager


class FingerprintSharedCapacityTests(unittest.TestCase):
    def test_releasing_fingerprint_capacity_wakes_existing_gateway_queue(self):
        with patch.object(ui, 'LIMITER') as limiter, patch.object(ui, 'MANAGER') as manager, patch.object(ui, 'SERVER_SHUTTING_DOWN', False):
            limiter.acquire.return_value = True
            bridge = ui._FingerprintLimiter()
            self.assertTrue(bridge.acquire('fingerprint-run'))
            bridge.release('fingerprint-run')
            limiter.release.assert_called_once_with('fingerprint-run')
            manager._pump.assert_called_once_with()

    def test_shutdown_does_not_start_pending_gateway_requests(self):
        with patch.object(ui, 'LIMITER') as limiter, patch.object(ui, 'MANAGER') as manager, patch.object(ui, 'SERVER_SHUTTING_DOWN', True):
            ui._FingerprintLimiter().release('fingerprint-run')
            limiter.release.assert_called_once_with('fingerprint-run')
            manager._pump.assert_not_called()


class FingerprintHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = FingerprintManager(self.temp.name)
        async def synthetic(request):
            body = json.loads(request.content)
            prompt = body['messages'][1]['content']
            alternate = body['model'] == 'synthetic-different'
            answer = ('8' if alternate else '7') if any(x in prompt for x in ('number', '数字', '随机数', '个数')) else ('red' if alternate else 'blue') if any(x in prompt for x in ('color', '颜色')) else ('dog' if alternate else 'cat')
            return httpx.Response(200, json={'model': body['model'], 'choices': [{'message': {'content': answer}, 'finish_reason': 'stop'}]})
        self.manager._new_client = lambda protocol: httpx.AsyncClient(transport=httpx.MockTransport(synthetic))
        self.patches = [patch.object(ui, 'FINGERPRINTS', self.manager),
                        patch.object(ui, '_current_user', lambda handler: {'username': 'synthetic-http-user', 'role': 'user'} if handler.headers.get('X-Test-Auth') == 'yes' else None),
                        patch.object(ui.Handler, '_audit', lambda *args, **kwargs: None),
                        patch.object(ui.Handler, '_note', lambda *args, **kwargs: None)]
        for item in self.patches:
            item.start()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), ui.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.manager.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def request(self, path, data=None, *, authenticated=True, method=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        headers = {'Content-Type': 'application/json'}
        if authenticated:
            headers['X-Test-Auth'] = 'yes'
        conn.request(method or ('GET' if data is None else 'POST'), path, body=None if data is None else json.dumps(data), headers=headers)
        response = conn.getresponse()
        raw = response.read()
        result = json.loads(raw) if 'application/json' in response.getheader('Content-Type', '') else raw.decode()
        status, out_headers = response.status, dict(response.getheaders())
        conn.close()
        return status, result, out_headers

    def config(self, **changes):
        return {'mode': 'selfcheck', 'name': '合成接口验收', 'preset': 'quick', 'samples_per_cell': 10,
                'concurrency': 2, 'timeout_seconds': 5, 'adapter': 'standard', 'risk_acknowledged': True,
                'endpoint': {'url': 'https://synthetic.invalid/v1', 'model': 'synthetic-model', 'api_key': 'SYNTHETIC-TEST-KEY'}, **changes}

    def finished(self, identifier):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status, value, _ = self.request('/fingerprints/' + identifier)
            self.assertEqual(status, 200)
            if value['status'] in {'finished', 'error', 'stopped', 'interrupted'}:
                return value
            time.sleep(.02)
        self.fail('synthetic collection did not finish')

    def test_all_fingerprint_data_routes_require_login(self):
        identifier = 'a' * 32
        for path in ('/fingerprint/catalog', '/fingerprints', '/fingerprints/' + identifier,
                     '/fingerprints/' + identifier + '/samples', '/fingerprints/' + identifier + '/export',
                     '/fingerprints/' + identifier + '/samples-export',
                     '/fingerprint/references', '/fingerprint/references/' + identifier + '/export'):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, authenticated=False)[0], 401)
        for path in ('/fingerprints', '/fingerprints/' + identifier + '/stop',
                     '/fingerprint/models',
                     '/fingerprints/' + identifier + '/delete', '/fingerprints/' + identifier + '/compare',
                     '/fingerprint/references/import', '/fingerprint/references/' + identifier + '/delete'):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, {}, authenticated=False)[0], 401)

    def test_public_assets_are_explicit_and_main_page_has_four_modules(self):
        status, html, _ = self.request('/', authenticated=False)
        self.assertEqual(status, 200)
        for module in ('gateway', 'routing', 'quality', 'fingerprint'):
            self.assertIn('data-module="' + module + '"', html)
        for path, mime in (('/assets/fingerprint.js', 'text/javascript'), ('/assets/fingerprint.css', 'text/css')):
            status, body, headers = self.request(path, authenticated=False)
            self.assertEqual(status, 200)
            self.assertGreater(len(body), 100)
            self.assertIn(mime, headers['Content-Type'])
            self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(self.request('/assets/../accounts.json', authenticated=False)[0], 401)

    def test_invalid_body_confirmation_and_missing_objects_return_readable_errors(self):
        self.assertEqual(self.request('/fingerprints', [1])[0], 400)
        self.assertEqual(self.request('/fingerprints', self.config(risk_acknowledged=False))[0], 400)
        self.assertEqual(self.request('/fingerprint/references/import', {'data': []})[0], 400)
        self.assertEqual(self.request('/fingerprints/' + 'a' * 32)[0], 404)
        self.assertEqual(self.request('/fingerprints/' + 'a' * 32 + '/stop', {})[0], 404)
        self.assertEqual(self.request('/fingerprint/unknown')[0], 404)

    def test_model_discovery_is_authenticated_and_does_not_save_keys_or_create_runs(self):
        connection = {'url': 'https://synthetic.invalid/v1', 'api_key': 'SYNTHETIC-DISCOVERY-KEY'}
        with patch.object(ui, 'discover_fingerprint_models', return_value={'models': ['model-a', 'model-b']}) as discover, patch.object(ui, '_save_api_key') as save_key, patch.object(ui, '_load_api_key') as load_key:
            self.assertEqual(self.request('/fingerprint/models', connection, authenticated=False)[0], 401)
            discover.assert_not_called()
            status, result, _ = self.request('/fingerprint/models', connection)
            self.assertEqual(status, 200, result)
            self.assertEqual(result['models'], ['model-a', 'model-b'])
            discover.assert_called_once_with(connection)
            save_key.assert_not_called()
            load_key.assert_not_called()
            self.assertEqual(self.manager.list_runs(), [])
            self.assertNotIn(connection['api_key'], json.dumps(result))

    def test_model_discovery_failures_remain_readable_validation_errors(self):
        with patch.object(ui, 'discover_fingerprint_models', side_effect=ValueError('该接口未提供模型列表，可手动填写模型名称')):
            status, result, _ = self.request('/fingerprint/models', {'url': 'https://synthetic.invalid/v1'})
            self.assertEqual(status, 400)
            self.assertIn('手动填写', result['error'])

    def test_reference_verify_offline_compare_export_import_and_delete_round_trip(self):
        status, body, _ = self.request('/fingerprints', self.config(mode='reference', trusted_source=True, source_note='完全合成的参考数据'))
        self.assertEqual(status, 201, body)
        baseline = self.finished(body['run']['id'])
        self.assertEqual(baseline['status'], 'finished', baseline)
        reference_id = baseline['reference_id']
        self.assertTrue(reference_id)
        status, refs, _ = self.request('/fingerprint/references')
        self.assertEqual(refs['references'][0]['id'], reference_id)
        status, body, _ = self.request('/fingerprints', self.config(mode='verify', reference_id=reference_id))
        self.assertEqual(status, 201, body)
        candidate = self.finished(body['run']['id'])
        self.assertEqual(candidate['comparison']['verdict'], 'match', candidate)
        run_id = candidate['id']
        status, evidence, headers = self.request('/fingerprints/' + run_id + '/export')
        self.assertEqual(status, 200)
        self.assertIn('attachment;', headers['Content-Disposition'])
        self.assertEqual(sum(c['total_count'] for c in evidence['cells'].values()), 40)
        status, samples, _ = self.request('/fingerprints/' + run_id + '/samples?offset=5&limit=7')
        self.assertEqual(status, 200)
        self.assertEqual(len(samples['samples']), 7)
        self.assertEqual(samples['total'], 40)
        status, bundle, headers = self.request('/fingerprints/' + run_id + '/samples-export')
        self.assertEqual(status, 200)
        self.assertEqual(len(bundle['samples']), 40)
        self.assertEqual(bundle['fingerprint']['protocol']['id'], evidence['protocol']['id'])
        self.assertIn('attachment;', headers['Content-Disposition'])
        self.assertEqual(self.request('/fingerprints/' + run_id + '/samples?offset=x')[0], 400)
        status, imported, _ = self.request('/fingerprint/references/import', {'data': evidence, 'name': '导入的合成证据', 'source_note': '合成测试', 'trusted_source': True})
        self.assertEqual(status, 201, imported)
        imported_id = imported['reference']['id']
        status, compared, _ = self.request('/fingerprints/' + run_id + '/compare', {'reference_id': imported_id})
        self.assertEqual(status, 200, compared)
        self.assertEqual(compared['comparison']['verdict'], 'match')
        status, tasks, _ = self.request('/tasks')
        self.assertEqual(status, 200)
        self.assertIn(run_id, [r['id'] for r in tasks['fingerprint_runs']])
        self.assertEqual(self.request('/fingerprints/' + run_id + '/delete', {})[0], 200)
        self.assertEqual(self.request('/fingerprints/' + run_id)[0], 404)
        self.assertEqual(self.request('/fingerprint/references/' + imported_id + '/export')[0], 200)
        self.assertEqual(self.request('/fingerprint/references/' + imported_id + '/delete', {})[0], 200)
        self.assertEqual(self.request('/fingerprint/references/' + imported_id + '/export')[0], 404)
        for path in Path(self.temp.name).rglob('*'):
            if path.is_file():
                self.assertNotIn('SYNTHETIC-TEST-KEY', path.read_text())

    def test_distribution_keys_that_look_like_credentials_survive_export(self):
        _, body, _ = self.request('/fingerprints', self.config())
        run = self.finished(body['run']['id'])
        artifact = copy.deepcopy(run['artifact'])
        cell = artifact['cells']['random-animal:en']
        cell['counts'] = {'token': cell['valid_count']}
        artifact['diagnostics']['returned_models'] = {'token': 40}
        artifact['endpoint']['model'] = 'token'
        artifact['api_key'] = 'not-an-importable-field'
        status, body, _ = self.request('/fingerprint/references/import', {'data': artifact, 'name': '映射键验收', 'source_note': '合成数据', 'trusted_source': True})
        self.assertEqual(status, 201, body)
        identifier = body['reference']['id']
        status, exported, _ = self.request('/fingerprint/references/' + identifier + '/export')
        self.assertEqual(exported['cells']['random-animal:en']['counts']['token'], 10)
        self.assertEqual(exported['diagnostics']['returned_models']['token'], 40)
        self.assertNotIn('api_key', exported)
        self.assertEqual(self.request('/fingerprint/references/import', {'data': exported, 'name': '二次导入', 'trusted_source': False})[0], 201)

    def test_import_size_and_compare_identifier_validation(self):
        _, body, _ = self.request('/fingerprints', self.config())
        run = self.finished(body['run']['id'])
        for malformed in ([], {}, None):
            self.assertEqual(self.request('/fingerprints/' + run['id'] + '/compare', {'reference_id': malformed})[0], 400)
        data = copy.deepcopy(run['artifact'])
        data['extra'] = 'x' * (2 * 1024 * 1024)
        status, body, _ = self.request('/fingerprint/references/import', {'data': data, 'name': '超限合成文件'})
        self.assertEqual(status, 400, body)
        self.assertIn('2', body['error'])

    def test_http_stop_settles_partial_samples(self):
        async def slow(request):
            await asyncio.sleep(.04)
            return httpx.Response(200, json={'choices': [{'message': {'content': '7'}, 'finish_reason': 'stop'}]})
        self.manager._new_client = lambda protocol: httpx.AsyncClient(transport=httpx.MockTransport(slow))
        _, body, _ = self.request('/fingerprints', self.config())
        identifier = body['run']['id']
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.manager.get(identifier)['progress']['done']:
                break
            time.sleep(.01)
        status, body, _ = self.request('/fingerprints/' + identifier + '/stop', {})
        self.assertEqual(status, 200, body)
        run = self.finished(identifier)
        self.assertEqual(run['status'], 'stopped')
        self.assertFalse(run['artifact']['complete'])
        self.assertGreater(run['progress']['done'], 0)
        self.assertLess(run['progress']['done'], 40)
        self.assertEqual(self.request('/fingerprints/' + identifier + '/export')[0], 200)


if __name__ == '__main__':
    unittest.main()
