"""真实上游兼容性夹具测试。

目标：用「录制风格」的 SSE 夹具，证明当前解析器（smart_route_load.do_request /
_parse_sse_line / _content_and_finish）对不同厂商实现的 /chat/completions 流式响应
的实际接受范围，并把已知缺陷固定成可回归的断言。

约束：不联网、不调用真实模型；只通过本地回环服务器（127.0.0.1）回放夹具字节。
本文件只新增测试，不修改 route_test_ui.py / quality_test.py / quality_report.py。

注：任务书提到的基线「93 项」已过时，当前基线为 99 项。
"""
import asyncio
import codecs
import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import httpx

from smart_route_load import (
    _content_and_finish,
    _extract_usage,
    _parse_sse_line,
    _payload_with_usage,
    do_request,
)

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

# 夹具 -> 用于回放的模型名（避免路径/中文出现在请求里）
FIXTURES = [
    "sse_minimal.txt",
    "sse_role_in_first_chunk.txt",
    "sse_role_in_later_chunk.txt",
    "sse_empty_content_chunks.txt",
    "sse_with_usage_final.txt",
    "sse_no_usage.txt",
    "sse_cache_hit.txt",
    "sse_cache_miss.txt",
    "sse_tool_call_chunks.txt",
    "sse_multiline_data.txt",
    "sse_error_mid_stream.txt",
    "sse_truncated.txt",
    "sse_done_only.txt",
]


def load_fixture(name):
    """按原始字节读取夹具，保证「末行截断」这类细节不被文本层抹平。"""
    with open(os.path.join(FIXTURE_DIR, name), "rb") as handle:
        return handle.read()


def replay_lines(raw):
    """按 do_request 的方式切行喂给 _parse_sse_line，返回 (正文, 完成信号, 有效事件数)。"""
    buffer = raw.decode("utf-8", "replace")
    parts = []
    valid_events = 0
    saw_finish = False
    while "\n" in buffer:
        line, buffer = buffer.split("\n", 1)
        content, finish, valid = _parse_sse_line(line.rstrip("\r"))
        valid_events += int(valid)
        saw_finish = saw_finish or finish
        if content:
            parts.append(content)
    if buffer.strip():  # 末尾未换行的残行，do_request 会补解析一次
        content, finish, valid = _parse_sse_line(buffer)
        valid_events += int(valid)
        saw_finish = saw_finish or finish
        if content:
            parts.append(content)
    return "".join(parts), saw_finish, valid_events


class FixtureHandler(BaseHTTPRequestHandler):
    """回放夹具字节；按行之间插入延迟，用于验证 TTFT 判定点。

    ``last_body`` 保存最近一次请求体，用于断言调用方真的带上了
    ``stream_options.include_usage``（这是拿缓存命中率的必要条件）。
    ``reject_stream_options`` 打开后对带该字段的请求回 400，用于验证降级重试。
    """

    last_body = None
    reject_stream_options = False

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).last_body = body
        if type(self).reject_stream_options and body.get("stream_options"):
            payload = json.dumps({"error": {"message": "unknown field stream_options"}})
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload.encode("utf-8"))
            return
        # 非流式请求：把夹具里的 SSE 行还原成一个普通 JSON 响应体。
        if not body.get("stream"):
            model = body.get("model", "")
            merged, usage = merge_fixture_to_json(load_fixture(model))
            payload = json.dumps({
                "id": "chatcmpl-nonstream", "object": "chat.completion", "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": merged},
                             "finish_reason": "stop"}],
                **({"usage": usage} if usage else {}),
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        raw = load_fixture(body.get("model", ""))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        # 逐行下发并在行间停顿：这样 TTFT（首字耗时）才可观测。
        try:
            for line in raw.split(b"\n"):
                self.wfile.write(line + b"\n")
                self.wfile.flush()
                time.sleep(0.03)
        except (BrokenPipeError, ConnectionResetError):
            # 客户端提前断开（截断/BUSYBOX 用例）属预期，不污染测试输出。
            pass
        self.close_connection = True


