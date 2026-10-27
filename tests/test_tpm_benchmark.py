"""Local benchmark diagnostics must preserve pressure and frozen acceptance rules."""
import copy
from contextlib import redirect_stderr
from datetime import datetime, timezone
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.benchmark_tpm import (
    FROZEN_CRITERIA, acceptance_diagnostics, admission_diagnostics, main, missed_reasons_reconcile,
    scheduling_diagnostics, upstream_evidence_checks, workbench_command,
)


def evidence():
    origin = 1_792_082_140.0713758
    stages = [dict(index=1, target_tpm=1000, duration_seconds=20, rps=2, planned_requests=40),
              dict(index=2, target_tpm=2000, duration_seconds=20, rps=4, planned_requests=80)]
    run = {'formal_started_at': origin, 'config': dict(FROZEN_CRITERIA, target_duration_seconds=20),
           'plan': {'stages': stages, 'criteria': dict(FROZEN_CRITERIA)},
           'stages': [dict(stage, started_at=origin + index * 20) for index, stage in enumerate(stages)],
           'series': [], 'report': {'missed': 0, 'planned': 120, 'target_missed': 0, 'target_planned': 80}}
    run['report'].update(target_success_rate=1, successful_usage_complete_rate=1, local_limited=False,
                         sustained_backlog=False, latency_worsened=False, errors_worsened=False,
                         verdict='insufficient_evidence', primary_window={'valid': False, 'ratio': None, 'success_rate': None})
    run['timing'] = {key: datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='milliseconds')
                     for key, value in {'started_at': origin - 20, 'ended_at': origin + 41,
                                       'dispatch_ended_at': origin + 40}.items()}
    run['timing'].update(elapsed_ms=61000, drain_ms=1000)
    rows = []
    for stage in run['stages']:
        for slot in range(stage['planned_requests']):
            at = stage['started_at'] + slot / stage['rps']
            rows.append({'id': f"{stage['index']}-{slot}", 'phase': 'formal', 'stage_index': stage['index'],
                         'scheduled_at': at, 'started_at': at + .001, 'schedule_delay_seconds': .001,
                         'pool_wait_seconds': .0001})
    return run, rows


