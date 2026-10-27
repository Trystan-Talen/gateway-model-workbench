import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import route_test_ui as ui
from smart_route_load import do_request, fetch_models, fetch_models_detailed


class GatewayHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        model = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))['model']
        if model == 'timeout':
            time.sleep(.25)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        if model == 'protocol':
            self.wfile.write(b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n')
            return
        self.wfile.write(b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n')
        self.wfile.flush()
        time.sleep(.14)
        self.wfile.write(b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n')
        self.wfile.flush()
        self.wfile.write(b'data: [DONE]\n\n')


class CoreTests(unittest.TestCase):
    def setUp(self):
        # 本地模拟服务器不经过机器的系统代理，避免回归请求被转发到外部。
        proxy_patch = mock.patch.dict(os.environ, {'NO_PROXY':'127.0.0.1,localhost', 'no_proxy':'127.0.0.1,localhost'})
        proxy_patch.start()
        self.addCleanup(proxy_patch.stop)
        patcher = mock.patch.object(ui, 'persist_history')
        patcher.start()
        self.addCleanup(patcher.stop)

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), GatewayHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}/v1/chat/completions'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()


    def make_config(self, **overrides):
        raw = {
            'url': self.url,
            'api_key': 'test-secret',
            'model': 'role-first',
            'mode': 'smoke',
            'rpm': 60000,
            'iterations': 8,
            'concurrency': 2,
            'read_timeout': 2,
        }
        raw.update(overrides)
        return ui.validate_config(raw)

    def run_ui_task(self, cfg, stop_after=None):
        steps, total = ui.build_plan(cfg)
        task = ui.Task(cfg, steps, total)
        task.status = 'running'
        task.started_at = time.time()
        fd, path = tempfile.mkstemp(suffix='.jsonl')
        os.close(fd)
        task.out_path = path
        task._out = open(path, 'a', encoding='utf-8', buffering=1)

        async def execute():
            runner = asyncio.create_task(ui.run_task(task))
            if stop_after is not None:
                await asyncio.sleep(stop_after)
                task._stopped.set()
            await runner

        with mock.patch.object(ui.MANAGER, 'finish', side_effect=lambda t: t.close_output()):
            asyncio.run(execute())
        with open(path, encoding='utf-8') as f:
            rows = [json.loads(line) for line in f if line.strip()]
        os.unlink(path)
        return task, rows

    def request(self, model, read=.08):
        async def run():
            async with httpx.AsyncClient() as client:
                return await do_request(client,
                    {'id': 1, 'mode': 'smoke', 'profile': 'smoke', 'step_index': 1, 'grade': None},
                    {'model': model, 'messages': [], 'stream': True}, {},
                    {'connect': 1, 'read': read, 'write': 1, 'pool': 1}, chat_url=self.url)
        return asyncio.run(run())

    def test_fetch_models_strips_full_chat_completion_url(self):
        class Response:
            status_code = 200
            def json(self):
                return {"data": [{"id": "model-a"}]}

        class FakeClient:
            def __init__(self):
                self.urls = []
            async def get(self, url, **kwargs):
                self.urls.append(url)
                return Response()

        async def run():
            client = FakeClient()
            models = await fetch_models(client, "https://example.test/v1/chat/completions", {})
            return models, client.urls

        models, urls = asyncio.run(run())
        self.assertEqual(models, ["model-a"])
        self.assertEqual(urls[0], "https://example.test/v1/models")
        self.assertNotIn("/chat/completions/models", urls)

    def test_fetch_models_detailed_distinguishes_empty_success(self):
        class Response:
            status_code = 200
            def json(self):
                return {"data": []}

        class FakeClient:
            async def get(self, url, **kwargs):
                return Response()

        async def run():
            return await fetch_models_detailed(FakeClient(), "https://example.test/v1", {})

        result = asyncio.run(run())
        self.assertTrue(result["ok"])
        self.assertEqual(result["models"], [])
        self.assertEqual(result["status"], 200)

    def test_fetch_models_detailed_reports_http_failure(self):
        class Response:
            status_code = 401
            def json(self):
                return {"error": "unauthorized"}

        class FakeClient:
            async def get(self, url, **kwargs):
                return Response()

        async def run():
            return await fetch_models_detailed(FakeClient(), "https://example.test/v1", {})

        result = asyncio.run(run())
        self.assertFalse(result["ok"])
        self.assertEqual(result["models"], [])
        self.assertIn("HTTP 401", result["error"])

    def test_phase_never_reports_a_finished_soak_as_running(self):
        cfg = self.make_config(mode='soak', duration=60)
        steps, total = ui.build_plan(cfg)
        task = ui.Task(cfg, steps, total)
        task.status = 'finished_with_errors'
        self.assertNotIn('仍在执行', task._phase())
        self.assertIn('部分请求失败', task._phase())

    def test_timeout_stays_timeout(self):
        result = self.request('timeout')
        self.assertEqual(result['error_type'], 'TIMEOUT')
        self.assertEqual(result['status_code'], 0)

    def test_200_incomplete_is_protocol(self):
        result = self.request('protocol', 1)
        self.assertEqual(result['status_code'], 200)
        self.assertEqual(result['error_type'], 'PROTOCOL')
        self.assertFalse(result['completed'])

    def test_ttft_waits_for_real_content(self):
        result = self.request('role-first', 1)
        self.assertTrue(result['completed'])
        self.assertGreaterEqual(result['ttft_ms'], 100)

    def test_invalid_ramp_rejected_before_queue(self):
        with self.assertRaisesRegex(ValueError, '阶梯 RPM'):
            ui.validate_config({'url':'http://x.test','api_key':'secret','model':'m','mode':'ramp','ramps':'abc'})

    def test_negative_concurrency_rejected(self):
        with self.assertRaisesRegex(ValueError, '并发上限'):
            ui.validate_config({'url':'http://x.test','api_key':'secret','model':'m','mode':'smoke','concurrency':-1})

    def test_new_gateway_defaults_match_form_and_explicit_pressure_is_preserved(self):
        base = {'url': 'http://x.test', 'api_key': 'synthetic-key', 'model': 'synthetic-model'}
        for mode, extra in (('smoke', {}), ('soak', {}), ('ramp', {'ramps': '100,200,400'})):
            with self.subTest(mode=mode):
                cfg = ui.validate_config({**base, 'mode': mode, **extra})
                self.assertEqual((cfg['rpm'], cfg['concurrency']), (100, 20))
        cfg = ui.validate_config({**base, 'rpm': 321, 'concurrency': 7})
        self.assertEqual((cfg['rpm'], cfg['concurrency']), (321, 7))

    def test_cache_profile_is_accepted_by_workbench_config(self):
        cfg = ui.validate_config({
            'url': 'http://x.test', 'api_key': 'synthetic-key', 'model': 'synthetic-model',
            'profile': 'cache',
        })
        self.assertEqual(cfg['profile'], 'cache')

    def test_task_classifies_protocol_separately(self):
        cfg = ui.validate_config({'url':'http://x.test','api_key':'secret','model':'m','mode':'smoke'})
        steps, total = ui.build_plan(cfg)
        task = ui.Task(cfg, steps, total)
        with tempfile.NamedTemporaryFile('w+', delete=False) as f:
            task._out = f
            task.record({'item_id':1,'step_index':1,'status_code':200,'completed':False,'error_type':'PROTOCOL','ttft_ms':None,'total_ms':5,'reason':'bad'})
            self.assertEqual(task.counters['protocol'], 1)
            self.assertEqual(task.counters['r4xx'], 0)
        os.unlink(f.name)

    def test_string_false_is_not_truthy(self):
        cfg = ui.validate_config({'url': 'http://x.test', 'api_key': 'secret', 'model': 'm',
                                  'mode': 'smoke', 'capture_raw': 'false', 'stream': '0'})
        self.assertFalse(cfg['capture_raw'])
        self.assertFalse(cfg['stream'])

    def test_non_finite_values_rejected(self):
        with self.assertRaisesRegex(ValueError, '有限数字'):
            ui.validate_config({'url': 'http://x.test', 'api_key': 'secret', 'model': 'm',
                                'mode': 'smoke', 'rpm': float('nan')})
        with self.assertRaisesRegex(ValueError, '有限数字'):
            ui.validate_config({'url': 'http://x.test', 'api_key': 'secret', 'model': 'm',
                                'mode': 'smoke', 'read_timeout': float('inf')})

    def test_tiny_schedule_rejected_instead_of_sending_hidden_request(self):
        cfg = ui.validate_config({'url': 'http://x.test', 'api_key': 'secret', 'model': 'm',
                                  'mode': 'soak', 'rpm': .01, 'duration': 1})
        with self.assertRaisesRegex(ValueError, '至少为 1'):
            ui.build_plan(cfg)

    def test_normal_run_does_not_clear_pending_queue(self):
        task, rows = self.run_ui_task(self.make_config(iterations=10, concurrency=2))
        self.assertEqual(task.status, 'finished')
        self.assertEqual(task.done, task.total)
        self.assertEqual(task.started, task.total)
        self.assertEqual(task.dropped, 0)
        self.assertEqual(len(rows), task.total)

    def test_stop_keeps_inflight_results_and_drops_only_pending(self):
        task, rows = self.run_ui_task(self.make_config(iterations=100, concurrency=2), stop_after=.05)
        self.assertEqual(task.status, 'stopped')
        self.assertGreater(task.started, 0)
        self.assertEqual(task.done, task.started)
        self.assertEqual(len(rows), task.done)
        self.assertEqual(task.dropped, task.total - task.done)
        frozen = task.snap()['elapsed']
        time.sleep(.05)
        self.assertAlmostEqual(task.snap()['elapsed'], frozen, places=3)

    def test_capture_raw_is_optional(self):
        async def run(capture):
            async with httpx.AsyncClient() as client:
                return await do_request(client,
                    {'id': 9, 'mode': 'smoke', 'profile': 'smoke', 'step_index': 1, 'grade': None},
                    {'model': 'role-first', 'messages': [{'role': 'user', 'content': 'hello'}], 'stream': True}, {},
                    {'connect': 1, 'read': 1, 'write': 1, 'pool': 1}, chat_url=self.url,
                    capture_raw=capture)
        without = asyncio.run(run(False))
        with_raw = asyncio.run(run(True))
        self.assertNotIn('raw_input', without)
        self.assertNotIn('raw_output', without)
        self.assertEqual(with_raw['raw_input']['messages'][0]['content'], 'hello')
        self.assertIn('content', with_raw['raw_output'])

    def test_record_write_failure_does_not_advance_stats(self):
        class BrokenWriter:
            def write(self, _):
                raise OSError('disk full')
            def flush(self):
                pass
        cfg = self.make_config(iterations=1)
        steps, total = ui.build_plan(cfg)
        task = ui.Task(cfg, steps, total)
        task._out = BrokenWriter()
        with self.assertRaises(OSError):
            task.record({'item_id': 1, 'step_index': 1, 'status_code': 200, 'completed': True,
                         'error_type': None, 'ttft_ms': 1, 'total_ms': 2, 'reason': None})
        self.assertEqual(task.done, 0)
        self.assertEqual(task.counters['ok'], 0)
        self.assertIsNotNone(task.write_error)

    def test_concurrent_config_saves_are_atomic(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'tasks.json')
            errors = []
            def save(i):
                try:
                    ui.save_config({'name': f'task-{i}', 'api_key': 'must-not-persist'})
                except Exception as exc:
                    errors.append(exc)
            with mock.patch.object(ui, 'TASKS_FILE', path):
                threads = [threading.Thread(target=save, args=(i,)) for i in range(20)]
                for thread in threads: thread.start()
                for thread in threads: thread.join()
                with open(path, encoding='utf-8') as f:
                    data = json.load(f)
            self.assertFalse(errors)
            self.assertNotIn('api_key', data['last_config'])

    def test_raw_record_endpoint_is_reachable(self):
        cfg = self.make_config(iterations=1, capture_raw=True)
        steps, total = ui.build_plan(cfg)
        task = ui.Task(cfg, steps, total)
        with tempfile.NamedTemporaryFile('w', delete=False, encoding='utf-8') as f:
            json.dump({'item_id': 7, 'raw_input': {'hello': 'world'}, 'raw_output': 'ok'}, f)
            f.write('\n')
            task.out_path = f.name
        with ui.MANAGER.lock:
            ui.MANAGER.tasks[task.id] = task
        server = ThreadingHTTPServer(('127.0.0.1', 0), ui.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}/task/{task.id}/record/7'
            # The endpoint must remain protected even before administrator setup.
            with mock.patch.object(ui, '_auth_setup_required', return_value=True):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(url, timeout=2)
                self.assertEqual(error.exception.code, 401)
            with mock.patch.object(ui, '_current_user', return_value={'username':'fixture','role':'user'}):
                with urllib.request.urlopen(url, timeout=2) as response:
                    data = json.load(response)
            self.assertEqual(data['raw_input']['hello'], 'world')
            self.assertEqual(data['raw_output'], 'ok')
        finally:
            server.shutdown(); server.server_close()
            with ui.MANAGER.lock:
                ui.MANAGER.tasks.pop(task.id, None)
            os.unlink(task.out_path)

    def test_quality_capture_raw_uses_strict_boolean_parser(self):
        base = {
            "name": "quality",
            "resources": [{"name": "A", "url": "http://x.test", "api_key": "secret", "model": "m"}],
        }
        with self.assertRaisesRegex(ValueError, "记录原始数据开关"):
            ui.validate_quality_config({**base, "capture_raw": "invalid"})

    def test_public_config_removes_nested_credential_fields(self):
        public = ui.public_config({
            "url": "http://x.test", "api_key": "secret", "password": "pw",
            "headers": {"Authorization": "Bearer secret", "X-Trace": "keep"},
            "metadata": {"access_token": "token", "safe": "keep"},
        })
        self.assertNotIn("api_key", public)
        self.assertNotIn("password", public)
        self.assertNotIn("headers", public)
        self.assertNotIn("metadata", public)
        normalized = ui.validate_config({"url": "http://x.test", "api_key": "secret", "model": "m", "unknown": "must-drop"})
        self.assertNotIn("unknown", normalized)

    def test_comparison_clears_shared_key_on_execution_exception(self):
        raw = {
            "name": "comparison",
            "shared_config": {"url": "http://x.test", "api_key": "secret", "model": "m"},
            "groups": [
                {"name": "A", "concurrency": 1, "rpm": 60, "iterations": 1},
                {"name": "B", "concurrency": 1, "rpm": 60, "iterations": 1},
            ],
        }
        run = ui.ComparisonRun(ui.validate_comparison_config(raw))
        with mock.patch.object(ui.MANAGER, "add", side_effect=RuntimeError("boom")):
            ui.ComparisonManager()._execute(run)
        self.assertEqual(run.config["shared_config"]["api_key"], "")

    def test_output_file_is_private(self):
        cfg = self.make_config(iterations=1)
        steps, total = ui.build_plan(cfg)
        task = ui.Task(cfg, steps, total)
        task.open_output()
        try:
            self.assertEqual(os.stat(task.out_path).st_mode & 0o777, 0o600)
        finally:
            task.close_output()
            os.unlink(task.out_path)


if __name__ == '__main__':
    unittest.main()
