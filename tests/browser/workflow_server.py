"""Isolated real HTTP backend + deterministic loopback upstream for browser regression."""
import json
import sys
import threading
import time
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ['NO_PROXY'] = os.environ['no_proxy'] = '127.0.0.1,localhost'

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
folder = Path(sys.argv[1])
folder.mkdir(parents=True, exist_ok=True)
os.environ["ROUTE_TEST_DATA_DIR"] = str(folder)
os.environ["ROUTE_TEST_DISABLE_CREDENTIAL_STORE"] = "1"
import route_test_ui as ui

folder = Path(sys.argv[1])
folder.mkdir(parents=True, exist_ok=True)
ui.BASE_DIR = str(folder)
ui.HISTORY_FILE = str(folder / 'index.json')
ui.TASKS_FILE = str(folder / 'tasks.json')
ui.ACCOUNTS_FILE = str(folder / 'accounts.json')
ui._save_api_key = lambda *args: False
ui._load_api_key = lambda *args: ''
permits = threading.Semaphore(0)
answers = ["PONG", "893", '{"name":"Ada","age":36,"tags":["math","ai"]}',
           "我不能帮助绕过安全控制，也不会提供具体步骤。",
           "项目摘要说明：项目代号：青松，本次批次：17。方案经过评审，支持灰度发布和可回滚，"
           "并且项目代号：青松、批次：17会在验收记录中再次确认。整体流程包含监控、复盘和风险控制。"]
smoke = Path(__file__).resolve().parents[1] / 'fixtures' / 'quality_smoke_v1.json'
by_prompt = dict(zip([case.prompt for case in ui.load_suite(smoke)], answers))
full_cases = {case.prompt:case for case in ui.load_suite()}


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/v1/models':
            return self.reply({'data': [{'id': model} for model in ('quality-oracle','quality-regressed','quality-slow')]})
        return self.reply({'error':'not found'},404)

    def do_POST(self):
        if self.path == '/advance':
            permits.release()
            return self.reply({'ok': True})
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
        if body['model'] == 'quality-fail':
            return self.reply({'error': 'synthetic upstream unavailable'}, 503)
        if body['model'] == 'quality-pass':
            if not permits.acquire(timeout=60):
                return self.reply({'error': 'regression control timeout'}, 504)
        prompt = body['messages'][0]['content']
        answer = by_prompt.get(prompt, 'synthetic comparison response')
        if body['model'] in ('quality-oracle', 'quality-regressed', 'quality-slow'):
            case = full_cases[prompt]
            time.sleep(.008 if body['model'] != 'quality-slow' else .15)
            answer = case.reference_answer
            if body['model'] == 'quality-regressed' and (case.category == 'data_analysis' or case.id == 'safety_boundary_01'):
                answer = case.negative_examples[0]
        self.reply({'choices': [{'message': {'role': 'assistant', 'content': answer}, 'finish_reason': 'stop'}]})


upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
threading.Thread(target=upstream.serve_forever, daemon=True).start()
ui.LOOP = ui.asyncio.new_event_loop()
threading.Thread(target=ui.LOOP.run_forever, daemon=True).start()
ui.restore_history()
server = ThreadingHTTPServer(('127.0.0.1', 0), ui.Handler)
print(json.dumps({'base': f'http://127.0.0.1:{server.server_port}',
                  'upstream': f'http://127.0.0.1:{upstream.server_port}', 'suite': str(smoke)}), flush=True)
try:
    server.serve_forever()
finally:
    ui.FINGERPRINTS.close()
    server.server_close()
    upstream.shutdown()
    upstream.server_close()
    ui.LOOP.call_soon_threadsafe(ui.LOOP.stop)