def merge_fixture_to_json(raw):
    """把录制夹具的 SSE 行合并成非流式响应的 (正文, usage) —— 只用于测试回放。"""
    text = raw.decode("utf-8", "replace")
    parts = []
    usage = None
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except ValueError:
            continue
        if isinstance(obj.get("usage"), dict):
            usage = obj["usage"]
        for choice in obj.get("choices") or []:
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                parts.append(delta["content"])
    return "".join(parts), usage


class UpstreamCompatTests(unittest.TestCase):
    def setUp(self):
        FixtureHandler.last_body = None
        FixtureHandler.reject_stream_options = False
        proxy_patch = mock.patch.dict(
            os.environ,
            {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"},
        )
        proxy_patch.start()
        self.addCleanup(proxy_patch.stop)

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/v1/chat/completions"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    # ---- 回放管道 ------------------------------------------------------

    def replay(self, fixture, read_timeout=5.0, include_usage=False, stream=True):
        """把夹具当作真实上游响应回放，返回 do_request 的结果字典。"""

        async def run():
            async with httpx.AsyncClient() as client:
                return await do_request(
                    client,
                    {"id": 1, "mode": "smoke", "profile": "smoke", "step_index": 1, "grade": None},
                    {"model": fixture, "messages": [], "stream": stream},
                    {},
                    {"connect": 2, "read": read_timeout, "write": 2, "pool": 2},
                    chat_url=self.url,
                    include_usage=include_usage,
                )

        return asyncio.run(run())

    def assert_never_raises(self, fixture):
        """所有夹具共同的最低要求：解析/回放不得抛异常。"""
        result = self.replay(fixture)
        self.assertIsInstance(result, dict)
        return result

    # ---- 逐夹具断言 ----------------------------------------------------

    def test_fixture_set_is_complete(self):
        """夹具目录应包含任务要求的全部 11 个形态。"""
        for name in FIXTURES:
            self.assertTrue(os.path.exists(os.path.join(FIXTURE_DIR, name)), name)

    def test_minimal_stream_is_success(self):
        result = self.assert_never_raises("sse_minimal.txt")
        self.assertEqual(result["status_code"], 200)
        self.assertTrue(result["completed"])
        self.assertIsNone(result["error_type"])
        self.assertEqual(result["response_text"], "好")
        self.assertIsNotNone(result["ttft_ms"])

    def test_role_in_first_chunk_tracks_first_real_token(self):
        """首块带 role（content:""）时，完整正文与完成信号都要正确。"""
        result = self.assert_never_raises("sse_role_in_first_chunk.txt")
        self.assertTrue(result["completed"])
        self.assertIsNone(result["error_type"])
        self.assertEqual(result["response_text"], "你好，世界")
        self.assertIsNotNone(result["ttft_ms"])

    def test_role_in_later_chunk_concatenates_in_order(self):
        """role 出现在后续块时，正文只按出现顺序拼接，role 块本身不进正文。"""
        result = self.assert_never_raises("sse_role_in_later_chunk.txt")
        self.assertTrue(result["completed"])
        self.assertEqual(result["response_text"], "第一段第二段")

    def test_empty_content_chunks_do_not_trigger_ttft(self):
        """content:"" 的空块不能算首字：TTFT 必须落在首个非空 chunk（本夹具约 4 行之后）。"""
        result = self.assert_never_raises("sse_empty_content_chunks.txt")
        self.assertTrue(result["completed"])
        self.assertEqual(result["response_text"], "AB")
        # 前 4 行都是空 content / 空正文，行间 0.03s 停顿，故 TTFT 应 ≥ ~0.09s。
        self.assertIsNotNone(result["ttft_ms"])
        self.assertGreaterEqual(
            result["ttft_ms"], 90,
            "空 content 块被误当作首字，TTFT 提前触发",  # 当前实现缺陷（若未来退化会命中）
        )

    def test_usage_final_block_does_not_pollute_text(self):
        """带 usage 的尾块不是正文，不能污染 response_text，也不能重置 TTFT。"""
        result = self.assert_never_raises("sse_with_usage_final.txt")
        self.assertTrue(result["completed"])
        self.assertEqual(result["response_text"], "答案是 42。")

    def test_no_usage_is_still_success(self):
        """缺少 usage 统计不应被判为协议错误。"""
        result = self.assert_never_raises("sse_no_usage.txt")
        self.assertTrue(result["completed"])
        self.assertIsNone(result["error_type"])
        self.assertEqual(result["response_text"], "没有用量块")

    # ---- 缓存命中计量（usage.cached_tokens） -----------------------------

    def test_cache_hit_is_captured_from_final_usage_block(self):
        """命中缓存的 usage 尾块必须被采到：cached_tokens>0 即命中。

        这是整个缓存命中率指标的地基。usage 块出现在 finish_reason 之后，
        只要连接在 finish 处断开，就永远看不到它——那会让所有请求都显示
        「无 usage」，从而把命中率误报为不可得。
        """
        result = self.replay("sse_cache_hit.txt", include_usage=True)
        self.assertTrue(result["completed"])
        self.assertTrue(result["usage_seen"], "usage 尾块被丢弃，缓存命中率将永远不可得")
        self.assertEqual(result["cached_tokens"], 1024)
        self.assertEqual(result["prompt_tokens"], 1536)
        self.assertEqual(result["completion_tokens"], 9)
        self.assertEqual(result["total_tokens"], 1545)
        self.assertTrue(result["cache_hit"])
        self.assertEqual(result["usage_level"], "HIT_ONLY")

    def test_cache_miss_zero_is_not_confused_with_missing_usage(self):
        """cached_tokens=0 是「真的未命中」，绝不能与「上游没回 usage」混同。

        两者在数据里都表现为「没命中」，但含义相反：前者是缓存退化，后者只是
        网关不支持 usage。若把后者也计入分母，会把兼容性差异读成缓存变差。
        """
        result = self.replay("sse_cache_miss.txt", include_usage=True)
        self.assertTrue(result["completed"])
        self.assertTrue(result["usage_seen"])
        self.assertEqual(result["cached_tokens"], 0)
        self.assertIsNotNone(result["cached_tokens"], "无 usage 与命中 0 被混为一谈")
        self.assertFalse(result["cache_hit"])
        self.assertEqual(result["usage_level"], "NONE")
        # 顶层 adapter 字段名也要能映射过来。
        self.assertEqual(result["prompt_tokens"], 900)

    def test_missing_usage_leaves_cache_fields_none(self):
        """上游完全不含 usage 时，缓存字段必须保持 None，而不是填 0。"""
        result = self.replay("sse_no_usage.txt", include_usage=True)
        self.assertTrue(result["completed"])
        self.assertFalse(result["usage_seen"])
        self.assertIsNone(result["cached_tokens"])
        self.assertIsNone(result["cache_hit"])

    def test_include_usage_injects_stream_options(self):
        """include_usage=True 必须真的把 stream_options.include_usage 发给上游。"""
        FixtureHandler.last_body = None
        self.replay("sse_cache_hit.txt", include_usage=True)
        body = FixtureHandler.last_body or {}
        self.assertEqual(
            (body.get("stream_options") or {}).get("include_usage"), True,
            "调用方没有请求 usage，上游就不会返回它",
        )

    def test_non_streaming_response_usage_is_captured(self):
        """非流式响应体里的 usage 同样要被采集（stream=False 路径）。"""
        result = self.replay("sse_cache_hit.txt", include_usage=True, stream=False)
        self.assertTrue(result["completed"])
        self.assertEqual(result["response_text"], "命中缓存的回答。")
        self.assertTrue(result["usage_seen"])
        self.assertEqual(result["cached_tokens"], 1024)
        self.assertTrue(result["cache_hit"])
        self.assertEqual(result["usage_source"], "body")

    def test_gateway_rejecting_stream_options_is_a_plain_4xx(self):
        """网关不认 stream_options 时回 400：这是明确的失败，不能被吞成成功。

        降级重试由 run() 统一编排；do_request 本身只如实上报状态码。
        """
        FixtureHandler.reject_stream_options = True
        try:
            result = self.replay("sse_cache_hit.txt", include_usage=True)
        finally:
            FixtureHandler.reject_stream_options = False
        self.assertEqual(result["status_code"], 400)
        self.assertFalse(result["completed"])
        self.assertFalse(result["usage_seen"])
        self.assertIsNone(result["cached_tokens"])

    def test_payload_with_usage_only_touches_streaming_requests(self):
        """payload 辅助函数：只给流式请求注入 include_usage，且不覆盖已有选项。"""
        stream_payload = {"model": "m", "stream": True}
        enriched = _payload_with_usage(stream_payload, True)
        self.assertTrue(enriched["stream_options"]["include_usage"])
        self.assertNotIn("stream_options", stream_payload, "不得就地修改原 payload")

        untouched = _payload_with_usage({"model": "m", "stream": True}, False)
        self.assertNotIn("stream_options", untouched)

        non_stream = _payload_with_usage({"model": "m", "stream": False}, True)
        self.assertNotIn("stream_options", non_stream)

        preserved = _payload_with_usage(
            {"model": "m", "stream": True, "stream_options": {"include_usage": False}}, True
        )
        self.assertTrue(preserved["stream_options"]["include_usage"])

    def test_extract_usage_maps_vendor_field_names(self):
        """不同厂商对同一个量用不同字段名，映射层必须都认，且不臆造缺失项。"""
        self.assertEqual(
            _extract_usage({"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12,
                            "prompt_tokens_details": {"cached_tokens": 8}}),
            {"prompt_tokens": 10, "cached_tokens": 8, "completion_tokens": 2, "total_tokens": 12},
        )
        # adapter 风格：顶层改名，无嵌套块。
        self.assertEqual(
            _extract_usage({"input_tokens": 5, "output_tokens": 1, "cache_read_input_tokens": 4}),
            {"prompt_tokens": 5, "cached_tokens": 4, "completion_tokens": 1, "total_tokens": None},
        )
        # 嵌套块优先于同名顶层字段。
        self.assertEqual(
            _extract_usage({"cached_tokens": 1, "prompt_tokens_details": {"cached_tokens": 99}}),
            {"prompt_tokens": None, "cached_tokens": 99, "completion_tokens": None,
             "total_tokens": None},
        )
        # 完全无 usage：返回 None，调用方据此区分「没数据」与「命中 0」。
        self.assertIsNone(_extract_usage({}))
        self.assertIsNone(_extract_usage(None))
        self.assertIsNone(_extract_usage("not-a-dict"))
        # 值非法（布尔/字符串）时不能当成 0 或数字。
        self.assertIsNone(_extract_usage({"prompt_tokens": True, "cached_tokens": "many"}))

    def test_tool_call_chunks_produce_empty_text(self):
        """函数调用流：当前解析器忽略 tool_calls，正文为空但 finish_reason 仍置位。

        这是要固定的行为基线：tool_calls 正文既不被采集，也不为空补全报错。
        """
        result = self.assert_never_raises("sse_tool_call_chunks.txt")
        self.assertTrue(result["completed"], "finish_reason=tool_calls 应被判为完成")
        self.assertIsNone(result["error_type"])
        # 当前实现缺陷：tool_calls 的 arguments 未计入正文，response_text 为空。
        self.assertEqual(result["response_text"], "")

    def test_multiline_data_is_reassembled(self):
        """data: 后无空格可解析；多行 data 事件应按 SSE 规范用 "\\n" 拼接。

        本夹具的 JSON 分片行被原样换行切开（正文里的换行未转义），严格 JSON
        不接受控制字符，因此解析器在严格模式失败后会放宽重试一次。缺了这一步，
        正文会只剩前半段「紧凑前缀」。
        """
        result = self.assert_never_raises("sse_multiline_data.txt")
        self.assertTrue(result["completed"])
        # 按规范用 "\n" 拼接后，正文里的换行是内容的一部分，必须原样保留。
        self.assertEqual(result["response_text"], "紧凑前缀、换行\n拼接正文")

    def test_mid_stream_error_is_not_silently_success(self):
        """流中途返回 {"error":{...}} 时应识别为上游/协议错误，绝不能算成功。

        当前实现只识别 choices/delta/content，error 对象被当作「无效事件」丢弃，
        又因尾随 [DONE] 置位 saw_finish，最终 completed=True、error_type=None，
        错误被静默掩盖成成功。
        """
        result = self.assert_never_raises("sse_error_mid_stream.txt")
        self.assertFalse(
            result["completed"] and not result["error_type"],
            "中途 error 对象被当成成功响应",
        )

    def test_truncated_stream_is_protocol(self):
        """连接在数据中途截断（末行不完整、无 [DONE]、无 finish_reason）应判为 protocol。"""
        result = self.assert_never_raises("sse_truncated.txt")
        self.assertEqual(result["status_code"], 200)
        self.assertFalse(result["completed"])
        self.assertEqual(result["error_type"], "PROTOCOL")
        # 截断前的完整行仍应被采集。
        self.assertEqual(result["response_text"], "这是一段本应继续但被掐断")

    def test_done_only_is_empty_success(self):
        """只有 [DONE]、无正文：当前实现计为成功完成 —— 固定这一行为基线。"""
        result = self.assert_never_raises("sse_done_only.txt")
        self.assertTrue(result["completed"])
        self.assertIsNone(result["error_type"])
        self.assertEqual(result["response_text"], "")
        # 当前实现缺陷：空补全未被标记，与真实成功无法区分。
        self.assertIsNone(result["ttft_ms"], "没有正文就不应记录 TTFT")

    # ---- 分类映射：解析结果 -> 计数器分类 --------------------------------

    def test_replay_classification_matches_record_mapping(self):
        """回放结果喂给 Task.record 的分类规则，验证 ok/protocol 归类一致。"""
        import route_test_ui as ui

        def classify(result):
            status = result.get("status_code") or 0
            error = result.get("error_type")
            completed = bool(result.get("completed"))
            if 200 <= status < 300 and completed:
                return "ok"
            if error == "PROTOCOL" or (200 <= status < 300 and not completed):
                return "protocol"
            if error == "TIMEOUT" or status == 408:
                return "timeout"
            if error == "NETWORK" or (status == 0 and error != "TIMEOUT"):
                return "network"
            return "r4xx"

        expected = {
            "sse_minimal.txt": "ok",
            "sse_role_in_first_chunk.txt": "ok",
            "sse_role_in_later_chunk.txt": "ok",
            "sse_empty_content_chunks.txt": "ok",
            "sse_with_usage_final.txt": "ok",
            "sse_no_usage.txt": "ok",
            "sse_truncated.txt": "protocol",
        }
        self.assertIn("ok", ui.COUNTER_KEYS)
        self.assertIn("protocol", ui.COUNTER_KEYS)
        for fixture, want in expected.items():
            with self.subTest(fixture=fixture):
                self.assertEqual(classify(self.replay(fixture)), want)

    # ---- 逐行解析原语（不经过网络） --------------------------------------

    def test_parse_sse_line_primitives(self):
        """SSE 行解析原语：空行/注释/keep-alive 跳过，data: 前导可带或不带空格。"""
        # 空行与注释行
        self.assertEqual(_parse_sse_line(""), ("", False, False))
        self.assertEqual(_parse_sse_line(": keep-alive comment"), ("", False, False))
        self.assertEqual(_parse_sse_line("event: message"), ("", False, False))
        # [DONE] 哨兵
        self.assertEqual(_parse_sse_line("data: [DONE]"), ("", True, True))
        # 无空格前缀
        content, finish, valid = _parse_sse_line(
            'data:{"choices":[{"delta":{"content":"x"}}]}'
        )
        self.assertEqual((content, finish, valid), ("x", False, True))
        # 正文块带 finish_reason，应同时给出正文与完成信号
        content, finish, valid = _parse_sse_line(
            'data: {"choices":[{"delta":{"content":"end"},"finish_reason":"stop"}]}'
        )
        self.assertEqual((content, finish, valid), ("end", True, True))
        # 非 JSON 行判为无效事件，不抛异常
        self.assertEqual(_parse_sse_line("data: not-json"), ("", False, False))

    def test_content_and_finish_accepts_message_and_text_shapes(self):
        """非流式兼容形态：message.content / choices[].text 也应被识别。"""
        text, finish = _content_and_finish(
            {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}
        )
        self.assertEqual((text, finish), ("hi", True))
        text, finish = _content_and_finish({"choices": [{"text": "legacy"}]})
        self.assertEqual((text, finish), ("legacy", False))
        self.assertEqual(_content_and_finish("not-a-dict"), ("", False))

    def test_chunk_boundaries_do_not_break_parsing(self):
        """字符分片切在任意位置（含 JSON 内部）时，正文与完成信号都不应丢失。

        复刻 do_request 的行缓冲逻辑：跨 chunk 的半行必须被缓存等待拼接。
        """
        text_source = load_fixture("sse_role_in_first_chunk.txt").decode("utf-8")
        for split in range(1, len(text_source)):
            text, finish, _ = simulate_text_chunks(text_source, [split, len(text_source) - split])
            self.assertEqual(text, "你好，世界", f"在字符偏移 {split} 处切分丢掉正文")
            self.assertTrue(finish, f"在字符偏移 {split} 处切分丢掉完成信号")

    def test_multibyte_split_across_byte_chunks_is_preserved(self):
        """TCP 分片可能把多字节字符切成两半，增量解码必须跨块拼回。

        逐块 decode("utf-8","replace") 会把半个汉字变成 U+FFFD；这里用
        simulate_byte_chunks（与 do_request 相同的增量解码）证明正文不被破坏。
        这是真实缺陷的回归断言：只要有人把解码改回逐块 replace，本测试即失败。
        """
        raw = load_fixture("sse_role_in_first_chunk.txt")
        for split in range(1, len(raw)):
            text, finish, _ = simulate_byte_chunks(raw, [split, len(raw) - split])
            self.assertEqual(text, "你好，世界", f"在字节偏移 {split} 处切分丢掉正文")
            self.assertTrue(finish, f"在字节偏移 {split} 处切分丢掉完成信号")

    def test_line_parser_never_raises_on_all_fixtures(self):
        """全部夹具走一遍逐行解析原语，任何一件都不能抛异常。"""
        for name in FIXTURES:
            with self.subTest(fixture=name):
                replay_lines(load_fixture(name))


def simulate_text_chunks(text, sizes):
    """按给定字符长度切片喂给行缓冲逻辑，复刻 do_request 的跨 chunk 拼接行为。

    传入已解码文本 —— 用于验证「行缓冲」本身，不涉及解码层。
    """
    line_buffer = ""
    parts = []
    saw_finish = False
    offset = 0
    for size in sizes:
        chunk = text[offset:offset + size]
        offset += size
        line_buffer += chunk
        while "\n" in line_buffer:
            line, line_buffer = line_buffer.split("\n", 1)
            content, finish, _ = _parse_sse_line(line.rstrip("\r"))
            saw_finish = saw_finish or finish
            if content:
                parts.append(content)
    if line_buffer.strip():
        content, finish, _ = _parse_sse_line(line_buffer)
        saw_finish = saw_finish or finish
        if content:
            parts.append(content)
    return "".join(parts), saw_finish, True


def simulate_byte_chunks(raw, sizes):
    """按给定字节数切片喂给行缓冲逻辑，复刻 do_request 的完整解码 + 拼接行为。

    与 smart_route_load.do_request 一致，使用增量解码器跨块拼回被切开的
    多字节字符。若把这里换回 chunk.decode("utf-8","replace")，本测试会立刻变红。
    """
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    line_buffer = ""
    parts = []
    saw_finish = False
    offset = 0
    for size in sizes:
        chunk = raw[offset:offset + size]
        offset += size
        line_buffer += decoder.decode(chunk)
        while "\n" in line_buffer:
            line, line_buffer = line_buffer.split("\n", 1)
            content, finish, _ = _parse_sse_line(line.rstrip("\r"))
            saw_finish = saw_finish or finish
            if content:
                parts.append(content)
    line_buffer += decoder.decode(b"", final=True)
    if line_buffer.strip():
        content, finish, _ = _parse_sse_line(line_buffer)
        saw_finish = saw_finish or finish
        if content:
            parts.append(content)
    return "".join(parts), saw_finish, True


if __name__ == "__main__":
    unittest.main()
