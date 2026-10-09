#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实时观测只统计已返回结果，并揭示状态成功与完整响应成功的差别。"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routing_ab  # noqa: E402
import routing_compare  # noqa: E402


def record(status=200, completed=True, error_type=None, cached=None, ttft=10, total=20):
    return {"status_code": status, "completed": completed, "error_type": error_type,
            "cached_tokens": cached, "ttft_ms": ttft, "total_ms": total}


def comparison():
    def side(name):
        return {"name": name, "url": "http://127.0.0.1", "model": "fixture",
                "api_key": "synthetic-live-secret", "concurrency": 2}
    return routing_compare.RouteComparison({"name": "实时观测", "tier": 1000,
                                           "baseline": side("固定路由"),
                                           "candidate": side("智能路由")})


class LiveStatisticsTests(unittest.TestCase):
    def test_unreturned_requests_are_not_failures_and_failed_latency_is_excluded(self):
        baseline = [record(cached=512, ttft=10, total=20),
                    record(cached=0, ttft=30, total=60)]
        candidate = [record(cached=512, ttft=20, total=40),
                     record(completed=False, error_type="TIMEOUT", ttft=9000, total=18000),
                     record(status=429, completed=False, error_type="HTTP_429")]
        live = routing_ab.build_live_comparison(baseline, candidate)
        self.assertEqual(live["baseline"]["success"]["pct"], 100.0)
        self.assertEqual(live["candidate"]["success"]["pct"], 33.33)
        self.assertEqual(live["success"]["delta_pct"], -66.67)
        self.assertEqual(live["baseline"]["requests"], 2)
        self.assertEqual(live["candidate"]["requests"], 3)
        self.assertEqual(live["baseline"]["ttft_p95_ms"], 29.0)
        self.assertEqual(live["candidate"]["ttft_p95_ms"], 20.0)
        self.assertEqual(live["candidate"]["total_p95_ms"], 40.0)
        self.assertEqual(live["ttft"]["delta_pct"], -31.03)
        self.assertEqual(live["cache"]["baseline"]["pct"], 50.0)
        self.assertEqual(live["cache"]["candidate"]["pct"], 100.0)
        self.assertEqual(live["cache"]["candidate_no_usage"], 2)

    def test_http_200_without_complete_body_is_explained_separately(self):
        records = [record(), record(completed=False, error_type="PROTOCOL"),
                   record(completed=False, error_type="TIMEOUT"),
                   record(status=429, completed=False),
                   record(status=502, completed=False),
                   record(status=None, completed=False, error_type="NETWORK")]
        stat = routing_ab.build_live_comparison([], records)["candidate"]
        self.assertEqual(stat["success"]["successes"], 1)
        self.assertEqual(stat["success"]["pct"], 16.67)
        self.assertEqual(stat["diagnostics"]["http_2xx"], 3)
        self.assertEqual(stat["diagnostics"]["complete_2xx"], 1)
        self.assertEqual(stat["diagnostics"]["incomplete_2xx"], 2)
        self.assertEqual(stat["diagnostics"]["completed_responses"], 1)
        self.assertEqual(stat["diagnostics"]["failures"], 5)
        self.assertEqual(stat["diagnostics"]["failure_types"], {
            "HTTP_429": 1, "HTTP_5XX": 1, "NETWORK": 1, "PROTOCOL": 1, "TIMEOUT": 1})
        self.assertEqual(stat["diagnostics"]["status_codes"], {
            "200": 3, "429": 1, "502": 1, "no_status": 1})

    def test_empty_side_and_missing_usage_are_undefined_not_zero(self):
        live = routing_ab.build_live_comparison([], [record()])
        self.assertIsNone(live["baseline"]["success"]["pct"])
        self.assertIsNone(live["success"]["delta_pct"])
        self.assertIsNone(live["cache"]["candidate"]["pct"])
        self.assertIsNone(live["cache"]["delta_pct"])
        self.assertEqual(live["cache"]["candidate_no_usage"], 1)
        self.assertIn("暂不可比", live["cache"]["note"])

    def test_no_significance_test_or_attribution_is_computed_while_running(self):
        with mock.patch.object(routing_ab, "fisher_exact_two_sided", side_effect=AssertionError), \
                mock.patch.object(routing_ab, "mcnemar_exact", side_effect=AssertionError):
            live = routing_ab.build_live_comparison([record()], [record()])
        serialized = json.dumps(live)
        self.assertNotIn("p_value", serialized)
        self.assertNotIn("attribution", serialized)
        self.assertNotIn("mcnemar", serialized)

    def test_diagnostics_do_not_echo_sensitive_request_or_error_fields(self):
        sensitive = "https://private.invalid?key=synthetic-secret"
        response = record(status=None, completed=False, error_type=sensitive)
        response.update({"url": sensitive, "api_key": sensitive, "raw_input": sensitive,
                         "raw_output": sensitive, "reason": sensitive, "error_detail": sensitive})
        live = routing_ab.build_live_comparison([], [response])
        self.assertEqual(live["candidate"]["diagnostics"]["failure_types"], {"UNKNOWN": 1})
        self.assertNotIn(sensitive, json.dumps(live))

    def test_unclassified_statuses_have_safe_failure_categories(self):
        stats = routing_ab.response_diagnostics([
            record(status=408, completed=False), record(status=401, completed=False),
            record(status=302, completed=False), record(completed=False),
            record(status=0, completed=False)])
        self.assertEqual(stats["failure_types"], {"HTTP_4XX": 1, "HTTP_OTHER": 1,
                                                "PROTOCOL": 1, "TIMEOUT": 1, "UNKNOWN": 1})
        self.assertEqual(stats["status_codes"]["no_status"], 1)


