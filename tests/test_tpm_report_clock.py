"""Clock consistency must gate capacity verdicts without rewriting evidence."""
import copy
import unittest

from tpm_engine import TpmRun
from tpm_report import CLOCK_EVIDENCE_TOLERANCE_MS, build_report, report_clock_evidence


def fixture():
    config = {"target_tpm": 200_000_000, "cache_mode": "avoid_cache"}
    rows = [{"id": "synthetic", "phase": "formal", "stage_index": 4,
             "scheduled_at": 150.0, "started_at": 150.001, "ended_at": 151.001,
             "signed_schedule_delay_seconds": .001, "stage_deadline_margin_seconds": 39.999,
             "schedule_delay_seconds": .001, "latency_seconds": 1.0, "success": True,
             "usage": {"complete": True, "total_tokens": 200_000_000,
                       "input_tokens": 198_000_000, "full_input_tokens": 198_000_000,
                       "output_tokens": 2_000_000, "cache_read_tokens": 0, "cache_write_tokens": 0}}]
    stages = [{"index": 4, "target_tpm": 200_000_000, "started_at": 100,
               "ended_at": 190, "planned_requests": 1}]
    series = [{"at": 189, "window_full": True, "full_token_tpm": 200_000_000}]
    return config, rows, stages, series


class TpmReportClockTests(unittest.TestCase):
    def report(self, data, **kwargs):
        return build_report(*data, dispatch_ended_at=190, last_ended_at=191, planned=1, **kwargs)

    def assert_blocked(self, report, status):
        self.assertEqual(report["verdict"], "insufficient_evidence")
        self.assertFalse(report["stable"])
        self.assertFalse(report["primary_window"]["valid"])
        self.assertEqual(report["clock_evidence"]["status"], status)
        self.assertFalse(report["primary_window"]["time_evidence_verified"])
        self.assertEqual(report["primary_window"]["measurement_status"], "unverified")

    def test_time_evidence_rule_is_disclosed_and_frozen_before_run(self):
        run = TpmRun({"url": "https://mock.invalid/v1", "api_key": "synthetic",
                      "model": "synthetic", "cache_write_price_mode": "input"})
        policy = run.plan["time_evidence_policy"]
        self.assertEqual(policy["max_clock_offset_ms"], CLOCK_EVIDENCE_TOLERANCE_MS)
        self.assertEqual(policy["missing_evidence"], "insufficient_evidence")
        self.assertTrue(any("5 毫秒" in warning and "证据不足" in warning for warning in run.plan["warnings"]))
        frozen_hash = run._plan_hash()
        policy["max_clock_offset_ms"] += 1
        self.assertNotEqual(run._plan_hash(), frozen_hash)

    def test_consistent_new_records_allow_existing_strict_capacity_verdict(self):
        data = fixture()
        before = copy.deepcopy(data)
        report = self.report(data, require_clock_evidence=True)
        self.assertEqual(report["verdict"], "reached")
        self.assertTrue(report["stable"])
        self.assertTrue(report["primary_window"]["valid"])
        self.assertEqual(report["clock_evidence"]["status"], "consistent")
        self.assertTrue(report["clock_evidence"]["complete"])
        self.assertEqual(report["clock_evidence"]["tolerance_ms"], 5)
        self.assertEqual(report["primary_window"]["measurement_status"], "verified")
        self.assertEqual(data, before)

    def test_late_encoding_failure_invalidates_otherwise_complete_target_window(self):
        config, rows, stages, series = fixture()
        run = TpmRun({**config, "url": "https://mock.invalid/v1", "api_key": "synthetic",
                      "model": "synthetic", "cache_write_price_mode": "input"})
        run.requests, run.stages, run.series = rows, stages, series
        run.dispatch_ended_at, run._planned = 190, 1
        run._build_report()
        self.assertEqual(run.report["verdict"], "reached")
        original_window = dict(run.report["primary_window"])
        run._evidence_encoding_error = True
        run.stop_reason = "persistence_error"
        run._build_report()
        self.assertEqual(run.report["verdict"], "insufficient_evidence")
        self.assertFalse(run.report["stable"])
        self.assertFalse(run.report["primary_window"]["valid"])
        self.assertEqual(run.report["primary_window"]["measurement_status"], "unverified")
        self.assertTrue(run.report["evidence_incomplete"])
        self.assertTrue(run.report["clock_evidence"]["consistent"], "persistence is a separate failure")
        for key in ("start", "end", "full_tokens", "ratio", "completed_requests"):
            self.assertEqual(run.report["primary_window"][key], original_window[key])

    def test_112ms_forward_and_backward_steps_invalidate_but_preserve_counts_and_times(self):
        for step in (-.112, .112):
            with self.subTest(step=step):
                data = fixture()
                data[1][0]["started_at"] += step
                data[1][0]["ended_at"] += step
                before = copy.deepcopy(data)
                report = self.report(data, require_clock_evidence=True)
                self.assert_blocked(report, "inconsistent")
                self.assertEqual(report["clock_evidence"]["start_mismatches"], 1)
                self.assertEqual(report["clock_evidence"]["end_mismatches"], 1)
                self.assertAlmostEqual(report["clock_evidence"]["max_abs_start_offset_ms"], 112)
                self.assertEqual(report["primary_window"]["full_tokens"], 200_000_000)
                self.assertEqual(report["primary_window"]["ratio"], 1)
                self.assertEqual(report["primary_window"]["completed_requests"], 1)
                self.assertEqual(report["rolling_peak_tpm"], 200_000_000)
                self.assertEqual(report["cumulative_usage"]["total_tokens"], 200_000_000)
                self.assertTrue(any("超过 5 毫秒" in note for note in report["limitations"]))
                self.assertEqual(data, before, "never rewrite real Singapore timestamps or usage")

    def test_step_after_request_start_is_detected_from_completion_projection(self):
        for step in (-.112, .112):
            with self.subTest(step=step):
                data = fixture()
                data[1][0]["ended_at"] += step
                report = self.report(data, require_clock_evidence=True)
                self.assert_blocked(report, "inconsistent")
                self.assertEqual(report["clock_evidence"]["start_mismatches"], 0)
                self.assertEqual(report["clock_evidence"]["end_mismatches"], 1)

    def test_exact_5ms_boundary_is_inclusive_without_expanding_tolerance(self):
        for shift, expected in ((.005, "consistent"), (-.005, "consistent"),
                                (.005000001, "inconsistent"), (-.005000001, "inconsistent")):
            with self.subTest(shift=shift):
                row = fixture()[1][0]
                # Small exact epoch avoids manufacturing a boundary failure from
                # rounding a 1.7-billion-second timestamp before this check.
                row.update(scheduled_at=0, signed_schedule_delay_seconds=0, latency_seconds=0,
                           started_at=shift, ended_at=shift)
                evidence = report_clock_evidence([row], required=True)
                self.assertEqual(evidence["status"], expected)
                self.assertEqual(evidence["tolerance_ms"], 5)

    def test_missing_or_invalid_new_evidence_cannot_produce_a_verified_window(self):
        fields = ("scheduled_at", "started_at", "ended_at", "signed_schedule_delay_seconds",
                  "stage_deadline_margin_seconds", "latency_seconds")
        for field in fields:
            for value in (None, True, "invalid", float("inf"), float("nan")):
                with self.subTest(field=field, value=value):
                    data = fixture()
                    data[1][0][field] = value
                    report = self.report(data, require_clock_evidence=True)
                    self.assert_blocked(report, "unknown")
                    self.assertFalse(report["clock_evidence"]["complete"])
                    self.assertIsNone(report["clock_evidence"]["consistent"])
        for field, value in (("signed_schedule_delay_seconds", -1), ("latency_seconds", -1),
                             ("stage_deadline_margin_seconds", 0), ("stage_deadline_margin_seconds", -1)):
            with self.subTest(field=field, value=value):
                data = fixture()
                data[1][0][field] = value
                self.assert_blocked(self.report(data, require_clock_evidence=True), "unknown")

    def test_partial_and_all_missing_live_evidence_are_required_even_when_keys_are_absent(self):
        data = fixture()
        missing = copy.deepcopy(data[1][0])
        missing.pop("signed_schedule_delay_seconds")
        data[1].append(missing)
        report = self.report(data)
        self.assert_blocked(report, "unknown")
        self.assertEqual(report["clock_evidence"]["missing_requests"], 1)
        self.assertEqual(report["clock_evidence"]["checked_requests"], 1)
        data = fixture()
        for field in ("signed_schedule_delay_seconds", "stage_deadline_margin_seconds"):
            data[1][0].pop(field)
        self.assert_blocked(self.report(data, require_clock_evidence=True), "unknown")
        legacy = self.report(data)
        self.assertEqual(legacy["clock_evidence"]["status"], "unknown")
        self.assertFalse(legacy["clock_evidence"]["required"])
        self.assertEqual(legacy["clock_evidence"]["source"], "legacy_missing")
        self.assertEqual(legacy["verdict"], "reached", "retain legacy arithmetic while explicitly marking it unverified")
        self.assertEqual(legacy["primary_window"]["measurement_status"], "unverified")
        self.assertTrue(any("旧记录" in note and "未知" in note for note in legacy["limitations"]))

    def test_production_report_requires_evidence_when_all_new_fields_are_missing(self):
        _, rows, stages, series = fixture()
        rows[0].pop("signed_schedule_delay_seconds")
        rows[0].pop("stage_deadline_margin_seconds")
        run = TpmRun({"url": "https://mock.invalid/v1", "api_key": "synthetic",
                      "model": "synthetic", "cache_write_price_mode": "input"})
        run.requests, run.stages, run.series = rows, stages, series
        run.dispatch_ended_at, run._planned = 190, 1
        run._build_report()
        self.assert_blocked(run.report, "unknown")
        self.assertTrue(run.report["clock_evidence"]["required"])

    def test_calibration_does_not_contaminate_formal_clock_check(self):
        data = fixture()
        calibration = copy.deepcopy(data[1][0])
        calibration.update(phase="calibration", signed_schedule_delay_seconds=None,
                           stage_deadline_margin_seconds=None, scheduled_at=None, started_at=0)
        data[1].append(calibration)
        report = self.report(data, require_clock_evidence=True)
        self.assertEqual(report["verdict"], "reached")
        self.assertEqual(report["clock_evidence"]["formal_requests"], 1)


if __name__ == "__main__":
    unittest.main()
