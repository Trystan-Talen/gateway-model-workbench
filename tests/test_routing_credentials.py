"""Saved route connections use a mocked OS store and temporary config only."""
import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import route_test_ui as ui


class RoutingCredentialTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.config_path = Path(self.folder.name) / 'config.json'
        self.keys = {}

        def save(ref, secret):
            self.keys[ref] = secret
            return True

        patches = [
            patch.object(ui, 'TASKS_FILE', str(self.config_path)),
            patch.object(ui, '_load_api_key', side_effect=lambda ref: self.keys.get(ref, '')),
            patch.object(ui, '_save_api_key', side_effect=save),
            patch.object(ui, '_keychain_available', return_value=True),
            patch.object(ui, '_current_user', return_value={'role': 'user'}),
            patch.object(ui.Handler, '_note'),
            patch.object(ui, 'persist_history'),
            patch.object(ui.Handler, '_audit'),
        ]
        self.mocks = [item.start() for item in patches]
        for item in patches:
            self.addCleanup(item.stop)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), ui.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.url = 'http://127.0.0.1:9001/v1'

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        connection.request('POST' if body is not None else 'GET', path,
                           json.dumps(body) if body is not None else None,
                           {'Content-Type': 'application/json', **(headers or {})})
        response = connection.getresponse()
        self.last_headers = dict(response.getheaders())
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def save_side(self, side, key):
        status, data = self.request('/routing-config', {'side': side, 'name': side,
                                                       'url': self.url, 'api_key': key})
        self.assertEqual(status, 200, data)
        self.assertTrue(data['saved'])

    def test_same_address_keeps_distinct_side_keys_and_restores_without_secrets(self):
        self.save_side('baseline', 'synthetic-fixed-secret')
        self.save_side('candidate', 'synthetic-smart-secret')
        ui.save_config({'url': 'http://127.0.0.1:9000/v1', 'model': 'ordinary-model'})
        status, data = self.request('/routing-config')
        self.assertEqual(status, 200)
        for side in ('baseline', 'candidate'):
            self.assertEqual(data['config'][side]['url'], self.url)
            self.assertTrue(data['config'][side]['api_key_saved'])
        self.assertEqual(ui._routing_api_key('baseline', self.url), 'synthetic-fixed-secret')
        self.assertEqual(ui._routing_api_key('candidate', self.url), 'synthetic-smart-secret')
        self.assertEqual(ui.read_saved_config()['model'], 'ordinary-model')
        for content in (json.dumps(data), self.config_path.read_text()):
            self.assertNotIn('synthetic-fixed-secret', content)
            self.assertNotIn('synthetic-smart-secret', content)
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o600)

    def test_creation_and_preflight_accept_both_empty_key_fields(self):
        self.save_side('baseline', 'synthetic-fixed-secret')
        self.save_side('candidate', 'synthetic-smart-secret')
        shared = {'url': self.url, 'api_key': '', 'model': 'shared-model', 'concurrency': 4, 'rpm': 120}
        body = {'tier': 1000, 'rpm': 120, 'baseline': dict(shared), 'candidate': dict(shared)}
        status, estimate = self.request('/preflight', {'kind': 'routing', 'config': body})
        self.assertEqual(status, 200, estimate)
        self.assertFalse(estimate.get('degraded', False), estimate)
        with patch.object(ui.ROUTING_COMPARISONS, 'create', return_value=SimpleNamespace(id='synthetic-run')) as create:
            status, data = self.request('/routing-comparisons', body)
        self.assertEqual(status, 201, data)
        created = create.call_args.args[0]
        self.assertEqual(created['baseline']['api_key'], 'synthetic-fixed-secret')
        self.assertEqual(created['candidate']['api_key'], 'synthetic-smart-secret')
        self.assertNotIn('synthetic-fixed-secret', self.config_path.read_text())

    def test_concurrent_saves_preserve_both_routes_and_ordinary_config(self):
        ui.save_config({'model': 'original-model'})
        threads = [threading.Thread(target=ui.save_routing_connection,
                                    args=(side, {'name': side, 'url': self.url, 'api_key': 'synthetic-' + side}))
                   for side in ('baseline', 'candidate')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        saved = json.loads(self.config_path.read_text())
        self.assertEqual(set(saved['last_routing_config']), {'baseline', 'candidate'})
        self.assertEqual(saved['last_config']['model'], 'original-model')

    def test_save_failure_is_reported_and_address_still_restores(self):
        self.mocks[2].side_effect = None
        self.mocks[2].return_value = False
        self.mocks[3].return_value = False
        status, data = self.request('/routing-config', {'side': 'candidate', 'url': self.url, 'api_key': 'synthetic-key'})
        self.assertEqual(status, 200)
        self.assertFalse(data['saved'])
        self.assertFalse(data['storage_available'])
        restored = self.request('/routing-config')[1]['config']['candidate']
        self.assertEqual(restored['url'], self.url)
        self.assertFalse(restored['api_key_saved'])
        self.assertNotIn('synthetic-key', self.config_path.read_text())

    def test_invalid_connection_does_not_write_a_secret_or_config(self):
        for url in ('https://user:pass@synthetic.invalid/v1', 'https://synthetic.invalid/v1?key=secret'):
            status, _ = self.request('/routing-config', {'side': 'candidate', 'url': url, 'api_key': 'synthetic-key'})
            self.assertEqual(status, 400)
        self.assertEqual(self.keys, {})
        self.assertFalse(self.config_path.exists())

    def test_explicit_reveal_returns_only_the_selected_side_key_without_caching(self):
        self.save_side('baseline', 'synthetic-fixed-secret')
        self.save_side('candidate', 'synthetic-smart-secret')
        for side, key in [('baseline', 'synthetic-fixed-secret'), ('candidate', 'synthetic-smart-secret')]:
            status, data = self.request('/routing-config/reveal', {'side': side, 'url': self.url})
            self.assertEqual(status, 200, data)
            self.assertEqual(data, {'api_key': key})
            self.assertEqual(self.last_headers['Cache-Control'], 'no-store')
            self.mocks[-1].assert_called_with('routing.key_reveal', target=side)
        self.assertNotIn('synthetic-smart-secret', json.dumps(self.request('/routing-config')[1]))
        self.assertNotIn('synthetic-fixed-secret', self.config_path.read_text())

    def test_reveal_rejects_changed_address_invalid_side_and_missing_store_entry(self):
        self.save_side('baseline', 'synthetic-fixed-secret')
        for body in [{'side': 'baseline', 'url': self.url + '/changed'},
                     {'side': 'other', 'url': self.url},
                     {'side': 'candidate', 'url': self.url}]:
            status, data = self.request('/routing-config/reveal', body)
            self.assertEqual(status, 400, data)
            self.assertNotIn('synthetic-fixed-secret', json.dumps(data))
        self.keys.clear()
        status, _ = self.request('/routing-config/reveal', {'side': 'baseline', 'url': self.url})
        self.assertEqual(status, 404)

    def test_reveal_requires_login_and_same_origin(self):
        self.save_side('baseline', 'synthetic-fixed-secret')
        body = {'side': 'baseline', 'url': self.url}
        with patch.object(ui, '_current_user', return_value=None):
            status, data = self.request('/routing-config/reveal', body)
            self.assertEqual(status, 401)
            self.assertNotIn('synthetic-fixed-secret', json.dumps(data))
        status, data = self.request('/routing-config/reveal', body, {'Origin': 'https://other.invalid'})
        self.assertEqual(status, 403)
        self.assertNotIn('synthetic-fixed-secret', json.dumps(data))
        self.mocks[-1].assert_not_called()


if __name__ == '__main__':
    unittest.main()