class LiveSnapshotTests(unittest.TestCase):
    def test_final_diagnostics_survive_history_restore_without_loading_raw_records(self):
        run = comparison()
        baseline = [dict(record(), item_id="synthetic-1")]
        candidate = [dict(record(status=429, completed=False), item_id="synthetic-1")]
        run._report = routing_ab.build_comparison(baseline, candidate)
        run.status = "finished"
        restored = routing_compare.RouteComparison.from_snapshot(run.snap())
        snapshot = restored.snap()
        self.assertIsNone(snapshot["live_report"])
        self.assertEqual(snapshot["report"]["candidate"]["diagnostics"]["failure_types"], {"HTTP_429": 1})
        self.assertEqual(snapshot["report"]["baseline"]["success"]["pct"], 100.0)
        self.assertEqual(snapshot["report"]["candidate"]["success"]["pct"], 0.0)

    def test_running_snapshot_is_updated_and_cached_without_changing_final_report(self):
        run = comparison()
        run.status = "running"
        with mock.patch.object(routing_ab, "build_live_comparison", wraps=routing_ab.build_live_comparison) as build:
            initial = run.snap()
            self.assertIsNone(initial["report"])
            self.assertTrue(initial["live_report"]["partial"])
            self.assertEqual(initial["live_report"]["candidate"]["requests"], 0)
            run.snap()
            self.assertEqual(build.call_count, 1)
            run._record("candidate", record(completed=False, error_type="PROTOCOL"))
            updated = run.snap()
            self.assertEqual(updated["live_report"]["candidate"]["success"]["pct"], 0.0)
            self.assertEqual(updated["live_report"]["candidate"]["diagnostics"]["incomplete_2xx"], 1)
            self.assertEqual(build.call_count, 2)
            self.assertIsNone(updated["report"])
            # 缓存更新不能改写调用方之前拿到的观测。
            self.assertEqual(initial["live_report"]["candidate"]["requests"], 0)
            run.status = "stopped"
            finished = run.snap()
            self.assertTrue(finished["live_report"]["partial"])
            self.assertEqual(build.call_count, 2)
            self.assertIsInstance(finished["live_report"]["observed_at"], (int, float))
            self.assertNotIn("synthetic-live-secret", json.dumps(finished))

    def test_only_a_finished_run_with_all_planned_results_is_complete(self):
        run = comparison()
        run.total = 2
        run._record("baseline", record())
        run._record("candidate", record())
        run.status = "finished"
        self.assertTrue(run.snap()["live_report"]["partial"])
        run._record("baseline", record())
        run._record("candidate", record())
        self.assertFalse(run.snap()["live_report"]["partial"])
        for state in ("stopped", "error"):
            run.status = state
            self.assertTrue(run.snap()["live_report"]["partial"])

    def test_snapshot_mutation_does_not_change_cached_observations(self):
        run = comparison()
        run._record("baseline", record())
        snapshot = run.snap()
        snapshot["live_report"]["baseline"]["success"]["pct"] = -1
        snapshot["live_report"]["baseline"]["diagnostics"]["failure_types"]["NETWORK"] = 100
        later = run.snap()
        self.assertEqual(later["live_report"]["baseline"]["success"]["pct"], 100.0)
        self.assertEqual(later["live_report"]["baseline"]["diagnostics"]["failure_types"], {})

    def test_history_without_records_does_not_recompute_live_statistics(self):
        source = comparison()
        source._record("baseline", record())
        source._record("candidate", record())
        source.status = "finished"
        source._report = routing_ab.build_comparison(source.records["baseline"], source.records["candidate"])
        snapshot = source.snap()
        history = routing_compare.RouteComparison.from_snapshot(snapshot)
        with mock.patch.object(routing_ab, "build_live_comparison", side_effect=AssertionError):
            restored = history.snap()
        self.assertIsNone(restored["live_report"])
        self.assertEqual(restored["report"], snapshot["report"])
        self.assertEqual(restored["sides"]["candidate"]["records"], 1)


if __name__ == "__main__":
    unittest.main()
