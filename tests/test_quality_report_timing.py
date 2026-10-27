"""可读质量报告保留任务与逐题时间；不编造旧证据，也不回显无效时间字符串。"""
import unittest

from quality_report import render_quality_markdown
from time_record import request_timing, task_timing


class QualityReportTimingTests(unittest.TestCase):
    def test_report_includes_singapore_task_and_each_request_in_execution_order(self):
        snapshot = {
            "name": "合成质量评测", "id": "synthetic", "config": {},
            "timing": task_timing(created_at=0, started_at=1, stop_requested_at=2,
                                  dispatch_ended_at=3, ended_at=4, interruption_detected_at=5,
                                  queue_ms=10, elapsed_ms=250, drain_ms=20),
            "resources": [
                {"name": "资源甲", "case_results": [{"case_id": "case-a", "execution_sequence": 2,
                    "timing": request_timing(started_at="2026-10-15T16:00:00.125+08:00",
                                             ended_at="2026-10-15T16:00:01.250+08:00", total_ms=1125)}]},
                {"name": "资源乙", "case_results": [{"case_id": "case-b", "execution_sequence": 1,
                    "timing": request_timing(started_at=10, ended_at=11, total_ms=1000)}]},
            ],
            "report": {"title": "原评分结论", "summary": "原有事实", "limitations": ["原限制"]},
        }
        exported = render_quality_markdown(snapshot)
        self.assertIn("原评分结论", exported)
        self.assertIn("原有事实", exported)
        self.assertIn("原限制", exported)
        self.assertIn("新加坡时间 UTC+08:00", exported)
        self.assertIn("| 创建 | 1970-01-01T08:00:00.000+08:00 |", exported)
        self.assertIn("| 请求停止 | 1970-01-01T08:00:02.000+08:00 |", exported)
        self.assertIn("| 发现意外中断 | 1970-01-01T08:00:05.000+08:00 |", exported)
        self.assertIn("| 运行耗时（毫秒） | 250 |", exported)
        self.assertIn("| 收尾耗时（毫秒） | 20 |", exported)
        self.assertIn("| 资源甲 | case-a | 2 | 2026-10-15T16:00:00.125+08:00 | 2026-10-15T16:00:01.250+08:00 | 1125 |", exported)
        self.assertLess(exported.index("| 资源乙 | case-b | 1 |"), exported.index("| 资源甲 | case-a | 2 |"))

    def test_old_duration_does_not_invent_end_first_token_or_execution_order(self):
        exported = render_quality_markdown({"created": 10, "started_at": 20, "elapsed": 30,
            "resources": [{"name": "旧资源", "case_results": [{"case_id": "old-case", "ts": 1000,
                                                                    "total_ms": 25}]}]})
        self.assertIn("| 结束 | 未记录 |", exported)
        self.assertIn("| 发现意外中断 | 未记录 |", exported)
        self.assertIn("| 运行耗时（毫秒） | 30000 |", exported)
        self.assertIn("| 旧资源 | old-case | 未记录 | 1970-01-01T08:00:01.000+08:00 | 未记录 | 25 |", exported)
        self.assertNotIn("1970-01-01T08:00:01.025+08:00", exported)

    def test_existing_offset_times_convert_without_mutating_original_evidence(self):
        original = {
            "timing": {"timezone": "UTC", "created_at": "2026-10-15T20:00:00.125Z",
                       "started_at": "2026-10-15T16:00:01.250-04:00", "elapsed_ms": 125},
            "resources": [{"name": "旧资源", "case_results": [{"case_id": "old-zone", "timing": {
                "timezone": "UTC", "started_at": "2026-10-15T20:00:01.250Z",
                "ended_at": "2026-10-15T22:00:02.500+02:00", "total_ms": 1250}}]}],
        }
        exported = render_quality_markdown(original)
        self.assertIn("| 创建 | 2026-10-16T04:00:00.125+08:00 |", exported)
        self.assertIn("| 实际开始 | 2026-10-16T04:00:01.250+08:00 |", exported)
        self.assertIn("| 旧资源 | old-zone | 未记录 | 2026-10-16T04:00:01.250+08:00 | 2026-10-16T04:00:02.500+08:00 | 1250 |", exported)
        self.assertEqual(original["timing"]["created_at"], "2026-10-15T20:00:00.125Z")
        self.assertEqual(original["timing"]["timezone"], "UTC")

    def test_invalid_time_duration_and_sequence_are_not_echoed(self):
        exported = render_quality_markdown({
            "timing": {"created_at": "unsafe-created-value", "ended_at": "2026-10-15T16:00:00",
                       "elapsed_ms": "unsafe-elapsed-value"},
            "resources": [{"name": "资源", "case_results": [{"case_id": "case-1",
                "execution_sequence": "unsafe-sequence-value", "timing": {
                    "started_at": "unsafe-start-value", "ended_at": "unsafe-end-value",
                    "total_ms": "unsafe-duration-value"}}]}],
        })
        self.assertNotIn("unsafe-", exported)
        self.assertNotIn("2026-10-15T16:00:00", exported)
        self.assertIn("| 资源 | case-1 | 未记录 | 未记录 | 未记录 | 未记录 |", exported)


if __name__ == "__main__":
    unittest.main()
