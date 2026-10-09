"""摘要按需生成与压测分位数口径的回归，全部使用合成内存数据。"""
from collections import deque
import io
import unittest
from unittest.mock import patch

import route_test_ui as ui


class TaskSnapshotTests(unittest.TestCase):
    def make_task(self):
        config = ui.validate_config({
            "url": "https://synthetic.invalid/v1", "api_key": "synthetic-key",
            "model": "synthetic-model", "mode": "smoke", "iterations": 10,
        })
        steps, total = ui.build_plan(config)
        return ui.Task(config, steps, total)

    def test_summary_matches_detail_projection_for_all_lifecycle_states(self):
        task = self.make_task()
        task.done = 3
        task.ttft.extend([90, 10, 50])
        task.total_ms.extend([900, 100, 500])
        task.schedule_lag_ms.extend([9, 1, 5])
        for status in ui.TASK_STATES:
            with self.subTest(status=status):
                task.status = status
                self.assertEqual(task.snap(summary=True), ui.list_snapshot(task.snap()))

    def test_summary_does_not_sort_samples_or_query_artifact(self):
        task = self.make_task()
        task.ttft.extend(range(10000))
        manager = ui.TaskManager()
        manager.tasks[task.id] = task
        with patch.object(ui, "_latency_percentiles", side_effect=AssertionError("多余统计")), \
                patch.object(task, "artifact_snapshot", side_effect=AssertionError("多余文件访问")):
            summaries = manager.ordered(summary=True)
        self.assertEqual(summaries[0]["total"], 10)
        self.assertTrue(summaries[0]["_summary"])
        self.assertNotIn("ttft", summaries[0])

    def test_history_summary_uses_stored_facts(self):
        task = self.make_task()
        task.history_only = True
        task._history_snapshot = {"id": task.id, "name": "历史", "status": "finished",
                                  "progress": 1, "done": 99, "total": 99,
                                  "ttft": {"p50": 123}, "timeline": [{"rpm": 5}]}
        self.assertEqual(task.snap(summary=True), ui.list_snapshot(task.snap()))
        self.assertEqual(task.snap(summary=True)["total"], 99)

    def test_summary_endpoint_includes_selected_full_detail(self):
        selected, other = self.make_task(), self.make_task()
        manager = ui.TaskManager()
        manager.tasks = {selected.id: selected, other.id: other}
        handler = ui.Handler.__new__(ui.Handler)
        handler.path = f"/tasks?summary=1&kind=tasks&selected={selected.id}"
        handler._json = lambda data, *args, **kwargs: data
        with patch.object(ui, "MANAGER", manager), \
                patch.object(ui, "COMPARISONS", ui.ComparisonManager()), \
                patch.object(ui, "QUALITY", ui.QualityManager()), \
                patch.object(ui.ROUTING_COMPARISONS, "ordered", return_value=[]), \
                patch.object(ui.FINGERPRINTS, "list_runs", return_value=[]), \
                patch.object(ui, "_current_user", return_value={"username": "synthetic"}):
            data = handler._do_get()
        by_id = {item["id"]: item for item in data["tasks"]}
        self.assertIn("ttft", by_id[selected.id])
        self.assertNotIn("_summary", by_id[selected.id])
        self.assertEqual(by_id[other.id], other.snap(summary=True))

    def test_percentile_pair_preserves_floor_rank_and_input(self):
        for samples in ([], [7], [30, 10, 20], [4, 2, 2, 8, 6], list(range(25, 0, -1))):
            with self.subTest(samples=samples):
                values = deque(samples)
                ordered = sorted(samples)
                expected = {
                    key: ordered[max(0, min(len(ordered) - 1, int(ratio * len(ordered)) - 1))]
                    if ordered else None
                    for key, ratio in (("p50", .5), ("p95", .95))
                }
                with patch.object(ui, "sorted", wraps=sorted, create=True) as sorting:
                    self.assertEqual(ui._latency_percentiles(values), expected)
                self.assertEqual(sorting.call_count, 1)
                self.assertEqual(list(values), samples)

    def test_record_and_history_share_error_classification_priority(self):
        examples = (
            ({"status_code": 200, "completed": True}, "ok", "成功"),
            ({"status_code": 200, "completed": False}, "protocol", "响应不完整"),
            ({"status_code": 408}, "timeout", "超时"),
            ({"status_code": 429}, "r429", "限流"),
            ({"status_code": 503}, "r5xx", "服务端错误"),
            ({"status_code": 401}, "r4xx", "客户端错误"),
            ({"status_code": 0}, "network", "网络错误"),
            ({"status_code": 503, "error_type": "TIMEOUT"}, "timeout", "超时"),
            ({"status_code": 429, "error_type": "PROTOCOL"}, "protocol", "响应不完整"),
        )
        for result, category, label in examples:
            with self.subTest(result=result):
                task = self.make_task()
                task._out = io.StringIO()
                self.addCleanup(task._out.close)
                task.record(result)
                self.assertEqual(ui._classify_result(result), (category, label, category))
                self.assertEqual(task.counters[category], 1)
                self.assertEqual(task.recent[-1]["label"], label)


if __name__ == "__main__":
    unittest.main()
