"""Routing discovery uses each supplied connection without saving its secret."""
import asyncio
import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import httpx
import route_test_ui as ui


class RoutingModelsTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch.object(ui, '_current_user', lambda handler: {'role': 'user'}
                         if handler.headers.get('X-Test-Auth') == 'yes' else None),
            patch.object(ui.Handler, '_note'),
            patch.object(ui, '_load_api_key'),
            patch.object(ui, '_save_api_key'),
            patch.object(ui, 'save_config'),
        ]
        self.mocks = [item.start() for item in self.patches]
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), ui.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.connection = {'url': 'https://synthetic.invalid/v1', 'api_key': 'synthetic-routing-key'}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        try:
            for mock in self.mocks[2:]:
                mock.assert_not_called()
        finally:
            for item in reversed(self.patches):
                item.stop()

    def request(self, body=None, *, authenticated=True, origin=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        headers = {'Content-Type': 'application/json'}
        if authenticated:
            headers['X-Test-Auth'] = 'yes'
        if origin:
            headers['Origin'] = origin
        conn.request('POST', '/routing-models', json.dumps(self.connection if body is None else body), headers)
        response = conn.getresponse()
        status, data = response.status, json.loads(response.read())
        conn.close()
        return status, data

    def test_requires_login_and_same_origin_before_fetching(self):
        with patch.object(ui, 'fetch_models_detailed_sync') as fetch:
            self.assertEqual(self.request(authenticated=False)[0], 401)
            self.assertEqual(self.request(origin='https://foreign.invalid')[0], 403)
            fetch.assert_not_called()

    def test_fetches_with_supplied_key_without_persistence(self):
        for url in ('https://synthetic.invalid', 'https://synthetic.invalid/v1',
                    'https://synthetic.invalid/v1/chat/completions'):
            with self.subTest(url=url):
                requests = []
                def upstream(request):
                    requests.append(request)
                    self.assertEqual(request.headers['Authorization'], 'Bearer synthetic-routing-key')
                    if request.url.path != '/v1/models':
                        return httpx.Response(404)
                    return httpx.Response(200, json={'data': [{'id': 'model-b'}, {'id': 'model-a'}, {'id': 'model-a'}]})
                client = httpx.AsyncClient
                with patch.object(ui.httpx, 'AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(upstream), **kwargs)):
                    status, data = self.request({**self.connection, 'url': url})
                self.assertEqual(status, 200, data)
                self.assertEqual(data, {'ok': True, 'models': ['model-a', 'model-b']})
                self.assertTrue(all(request.method == 'GET' for request in requests))
                self.assertEqual(str(requests[-1].url), 'https://synthetic.invalid/v1/models')

    def test_invalid_connections_do_not_discover_or_load_saved_keys(self):
        invalid = [{}, {'url': self.connection['url']}, {**self.connection, 'api_key': '  '},
                   *({**self.connection, 'url': url} for url in (
                       'file:///tmp/models', 'https://name:password@synthetic.invalid/v1',
                       'https://synthetic.invalid/v1?key=secret', 'https://synthetic.invalid/v1#secret',
                       'https://synthetic.invalid/synthetic-routing-key', 'https://invalid host/v1'))]
        with patch.object(ui, 'fetch_models_detailed_sync') as fetch:
            for body in invalid:
                with self.subTest(body=body):
                    self.assertEqual(self.request(body)[0], 400)
            fetch.assert_not_called()

    def test_upstream_failure_is_readable_and_redacted(self):
        with patch.object(ui, 'fetch_models_detailed_sync', return_value={
            'ok': False, 'models': [], 'error': 'HTTP 401 synthetic-routing-key',
        }):
            status, data = self.request()
        self.assertEqual(status, 502)
        self.assertIn('401', data['error'])
        self.assertNotIn(self.connection['api_key'], json.dumps(data))

    def test_empty_list_does_not_fabricate_models(self):
        with patch.object(ui, 'fetch_models_detailed_sync', return_value={'ok': True, 'models': []}):
            self.assertEqual(self.request(), (200, {'ok': True, 'models': []}))

    def test_timeout_has_a_retryable_message(self):
        with patch.object(ui, 'fetch_models_detailed_sync', side_effect=TimeoutError) as fetch:
            status, data = self.request()
        self.assertEqual(status, 504)
        self.assertIn('超时', data['error'])
        self.assertEqual(fetch.call_args.kwargs['timeout'], 30)

    def test_discovery_deadline_cancels_a_slow_upstream(self):
        cancelled = []
        async def slow(request):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.append(True)
        client = httpx.AsyncClient
        with patch.object(ui.httpx, 'AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(slow), **kwargs)):
            with self.assertRaises(TimeoutError):
                ui.fetch_models_detailed_sync(self.connection['url'], {}, timeout=.01)
        self.assertEqual(cancelled, [True])


if __name__ == '__main__':
    unittest.main()
