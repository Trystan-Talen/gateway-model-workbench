"""逐条路由统计仅使用合成记录、临时文件与本机接口，不接触凭据。"""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import route_test_ui as ui
import routing_compare


def result(index, **extra):
    return {"item_id": f"routing-ab-v1-{index:06d}", "index": index,
            "status_code": 200, "completed": True, "ttft_ms": index + 1,
            "total_ms": index + 2, **extra}


def comparison(history=False, records=0):
    if history:
        return routing_compare.RouteComparison.from_snapshot({
            "id": "syntheticrequests", "name": "合成历史记录", "tier": 1000,
            "status": "finished", "concurrency": {"baseline": 1, "candidate": 1},
            "sides": {side: {"name": side, "model": "synthetic", "url": "http://127.0.0.1:9",
                             "records": records} for side in ("baseline", "candidate")},
            "report": {"historical_value": 17},
        })
    side = {"url": "http://127.0.0.1:9", "model": "synthetic", "concurrency": 1}
    return routing_compare.RouteComparison({"name": "合成路由", "tier": 1000,
                                           "baseline": {**side, "name": "固定路由"},
                                           "candidate": {**side, "name": "智能路由"}})


class RequestPagesTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.data_patch = patch.object(routing_compare, "DATA_DIR", self.folder.name)
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)

    def write_history(self, side, lines):
        path = Path(routing_compare.result_paths("syntheticrequests")[side])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_all_records_are_paginated_in_request_order_while_running(self):
        run = comparison()
        run.status = "running"
        for index in reversed(range(125)):
            run._record("baseline", result(index))
        pages = [run.request_page("baseline", offset, 50) for offset in (0, 50, 100)]
        self.assertEqual([len(page["items"]) for page in pages], [50, 50, 25])
        self.assertEqual([row["request_index"] for page in pages for row in page["items"]], list(range(125)))
        self.assertEqual(pages[0]["total"], 125)
        self.assertTrue(pages[0]["has_more"])
        self.assertFalse(pages[2]["has_more"])
        run._record("baseline", result(125))
        self.assertEqual(run.request_page("baseline", 100)["total"], 126)
        pages[0]["items"][0]["reason"] = "modified"
        self.assertEqual(run.request_page()["items"][0]["reason"], "完整成功响应")

    def test_public_fields_never_echo_body_keys_url_or_exception_text(self):
        sensitive = "https://synthetic.invalid?key=synthetic-private"
        run = comparison()
        run._record("candidate", result(0, status_code=429, completed=False,
                                       error_type=sensitive, error_detail=sensitive, reason=sensitive,
                                       raw_input=sensitive, raw_output=sensitive, url=sensitive,
                                       api_key=sensitive, usage_source=sensitive, usage_level=sensitive))
        page = run.request_page("candidate")
        self.assertNotIn(sensitive, json.dumps(page))
        self.assertEqual(page["items"][0]["error_type"], "HTTP_429")
        self.assertEqual(page["items"][0]["reason"], "请求被限流")
        self.assertEqual(page["items"][0]["sequence"], 1)
        self.assertNotIn("error_detail", page["items"][0])

    def test_history_sorts_legacy_ids_and_caches_only_safe_summaries(self):
        rows = [result(index, raw_input="synthetic-body", error_detail="synthetic-detail")
                for index in reversed(range(125))]
        for row in rows:
            row.pop("index")
        self.write_history("baseline", [json.dumps(row) for row in rows])
        run = comparison(history=True, records=125)
        with patch.object(routing_compare, "open", wraps=open, create=True) as reader:
            first = run.request_page("baseline")
            later = run.request_page("baseline", 100)
            self.assertEqual(reader.call_count, 1)
        self.assertEqual(first["items"][0]["request_index"], 0)
        self.assertEqual(later["items"][-1]["sequence"], 125)
        self.assertNotIn("synthetic-body", repr(run._request_cache))
        self.assertNotIn("synthetic-detail", repr(run._request_cache))
        self.assertEqual(run._report, {"historical_value": 17})

    def test_history_file_changes_invalidate_cached_page(self):
        path = self.write_history("baseline", [json.dumps(result(1))])
        run = comparison(history=True, records=1)
        self.assertEqual(run.request_page()["total"], 1)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(result(0)) + "\n")
        page = run.request_page()
        self.assertEqual(page["total"], 2)
        self.assertEqual([row["request_index"] for row in page["items"]], [0, 1])
        path.unlink()
        missing = run.request_page()
        self.assertFalse(missing["available"])
        self.assertIn("不可读", missing["reason"])

    def test_missing_and_corrupt_history_are_explicit(self):
        run = comparison(history=True, records=3)
        missing = run.request_page()
        self.assertFalse(missing["available"])
        self.assertEqual(missing["recorded_total"], 3)
        self.write_history("baseline", [json.dumps(result(0)), "bad json", "[]"])
        damaged = run.request_page()
        self.assertTrue(damaged["available"])
        self.assertEqual(damaged["total"], 1)
        self.assertEqual(damaged["invalid_lines"], 2)
        self.assertIn("损坏", damaged["reason"])

    def test_invalid_field_types_are_safely_normalized(self):
        row = routing_compare.public_request_record({
            "item_id": "synthetic-private", "status_code": "synthetic-private",
            "completed": "synthetic-private", "error_type": {"private": "synthetic-private"},
            "ttft_ms": float("nan"), "total_ms": -1, "prompt_tokens": True,
            "usage_source": {"private": "synthetic-private"}, "usage_level": ["synthetic-private"],
            "cached_tokens": 10 ** 1000,
        })
        self.assertIsNone(row["request_index"])
        self.assertIsNone(row["ttft_ms"])
        self.assertIsNone(row["cached_tokens"])
        self.assertEqual(row["error_type"], "UNKNOWN")
        self.assertNotIn("synthetic-private", json.dumps(row))

    def test_history_derives_new_statistics_without_changing_original_report(self):
        rows = [result(index, status_code=429, completed=False) if index in (0, 1, 3, 4, 5)
                else result(index) for index in reversed(range(11))]
        self.write_history("baseline", [json.dumps(row) for row in rows])
        run = comparison(history=True, records=11)
        run.total = 11
        snapshot = run.snap()
        self.assertEqual(snapshot["report"], {"historical_value": 17})
        stability = snapshot["derived_report"]["stability"]
        self.assertEqual(stability["baseline"]["max_consecutive_failures"], 3)
        self.assertIsNotNone(stability["baseline"]["ttft_ewma_max_ms"])
        self.assertFalse(stability["baseline"]["partial"])
        self.assertFalse(stability["candidate"]["available"])
        self.assertIsNone(stability["candidate"]["max_consecutive_failures"])
        stability["baseline"]["max_consecutive_failures"] = 99
        self.assertEqual(run.snap()["derived_report"]["stability"]["baseline"]["max_consecutive_failures"], 3)

    def test_new_protocol_history_preserves_original_metrics_without_reading_files(self):
        run = comparison(history=True)
        run._report = {"stability": {"protocol": {"version": "original-protocol"}},
                       "baseline": {"max_consecutive_failures": 9}}
        with patch.object(run, "_request_rows", side_effect=AssertionError("不应重新计算原报告")):
            snapshot = run.snap()
        self.assertIsNone(snapshot["derived_report"])
        self.assertEqual(snapshot["report"]["baseline"]["max_consecutive_failures"], 9)

    def test_ten_thousand_historical_records_remain_reachable_without_rereading(self):
        rows = [json.dumps(result(index)) for index in reversed(range(10000))]
        self.write_history("baseline", rows)
        run = comparison(history=True, records=10000)
        with patch.object(routing_compare, "open", wraps=open, create=True) as reader:
            last = run.request_page("baseline", 9950)
            first = run.request_page("baseline", 0)
            self.assertEqual(reader.call_count, 1)
        self.assertEqual(last["total"], 10000)
        self.assertEqual(last["items"][-1]["sequence"], 10000)
        self.assertEqual(first["items"][0]["sequence"], 1)

    def test_worker_persists_scheduled_index_independent_of_completion_order(self):
        run = comparison()
        cfg = run.config["baseline"]
        cfg.update({"stream": True, "api_key": "synthetic-worker-secret", "connect_timeout": 1,
                    "read_timeout": 1, "write_timeout": 1, "chat_url": "http://127.0.0.1:9"})
        item = {"item_id": "routing-ab-v1-000007", "index": 7, "prompt": "synthetic-prompt"}
        transport = AsyncMock(return_value={"item_id": item["item_id"], "status_code": 200,
                                           "completed": True})

        async def worker():
            queue = asyncio.Queue()
            await queue.put(item)
            await queue.put(None)
            await run._run_side_worker("baseline", cfg, queue, None, asyncio.Semaphore(1))

        run._open_outputs()
        try:
            with patch.object(routing_compare, "do_request", transport):
                asyncio.run(worker())
        finally:
            run._close_outputs()
        stored = json.loads(Path(run.out_paths["baseline"]).read_text())
        self.assertEqual(stored["index"], 7)
        self.assertNotIn("request_index", stored)
        self.assertEqual(transport.call_args.args[1]["index"], 7)


