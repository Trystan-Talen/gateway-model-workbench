"""Loopback-only synthetic model discovery and streamed long-input fixture."""
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

lock = threading.Lock()
slow_models = threading.Event()
observed = {"requests": 0, "min_body_bytes": None, "max_body_bytes": 0,
            "stream_delay_seconds": 1.5, "usage_is_synthetic": True,
            "model_requests": [], "flaky_models_ready": False}


def handle_get(handler):
    if handler.path == "/tpm-stats":
        with lock:
            handler.reply(dict(observed))
        return True
    if handler.path.endswith("/models"):
        with lock:
            observed["model_requests"].append(handler.path)
            ready = observed["flaky_models_ready"]
        if handler.path.startswith("/slow/"):
            slow_models.wait(timeout=8)
            models = ["tpm-stale-model"]
        elif handler.path.startswith("/flaky/"):
            if not ready:
                handler.reply({"error": "synthetic model discovery failure"}, 503)
                return True
            models = ["tpm-retry-model", "tpm-mock"]
        elif handler.path.startswith("/new/"):
            models = ["tpm-new-model", "tpm-mock"]
        else:
            models = ["tpm-mock", "tpm-fail"]
        handler.reply({"data": [{"id": model} for model in models]})
        return True
    return False


def handle_post(handler, body, body_length):
    if handler.path == "/tpm-control":
        with lock:
            if "flaky_models_ready" in body:
                observed["flaky_models_ready"] = bool(body["flaky_models_ready"])
        if body.get("release_slow_models"):
            slow_models.set()
        handler.reply({"ok": True})
        return True
    if body.get("model") not in ("tpm-mock", "tpm-fail"):
        return False
    with lock:
        observed["requests"] += 1
        observed["min_body_bytes"] = min(observed["min_body_bytes"] or body_length, body_length)
        observed["max_body_bytes"] = max(observed["max_body_bytes"], body_length)
    if body["model"] == "tpm-fail":
        handler.reply({"error": "synthetic upstream unavailable"}, 503)
        return True
    # This intentionally counts synthetic word units, not a real model's tokens.
    prompt_tokens = len(re.findall(r"[A-Za-z]+", "\n".join(message["content"] for message in body["messages"]))) + 64
    blocks = [
        {"choices": [{"delta": {"content": "合成测试响应"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 500,
                                  "total_tokens": prompt_tokens + 500,
                                  "prompt_tokens_details": {"cached_tokens": int(prompt_tokens * .95)}}},
    ]
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Connection", "close")
    handler.end_headers()
    try:
        handler.wfile.write(("data: " + json.dumps(blocks[0], ensure_ascii=False) + "\n\n").encode())
        handler.wfile.flush()
        time.sleep(observed["stream_delay_seconds"])
        for block in blocks[1:]:
            handler.wfile.write(("data: " + json.dumps(block, ensure_ascii=False) + "\n\n").encode())
        handler.wfile.write(b"data: [DONE]\n\n")
        handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        pass
    return True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if not handle_get(self):
            self.reply({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        if not handle_post(self, body, length):
            self.reply({"error": "synthetic fixture rejects unknown model"}, 400)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    print(json.dumps({"upstream": f"http://127.0.0.1:{server.server_port}"}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