class TpmBenchmarkDiagnosticsTests(unittest.TestCase):
    def test_normal_command_never_loads_probe_and_diagnostic_modes_are_explicit(self):
        normal = workbench_command(SimpleNamespace(diagnostics=False), Path('/tmp/synthetic'))
        self.assertTrue(normal[-1].endswith('/route_test_ui.py'))
        self.assertNotIn('--mode', normal)
        full = workbench_command(SimpleNamespace(diagnostics=True, sparse_diagnostics=False), Path('/tmp/synthetic'))
        sparse = workbench_command(SimpleNamespace(diagnostics=False, sparse_diagnostics=True), Path('/tmp/synthetic'))
        self.assertEqual(full[-2:], ['--mode', 'full'])
        self.assertEqual(sparse[-2:], ['--mode', 'sparse'])
        self.assertEqual(full[:-1], sparse[:-1])

    def test_full_and_sparse_diagnostics_are_mutually_exclusive_before_work(self):
        with patch('sys.argv', ['benchmark_tpm.py', '--diagnostics', '--sparse-diagnostics']), \
             patch('tools.benchmark_tpm.benchmark', side_effect=AssertionError('must not start work')), \
             redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                main()
        self.assertEqual(caught.exception.code, 2)

    def test_admission_checks_signed_delay_and_strict_per_stage_cutoff(self):
        run, rows = evidence()
        for row in rows:
            stage = run['stages'][row['stage_index'] - 1]
            slot = round((row['scheduled_at'] - stage['started_at']) * stage['rps'])
            row['signed_schedule_delay_seconds'] = .001
            row['stage_deadline_margin_seconds'] = stage['duration_seconds'] - slot / stage['rps'] - .001
        self.assertTrue(all(admission_diagnostics(run, rows)['checks'].values()))
        # Existing clamped delay and global dispatch checks cannot expose this.
        rows[0]['signed_schedule_delay_seconds'] = -.0000001
        rows[0]['stage_deadline_margin_seconds'] += .0010001
        result = admission_diagnostics(run, rows)
        self.assertEqual(result['early_starts'], 1)
        self.assertFalse(result['checks']['no_early_starts'])
        self.assertTrue(result['checks']['stage_admission_matches_frozen_plan'])
        first_stage_last = rows[39]
        first_stage_last.update(signed_schedule_delay_seconds=.5, stage_deadline_margin_seconds=0)
        result = admission_diagnostics(run, rows)
        self.assertEqual(result['starts_at_or_after_stage_end'], 1)
        self.assertFalse(result['checks']['no_starts_at_or_after_stage_end'])
        self.assertTrue(result['checks']['stage_admission_matches_frozen_plan'])

    def test_arithmetic_tolerance_cannot_hide_submicrosecond_cutoff_violation(self):
        run, rows = evidence()
        row = rows[39]
        row.update(signed_schedule_delay_seconds=.50000025, stage_deadline_margin_seconds=.00000025)
        result = admission_diagnostics(run, [row])
        self.assertEqual(result['starts_at_or_after_stage_end'], 1)
        self.assertFalse(result['checks']['no_starts_at_or_after_stage_end'])
        self.assertFalse(all(result['checks'].values()))

    def test_admission_missing_invalid_or_forged_evidence_fails_closed(self):
        run, rows = evidence()
        self.assertFalse(admission_diagnostics(run, rows)['checks']['stage_admission_evidence_complete'])
        self.assertFalse(all(admission_diagnostics(run, [])['checks'].values()))
        row = rows[0]
        row.update(signed_schedule_delay_seconds=.001, stage_deadline_margin_seconds=19.999)
        self.assertTrue(all(admission_diagnostics(run, [row])['checks'].values()))
        for value in (None, True, float('nan'), float('inf')):
            with self.subTest(value=value):
                result = admission_diagnostics(run, [dict(row, signed_schedule_delay_seconds=value)])
                self.assertFalse(result['checks']['stage_admission_evidence_complete'])
        self.assertFalse(admission_diagnostics(run, [dict(row, stage_deadline_margin_seconds=20)])['checks']['stage_admission_matches_frozen_plan'])
        self.assertFalse(admission_diagnostics(run, [dict(row, scheduled_at=row['scheduled_at'] + .2)])['checks']['stage_admission_matches_frozen_plan'])

    def test_scheduled_slot_attribution_is_independent_of_actual_start_bucket(self):
        run, rows = evidence()
        rows = [row for row in rows if row['id'] not in {'1-19', '1-20', '2-4'}]
        # A delayed start crosses the 10-second boundary. Its scheduled slot still
        # belongs to the first bucket, and the missing slots remain distinct.
        row = next(row for row in rows if row['id'] == '1-18')
        row['started_at'] += 2
        row['schedule_delay_seconds'] += 2
        before = copy.deepcopy((run, rows))
        result = scheduling_diagnostics(run, rows)
        self.assertEqual((run, rows), before)
        self.assertEqual(result['reconstructed_missed'], 3)
        self.assertEqual([stage['missed'] for stage in result['stages']], [2, 1])
        self.assertEqual([bucket['missed'] for bucket in result['time_buckets']], [1, 1, 1, 0])
        self.assertEqual(result['time_buckets'][0]['started_for_scheduled_slots'], 19)
        self.assertEqual(result['time_buckets'][0]['actual_starts'], 18)
        self.assertEqual(result['missing_slot_ranges'][0]['count'], 2)
        self.assertEqual(result['missing_slot_ranges'][0]['first_slot'], 19)
        self.assertTrue(result['missing_slot_ranges'][0]['scheduled_at'].endswith('+08:00'))
        self.assertEqual(result['largest_start_gaps'][0]['from_request_id'], '1-17')
        self.assertAlmostEqual(result['largest_start_gaps'][0]['gap_ms'], 2000, places=3)

    def test_absent_stage_is_reported_as_unstarted_and_not_lost_from_plan(self):
        run, rows = evidence()
        run['stages'] = run['stages'][:1]
        rows = [row for row in rows if row['stage_index'] == 1]
        result = scheduling_diagnostics(run, rows)
        self.assertEqual(result['reconstructed_missed'], 80)
        self.assertEqual(result['stages'][1]['started'], 0)
        self.assertIsNone(result['stages'][1]['max_start_gap_ms'])

    def test_duplicate_and_unmapped_requests_are_explicit(self):
        run, rows = evidence()
        rows += [dict(rows[0], id='duplicate'), dict(rows[0], id='unknown', scheduled_at=None)]
        result = scheduling_diagnostics(run, rows)
        self.assertEqual(result['duplicate_slots'], 1)
        self.assertEqual(result['unmapped_requests'], 1)
        self.assertEqual(result['reconstructed_missed'], 0)

    def test_observer_and_inflight_samples_follow_time_buckets(self):
        run, rows = evidence()
        at = run['formal_started_at']
        run['series'] = [{'at': at + 9, 'in_flight': 3}, {'at': at + 10, 'in_flight': 4}]
        result = scheduling_diagnostics(run, rows, [{'at': at + 9, 'elapsed_ms': 100},
                                                   {'at': at + 10, 'elapsed_ms': 200}])
        self.assertEqual(result['time_buckets'][0]['sampled_peak_inflight'], 3)
        self.assertEqual(result['time_buckets'][0]['observer_http_max_ms'], 100)
        self.assertEqual(result['time_buckets'][1]['sampled_peak_inflight'], 4)
        self.assertEqual(result['time_buckets'][1]['observer_http_max_ms'], 200)
        self.assertIsNone(result['time_buckets'][2]['observer_http_max_ms'])

    def test_one_percent_limit_is_not_replaced_by_five_percent_rate_tolerance(self):
        run, _ = evidence()
        run['report'].update(planned=1000, missed=10, target_planned=1000, target_missed=10)
        self.assertTrue(all(acceptance_diagnostics(run)['checks'].values()))
        run['report']['target_missed'] = 11
        checks = acceptance_diagnostics(run)['checks']
        self.assertTrue(checks['formal_missed_within_frozen_limit'])
        self.assertFalse(checks['target_missed_within_frozen_limit'])
        run['report'].update(missed=20, target_missed=20)
        checks = acceptance_diagnostics(run)['checks']
        self.assertFalse(checks['formal_missed_within_frozen_limit'])
        self.assertFalse(checks['target_missed_within_frozen_limit'])

    def test_frozen_policy_drift_is_rejected_even_if_missed_ratio_passes(self):
        for location in ('config', 'plan'):
            with self.subTest(location=location):
                run, _ = evidence()
                if location == 'config':
                    run['config']['max_missed_ratio'] = .05
                else:
                    run['plan']['criteria']['max_missed_ratio'] = .05
                self.assertFalse(acceptance_diagnostics(run)['checks']['frozen_criteria_preserved'])

    def test_empty_pressure_cannot_pass_missed_ratio_checks(self):
        run, _ = evidence()
        run['report'].update(planned=0, missed=0, target_planned=0, target_missed=0)
        result = acceptance_diagnostics(run)
        self.assertIsNone(result['formal_missed_ratio'])
        self.assertFalse(result['checks']['formal_missed_within_frozen_limit'])
        self.assertFalse(result['checks']['target_missed_within_frozen_limit'])

    def test_wall_clock_jump_does_not_change_monotonic_gaps_or_missing_slots(self):
        run, rows = evidence()
        baseline = scheduling_diagnostics(run, rows)
        self.assertTrue(baseline['clock_evidence']['consistent'])
        for row in rows[40:]:
            row['started_at'] += 30
        result = scheduling_diagnostics(run, rows)
        self.assertEqual(result['largest_start_gaps'], baseline['largest_start_gaps'])
        self.assertEqual(result['time_buckets'], baseline['time_buckets'])
        self.assertEqual(result['reconstructed_missed'], 0)
        self.assertFalse(result['clock_evidence']['consistent'])
        self.assertAlmostEqual(result['clock_evidence']['request_max_abs_wall_offset_ms'], 30000, places=2)

    def test_clock_jump_after_last_start_is_detected_from_task_and_drain_duration(self):
        run, rows = evidence()
        run['timing']['elapsed_ms'] += 1000
        run['timing']['drain_ms'] += 1000
        result = scheduling_diagnostics(run, rows)
        self.assertFalse(result['clock_evidence']['consistent'])
        self.assertAlmostEqual(result['clock_evidence']['task_wall_minus_monotonic_ms'], -1000)
        self.assertAlmostEqual(result['clock_evidence']['drain_wall_minus_monotonic_ms'], -1000)

    def test_missing_clock_evidence_does_not_silently_pass(self):
        run, rows = evidence()
        del run['timing']['drain_ms']
        result = scheduling_diagnostics(run, rows)
        self.assertFalse(result['clock_evidence']['complete'])
        self.assertFalse(result['clock_evidence']['consistent'])

    def test_reason_counts_must_reconcile_both_global_and_per_stage_totals(self):
        run, _ = evidence()
        run['metrics'] = {'missed': 3}
        run['scheduling'] = {'missed_by_reason': {'scheduler_late': 2, 'concurrency_limit': 1},
                             'missed_by_stage': {'1': {'scheduler_late': 1},
                                                 '2': {'scheduler_late': 1, 'concurrency_limit': 1}}}
        self.assertTrue(missed_reasons_reconcile(run))
        run['scheduling']['missed_by_stage']['2']['scheduler_late'] = 2
        self.assertFalse(missed_reasons_reconcile(run))
        run['scheduling']['missed_by_stage']['2']['scheduler_late'] = 1
        run['metrics']['missed'] = 4
        self.assertFalse(missed_reasons_reconcile(run))
        run.pop('scheduling')
        self.assertFalse(missed_reasons_reconcile(run))

    def test_zero_missed_slots_cannot_hide_failed_requests_or_missing_usage(self):
        for field, value, check in (
            ('target_success_rate', .98, 'target_success_rate_meets_frozen_minimum'),
            ('target_success_rate', None, 'target_success_rate_meets_frozen_minimum'),
            ('successful_usage_complete_rate', .999, 'successful_usage_complete'),
            ('successful_usage_complete_rate', None, 'successful_usage_complete'),
        ):
            with self.subTest(field=field, value=value):
                run, _ = evidence()
                run['report'][field] = value
                checks = acceptance_diagnostics(run)['checks']
                self.assertTrue(checks['target_missed_within_frozen_limit'])
                self.assertFalse(checks[check])
                self.assertFalse(all(checks.values()))

    def test_instability_cannot_pass_even_with_perfect_usage_and_success(self):
        for field, check in (
            ('local_limited', 'report_not_local_limited'),
            ('sustained_backlog', 'no_sustained_backlog'),
            ('latency_worsened', 'no_latency_degradation'),
            ('errors_worsened', 'no_error_degradation'),
        ):
            for value in (True, None):
                with self.subTest(field=field, value=value):
                    run, _ = evidence()
                    run['report'][field] = value
                    self.assertFalse(acceptance_diagnostics(run)['checks'][check])

    def test_complete_window_requires_accepted_verdict_ratio_and_success_rate(self):
        for verdict, ratio, success, valid, expected in (
            ('near', .95, .99, True, True),
            ('reached', 1, 1, True, True),
            ('not_reached', .96, 1, True, False),
            ('insufficient_evidence', 1, 1, True, False),
            ('near', .949999, 1, True, False),
            ('near', .96, .989999, True, False),
            ('near', None, 1, True, False),
            ('near', 1, None, True, False),
            ('near', 1, 1, False, False),
        ):
            with self.subTest(verdict=verdict, ratio=ratio, success=success, valid=valid):
                run, _ = evidence()
                run['config']['target_duration_seconds'] = 90
                run['report'].update(verdict=verdict, primary_window={'valid': valid, 'ratio': ratio, 'success_rate': success})
                result = acceptance_diagnostics(run)
                self.assertEqual(result['checks']['target_window_meets_frozen_acceptance'], expected)
                self.assertEqual(run['report']['verdict'], verdict)

    def test_short_run_exempts_only_minute_window_evidence(self):
        run, _ = evidence()
        result = acceptance_diagnostics(run)
        self.assertFalse(result['full_window_required'])
        self.assertTrue(all(result['checks'].values()))
        self.assertEqual(run['report']['verdict'], 'insufficient_evidence')
        run['report']['target_success_rate'] = .98
        self.assertFalse(all(acceptance_diagnostics(run)['checks'].values()))
        run['config']['target_duration_seconds'] = 60
        self.assertFalse(acceptance_diagnostics(run)['checks']['target_window_meets_frozen_acceptance'])

    def test_independent_upstream_receipts_detect_common_engine_and_persistence_loss(self):
        received = [300000, 305674, 305676]
        persisted = [{'body_bytes': value} for value in received]
        self.assertTrue(all(upstream_evidence_checks(received, persisted).values()))
        checks = upstream_evidence_checks(received, persisted[:-1])
        self.assertFalse(checks['upstream_request_count_reconciled'])
        self.assertFalse(checks['upstream_request_bytes_reconciled'])

    def test_independent_byte_totals_detect_wrong_sizes_even_when_request_counts_match(self):
        checks = upstream_evidence_checks([300000, 305674], [{'body_bytes': 300000}, {'body_bytes': 305673}])
        self.assertTrue(checks['upstream_request_count_reconciled'])
        self.assertFalse(checks['upstream_request_bytes_reconciled'])
        checks = upstream_evidence_checks([300000], [{}])
        self.assertTrue(checks['upstream_request_count_reconciled'])
        self.assertFalse(checks['upstream_request_bytes_reconciled'])


if __name__ == '__main__':
    unittest.main()