class RequestsApiTests(unittest.TestCase):
    def setUp(self):
        self.run = comparison()
        self.run._record("baseline", result(2))
        with ui.ROUTING_COMPARISONS.lock:
            ui.ROUTING_COMPARISONS.runs[self.run.id] = self.run
        self.addCleanup(lambda: ui.ROUTING_COMPARISONS.runs.pop(self.run.id, None))
        patches = [patch.object(ui, "_current_user", side_effect=lambda handler:
                               {"role": "user"} if handler.headers.get("X-Test-Auth") == "yes" else None),
                   patch.object(ui.Handler, "_note"),
                   patch.object(ui, "_load_api_key", side_effect=AssertionError("不能读取凭据"))]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.handler = ui.Handler.__new__(ui.Handler)
        self.handler._begin = lambda: None
        self.handler._note = lambda *args, **kwargs: None
        self.handler._json = lambda data, status=200, **kwargs: (status, data)

    def request(self, query="", auth=True, run_id=None):
        self.handler.path = f"/routing-comparison/{run_id or self.run.id}/requests{query}"
        self.handler.headers = {"X-Test-Auth": "yes"} if auth else {}
        return self.handler.do_GET()

    def test_authentication_and_not_found(self):
        self.assertEqual(self.request(auth=False)[0], 401)
        self.assertEqual(self.request(run_id="missing")[0], 404)

    def test_api_does_not_build_full_snapshot(self):
        with patch.object(self.run, "snap", side_effect=AssertionError("不能构建全量报告")):
            status, page = self.request("?side=baseline&offset=0&limit=50")
        self.assertEqual(status, 200)
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["sequence"], 3)

    def test_invalid_pagination_is_rejected(self):
        for query in ("?side=invalid", "?offset=-1", "?offset=abc", "?limit=0", "?limit=101",
                      "?limit=50&limit=10", "?key=synthetic-private"):
            with self.subTest(query=query):
                self.assertEqual(self.request(query)[0], 400)


if __name__ == "__main__":
    unittest.main()
