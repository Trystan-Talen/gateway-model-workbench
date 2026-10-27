"""Phase/clock evidence supplements, rather than changing, ps sampling."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools.tpm_observation import ClockReader, ObservationRecorder, task_context


def run_context(phase='running'):
    return {'phase': phase, 'request_count': 55,
            'metrics': {'started': 40, 'succeeded': 30, 'failed': 1, 'in_flight': 9, 'all_in_flight': 9},
            'stages': [{'index': 4, 'target_tpm': 200_000_000}],
            'plan': {'stages': [{'index': 1, 'target_tpm': 50_000_000},
                                {'index': 4, 'target_tpm': 200_000_000}]},
            'formal_started_at': 1792140000., 'dispatch_ended_at': None,
            'config': {'api_key': 'NEVER_SAVE_THIS'}, 'recent_requests': [{'raw_input': 'NEVER_SAVE_THIS'}]}


class FakeClocks:
    def __init__(self, *, jump=False, raw=True):
        self.index, self.jump, self.raw = 0, jump, raw

    def metadata(self):
        return {'status': 'available' if self.raw else 'unknown', 'source': 'synthetic' if self.raw else None}

    def point(self):
        self.index += 1
        elapsed = self.index * 1_000_000
        jump = 1_000_000_000 if self.jump and self.index >= 3 else 0
        return {'wall_ns': 1792140000_000_000_000 + elapsed + jump,
                'monotonic_ns': 200_000_000_000 + elapsed,
                'raw_monotonic_ns': 300_000_000_000 + elapsed if self.raw else None,
                'capture_begin_perf_ns': 200_000_000_100 + elapsed,
                'capture_end_perf_ns': 200_000_000_110 + elapsed, 'capture_span_ns': 10}


class TpmObservationTests(unittest.TestCase):
    def sample(self, recorder, value=b'12345\n', run=None):
        async def read():
            return value
        return asyncio.run(recorder.sample_rss(run or run_context(), read))

    def finish(self, recorder, output):
        recorder.mark('export_before', run_context('finished'))
        recorder.mark('export_after', run_context('finished'))
        return recorder.save(output, benchmark_complete=True)

    def test_context_preserves_phase_stage_and_counts_without_payload(self):
        context = task_context(run_context())
        self.assertEqual(context['category'], 'dispatching')
        self.assertEqual(context['stage_index'], 4)
        self.assertTrue(context['is_target_stage'])
        self.assertEqual(context['formal_started'], 40)
        self.assertEqual(context['formal_completed'], 31)
        self.assertEqual(context['completed_record_count'], 55)
        self.assertNotIn('NEVER_SAVE_THIS', json.dumps(context))

    def test_missing_or_invalid_counters_remain_unknown(self):
        for run in ({}, {'phase': 'running', 'metrics': {'started': True, 'succeeded': 1}},
                    {'phase': 'running', 'request_count': -1}):
            with self.subTest(run=run):
                context = task_context(run)
                self.assertIsNone(context['formal_started'])
                self.assertIsNone(context['formal_completed'])
                self.assertIsNone(context['completed_record_count'])
        self.assertEqual(task_context({'request_count': 0})['completed_record_count'], 0)

    def test_draining_report_and_stage_transition_are_explicit(self):
        self.assertEqual(task_context(run_context('draining'))['category'], 'draining')
        self.assertEqual(task_context(run_context('finished'))['category'], 'final_report')
        run = run_context()
        run['stages'][-1]['ended_at'] = 1792140001
        context = task_context(run)
        self.assertIsNone(context['stage_index'])
        self.assertEqual(context['latest_stage_index'], 4)
        self.assertIsNone(context['is_target_stage'])

    def test_one_ps_call_returns_original_rss_and_brackets_timing(self):
        recorder = ObservationRecorder(clocks=FakeClocks())
        calls = []
        async def read():
            calls.append(1)
            return b' 12345\n'
        self.assertEqual(asyncio.run(recorder.sample_rss(run_context(), read)), 12345)
        self.assertEqual(calls, [1])
        sample = recorder.samples[0]
        self.assertEqual(sample['read_duration_ms'], 1)
        self.assertEqual(sample['before']['monotonic_offset_ns'], 1_000_000)
        self.assertEqual(sample['after']['monotonic_offset_ns'], 2_000_000)
        self.assertEqual(sample['before']['capture_span_ns'], 10)
        self.assertEqual(sample['after']['monotonic_elapsed_minus_raw_ns'], 0)
        self.assertEqual(sample['status'], 'known')
        self.assertTrue(sample['before']['at'].endswith('+08:00'))

    def test_paired_points_expose_wall_jump_without_adjusting_timestamps(self):
        recorder = ObservationRecorder(clocks=FakeClocks(jump=True))
        self.sample(recorder)
        sample = recorder.samples[0]
        self.assertEqual(sample['read_wall_minus_monotonic_ns'], 1_000_000_000)
        self.assertEqual(sample['after']['wall_elapsed_minus_monotonic_ns'], 1_000_000_000)
        self.assertEqual(sample['after']['monotonic_elapsed_minus_raw_ns'], 0)

    def test_raw_clock_unavailable_is_unknown_not_zero_or_monotonic_alias(self):
        fake = SimpleNamespace(time_ns=lambda: 100, monotonic_ns=lambda: 20,
                               perf_counter_ns=Mock(side_effect=[5, 7]))
        clocks = ClockReader(fake)
        self.assertEqual(clocks.metadata()['status'], 'unknown')
        self.assertIsNone(clocks.metadata()['source'])
        self.assertIsNone(clocks.point()['raw_monotonic_ns'])
        recorder = ObservationRecorder(clocks=FakeClocks(raw=False))
        self.sample(recorder)
        self.assertIsNone(recorder.samples[0]['after']['monotonic_elapsed_minus_raw_ns'])

    def test_available_raw_clock_is_paired_and_runtime_failure_is_explicit(self):
        raw = Mock(side_effect=[99, 100, OSError('unsupported')])
        fake = SimpleNamespace(CLOCK_MONOTONIC_RAW=4, clock_gettime_ns=raw,
                               time_ns=lambda: 1000, monotonic_ns=lambda: 200,
                               perf_counter_ns=Mock(side_effect=[1, 3, 4, 8]))
        clocks = ClockReader(fake)
        self.assertEqual(clocks.metadata()['status'], 'available')
        self.assertEqual(clocks.point()['raw_monotonic_ns'], 100)
        self.assertIsNone(clocks.point()['raw_monotonic_ns'])
        self.assertEqual(clocks.metadata()['status'], 'unknown')
        self.assertEqual(clocks.metadata()['error_type'], 'OSError')
        self.assertEqual(raw.call_count, 3)

    def test_invalid_rss_and_read_failure_never_become_zero_cost_evidence(self):
        for value in (b'-1', b'not a number', b''):
            recorder = ObservationRecorder(clocks=FakeClocks())
            self.assertIsNone(self.sample(recorder, value))
            self.assertEqual(recorder.samples[0]['status'], 'unknown')
            self.assertIsNone(recorder.samples[0]['rss_kib'])
            with tempfile.TemporaryDirectory() as folder:
                result = self.finish(recorder, Path(folder) / 'observation.json')
            self.assertFalse(result['complete'])
            self.assertEqual(result['peak_rss_kib_by_category'], {})
        recorder = ObservationRecorder(clocks=FakeClocks())
        async def fail():
            raise OSError('NEVER_SAVE_THIS')
        self.assertIsNone(asyncio.run(recorder.sample_rss(run_context(), fail)))
        self.assertNotIn('NEVER_SAVE_THIS', json.dumps(recorder.samples + recorder.failures))

    def test_final_markers_do_not_read_rss_or_change_original_peak(self):
        recorder = ObservationRecorder(clocks=FakeClocks())
        self.sample(recorder, b'12000')
        self.sample(recorder, b'14000', run_context('draining'))
        self.sample(recorder, b'13000', run_context('finished'))
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'observation.json'
            result = self.finish(recorder, output)
            document = json.loads(output.read_text())
        self.assertTrue(result['complete'])
        self.assertTrue(result['saved'])
        self.assertEqual(result['sample_count'], 3)
        self.assertEqual(result['marker_count'], 2)
        self.assertEqual(result['peak_rss_kib_by_category'],
                         {'dispatching': 12000, 'draining': 14000, 'final_report': 13000})
        self.assertEqual(result['target_peak_rss_kib'], 12000)
        self.assertNotIn('rss_kib', document['markers'][0])
        self.assertNotIn('NEVER_SAVE_THIS', json.dumps(document))

    def test_abort_missing_markers_and_bounded_samples_fail_closed(self):
        recorder = ObservationRecorder(clocks=FakeClocks(), sample_limit=1)
        self.sample(recorder)
        self.sample(recorder)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'observation.json'
            result = self.finish(recorder, output)
            self.assertFalse(result['complete'])
            self.assertEqual(result['dropped_samples'], 1)
            self.assertFalse(recorder.save(output, benchmark_complete=False)['complete'])
        recorder = ObservationRecorder(clocks=FakeClocks())
        self.sample(recorder)
        with tempfile.TemporaryDirectory() as folder:
            self.assertFalse(recorder.save(Path(folder) / 'observation.json', benchmark_complete=True)['complete'])

    def test_write_or_rename_failure_never_claims_complete_saved_evidence(self):
        for method in ('write_text', 'replace'):
            recorder = ObservationRecorder(clocks=FakeClocks())
            self.sample(recorder)
            with tempfile.TemporaryDirectory() as folder, \
                 patch.object(Path, method, side_effect=PermissionError('private path')):
                output = Path(folder) / 'observation.json'
                result = self.finish(recorder, output)
                self.assertFalse(result['complete'])
                self.assertFalse(result['saved'])
                self.assertEqual(result['save_error_type'], 'PermissionError')
                self.assertFalse(output.exists())
                self.assertFalse(output.with_suffix('.json.tmp').exists())


if __name__ == '__main__':
    unittest.main()
