"""Timing probes are bounded, preserve behavior, and require isolated local data."""
import asyncio
import gc
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools.tpm_runtime_probe import (
    FORMAL_BUCKET_LIMIT, FORMAL_ORIGIN_LIMIT, SELECTOR_HISTOGRAM_LABEL, RuntimeProbe, validate_isolation,
)


class RuntimeProbeTests(unittest.TestCase):
    def test_human_notes_distinguish_sparse_samples_from_full_observation(self):
        sparse = RuntimeProbe(mode='sparse').snapshot()
        full = RuntimeProbe().snapshot()
        self.assertIn('仅记录抽样调用', sparse['statistics_note'])
        self.assertIn('仅被抽样睡眠', sparse['histogram_note'])
        self.assertIn('周期相位偏置', sparse['histogram_note'])
        self.assertNotIn('全部观测调用', sparse['statistics_note'])
        self.assertIn('全部观测调用', full['statistics_note'])
        self.assertIn('每次观测睡眠', full['histogram_note'])

    def test_sparse_mode_rejects_invalid_interval_and_reports_sample_scope(self):
        for value in (0, -1, True, .5, '32'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RuntimeProbe(mode='sparse', sample_every=value)
        with self.assertRaises(ValueError):
            RuntimeProbe(mode='unsupported')
        snapshot = RuntimeProbe(mode='sparse').snapshot()
        self.assertEqual(snapshot['mode'], 'sparse')
        self.assertEqual(snapshot['sampling']['sample_every'], 32)
        self.assertEqual(snapshot['sampling']['histogram_scope'], 'sampled_calls_only')
        self.assertEqual(snapshot['formal_delay_buckets']['sample_scope'], 'sampled_calls_only')

    def test_sparse_sleep_skips_clocks_and_marker_until_each_32nd_call(self):
        probe = RuntimeProbe(mode='sparse')
        calls = []
        run = SimpleNamespace(formal_started_at=1000., _formal_origin=10.)
        loop = SimpleNamespace(time=Mock(side_effect=[10., 10.001]))
        async def original(instance, seconds):
            calls.append(seconds)
            self.assertEqual(probe._sleep_scope.get() is not None, len(calls) % 32 == 0)
            return 7
        async def invoke():
            measured = probe.wrap_sleep(original)
            with patch('tools.tpm_runtime_probe.asyncio.get_running_loop', return_value=loop):
                with patch('tools.tpm_runtime_probe.time.monotonic', side_effect=AssertionError('skipped clock')), \
                     patch('tools.tpm_runtime_probe.time.perf_counter_ns', side_effect=AssertionError('skipped clock')):
                    for _ in range(31):
                        self.assertEqual(await measured(run, .01), 7)
                with patch('tools.tpm_runtime_probe.time.monotonic', side_effect=[20., 20.001]):
                    self.assertEqual(await measured(run, .01), 7)
        asyncio.run(invoke())
        snapshot = probe.snapshot()
        self.assertEqual(len(calls), 32)
        self.assertEqual(len(snapshot['formal_time_origins']), 1)
        self.assertEqual(snapshot['sampling']['boundaries']['sleep'],
                         {'seen': 32, 'sampled': 1, 'skipped': 31, 'sample_fraction': 1 / 32})
        self.assertEqual(snapshot['loop_delay_histograms']['TpmRun._sleep.overrun']['count'], 1)

    def test_sparse_request_entry_samples_only_each_32nd_formal_call(self):
        probe = RuntimeProbe(mode='sparse')
        calls = []
        async def original(*args, **kwargs):
            calls.append(1)
            return 'safe'
        async def invoke():
            measured = probe.wrap_arrival('TpmRun._request.entry', original, request_entry=True)
            with patch('tools.tpm_runtime_probe.time.monotonic', side_effect=AssertionError('skipped clock')):
                await measured(object(), 'calibration', due=10.)
                for _ in range(31):
                    await measured(object(), 'formal', due='UNREAD_SECRET')
            with patch('tools.tpm_runtime_probe.time.monotonic', return_value=10.001):
                self.assertEqual(await measured(object(), 'formal', due=10.), 'safe')
        asyncio.run(invoke())
        snapshot = probe.snapshot()
        self.assertEqual(len(calls), 33)
        self.assertEqual(snapshot['sampling']['boundaries']['request.entry']['sampled'], 1)
        self.assertEqual(snapshot['sampling']['boundaries']['request.entry']['seen'], 32)
        self.assertEqual(snapshot['loop_delay_histograms']['TpmRun._request.entry']['count'], 1)
        self.assertNotIn('UNREAD_SECRET', json.dumps(snapshot))

    def test_sparse_selector_skips_clock_and_counts_only_sampled_outcomes(self):
        probe = RuntimeProbe(mode='sparse')
        target = object()
        probe._selector = target
        values = []
        calls = []
        def original(selector, timeout):
            calls.append((selector, timeout))
            return values
        measured = probe.wrap_selector(original)
        with patch('tools.tpm_runtime_probe.time.monotonic', side_effect=AssertionError('skipped clock')), \
             patch('tools.tpm_runtime_probe.time.perf_counter_ns', side_effect=AssertionError('skipped clock')):
            for _ in range(31):
                self.assertIs(measured(target, .01), values)
        with patch('tools.tpm_runtime_probe.time.perf_counter_ns', side_effect=[0, 11_000_000]):
            self.assertIs(measured(target, .01), values)
        snapshot = probe.snapshot()
        self.assertEqual(len(calls), 32)
        self.assertEqual(snapshot['sampling']['boundaries']['selector.select']['skipped'], 31)
        self.assertEqual(snapshot['selector']['counts']['positive']['empty_expired'], 1)
        self.assertEqual(snapshot['selector']['count_scope'], 'sampled_select_calls_only')
        self.assertEqual(snapshot['loop_delay_histograms'][SELECTOR_HISTOGRAM_LABEL]['count'], 1)

    def test_sparse_install_leaves_frequent_boundaries_unwrapped_but_keeps_gc(self):
        class Run:
            def snap(self): return 1
            def _cache_protection(self): return 2
            def _finish(self): return 3
            def _build_report(self): return 4
            async def _sleep(self, seconds): return 5
            async def _wait_for_slot(self, due, stage_end): return 6
            async def _request(self, phase, stage_index=None, due=None, group=0): return 7
        class Handler:
            def _json(self, data): return data
        app = SimpleNamespace(Handler=Handler, persist_history=lambda: True)
        unchanged = [(Run, name, getattr(Run, name)) for name in
                     ('snap', '_cache_protection', '_finish', '_build_report', '_wait_for_slot')]
        unchanged += [(Handler, '_json', Handler._json), (app, 'persist_history', app.persist_history)]
        originals = Run._sleep, Run._request, asyncio.Timeout._on_timeout
        probe = RuntimeProbe(mode='sparse')
        configuration = gc.isenabled(), gc.get_threshold()
        try:
            probe.install(app, Run)
            self.assertIn(probe.gc_callback, gc.callbacks)
            self.assertIsNot(Run._sleep, originals[0])
            self.assertIsNot(Run._request, originals[1])
            for owner, name, original in unchanged:
                self.assertIs(getattr(owner, name), original)
        finally:
            probe.close()
        self.assertEqual((Run._sleep, Run._request, asyncio.Timeout._on_timeout), originals)
        self.assertEqual((gc.isenabled(), gc.get_threshold()), configuration)

    def test_isolation_rejects_shared_data_and_real_credentials_before_app_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / 'isolated-run'
            folder.mkdir()
            output = folder / 'runtime-probe.json'
            env = {'ROUTE_TEST_HOST': '127.0.0.1', 'ROUTE_TEST_DISABLE_CREDENTIAL_STORE': '1',
                   'ROUTE_TEST_DATA_DIR': str(folder / 'data')}
            self.assertEqual(validate_isolation(output, env, benchmark_root=root), output.resolve())
            for key, value in (('ROUTE_TEST_HOST', '0.0.0.0'), ('ROUTE_TEST_DISABLE_CREDENTIAL_STORE', '0'),
                               ('ROUTE_TEST_DATA_DIR', str(root / 'shared-data'))):
                with self.subTest(key=key), self.assertRaises(ValueError):
                    validate_isolation(output, dict(env, **{key: value}), benchmark_root=root)
            (folder / 'data').mkdir()
            (folder / 'data' / 'existing.json').write_text('{}')
            with self.assertRaises(ValueError):
                validate_isolation(output, env, benchmark_root=root)

    def test_wrapper_preserves_result_and_exception_without_recording_payload(self):
        probe = RuntimeProbe()
        secret = 'SHOULD_NEVER_BE_RECORDED'
        wrapped = probe.wrap('TpmRun.snap', lambda value: {'answer': value})
        with patch('tools.tpm_runtime_probe.time.perf_counter_ns', side_effect=[0, 3_000_000]):
            self.assertEqual(wrapped(secret), {'answer': secret})
        def fail(value):
            raise ValueError(value)
        with patch('tools.tpm_runtime_probe.time.perf_counter_ns', side_effect=[0, 4_000_000]):
            with self.assertRaisesRegex(ValueError, secret):
                probe.wrap('Handler._json', fail)(secret)
        snapshot = probe.snapshot()
        self.assertNotIn(secret, json.dumps(snapshot))
        self.assertEqual(snapshot['statistics']['TpmRun.snap']['count'], 1)
        self.assertEqual(snapshot['events'][0]['duration_ms'], 3)
        self.assertTrue(snapshot['events'][1]['raised'])
        self.assertTrue(snapshot['events'][0]['started_at'].endswith('+08:00'))
        self.assertEqual(probe.snapshot()['events'], snapshot['events'])

    def test_gc_events_are_thresholded_and_bounded_without_forcing_collection(self):
        probe = RuntimeProbe(event_limit=2)
        for generation, elapsed in ((0, 1_000_000), (1, 2_000_000), (2, 3_000_000), (0, 4_000_000)):
            with patch('tools.tpm_runtime_probe.time.perf_counter_ns', side_effect=[0, elapsed]):
                probe.gc_callback('start', {'generation': generation})
                probe.gc_callback('stop', {'generation': generation})
        snapshot = probe.snapshot()
        self.assertEqual(snapshot['retained_events'], 2)
        self.assertEqual(snapshot['dropped_events'], 1)
        self.assertEqual([event['generation'] for event in snapshot['events']], [2, 0])
        self.assertEqual(snapshot['statistics']['gc.generation_0']['count'], 2)
        self.assertEqual(snapshot['statistics']['gc.generation_0']['slow_count'], 1)

    def test_formal_origin_records_only_numeric_clocks_once_even_for_short_snapshots(self):
        secret = 'NEVER_RECORD_PAYLOAD_OR_CONFIGURATION'
        run = SimpleNamespace(formal_started_at=None, _formal_origin=None,
                              id=secret, config={'api_key': secret})
        probe = RuntimeProbe()
        measured = probe.wrap('TpmRun.snap', lambda instance, **kwargs: kwargs,
                              capture_formal_origin=True)
        measured(run, secret=secret)
        self.assertEqual(probe.snapshot()['formal_time_origins'], [])
        run.formal_started_at, run._formal_origin = 1792136219.594569, 322993.5567
        for _ in range(3):
            with patch('tools.tpm_runtime_probe.time.perf_counter_ns', side_effect=[0, 1_000]):
                self.assertEqual(measured(run, secret=secret), {'secret': secret})
        snapshot = probe.snapshot()
        self.assertEqual(snapshot['formal_time_origins'], [
            {'formal_started_at': 1792136219.594569, 'formal_origin_monotonic': 322993.5567}])
        self.assertNotIn(secret, json.dumps(snapshot))
        self.assertEqual(snapshot['events'], [])
        self.assertFalse(snapshot['formal_origin_limit_reached'])
        snapshot['formal_time_origins'][0]['formal_started_at'] = 0
        self.assertEqual(probe.snapshot()['formal_time_origins'][0]['formal_started_at'], 1792136219.594569)

    def test_gc_collection_counts_keep_unknown_distinct_and_do_not_collect(self):
        probe = RuntimeProbe()
        with patch('tools.tpm_runtime_probe.gc.collect', side_effect=AssertionError('must not collect')), \
             patch('tools.tpm_runtime_probe.time.perf_counter_ns', side_effect=[0, 3_000_000]), \
             patch('tools.tpm_runtime_probe.sys.getallocatedblocks', side_effect=[1000, 700]):
            probe.gc_callback('start', {'generation': 2})
            probe.gc_callback('stop', {'generation': 2, 'collected': 11, 'uncollectable': 0})
        value = probe.snapshot()
        self.assertEqual(value['gc_totals']['2'], {'collections': 1, 'collected': 11,
                                                  'uncollectable': 0, 'complete_samples': 1})
        self.assertEqual(value['events'][0]['allocated_blocks_before'], 1000)
        self.assertEqual(value['events'][0]['allocated_blocks_after'], 700)
        with patch('tools.tpm_runtime_probe.time.perf_counter_ns', side_effect=[0, 3_000_000]):
            probe.gc_callback('start', {'generation': 1})
            probe.gc_callback('stop', {'generation': 1})
        value = probe.snapshot()
        self.assertEqual(value['gc_totals']['1']['complete_samples'], 0)
        self.assertIsNone(value['events'][1]['collected'])
        self.assertIsNone(value['events'][1]['allocated_blocks_before'])
        value['gc_totals']['2']['collected'] = -1
        self.assertEqual(probe.snapshot()['gc_totals']['2']['collected'], 11)

    def test_formal_origins_reject_invalid_types_and_stay_bounded(self):
        probe = RuntimeProbe()
        for invalid in (None, True, False, 'secret', {}, float('nan'), float('inf'), -1):
            probe._capture_formal_origin(SimpleNamespace(formal_started_at=invalid, _formal_origin=1))
            probe._capture_formal_origin(SimpleNamespace(formal_started_at=1, _formal_origin=invalid))
        self.assertEqual(probe.snapshot()['formal_time_origins'], [])
        for index in range(FORMAL_ORIGIN_LIMIT + 3):
            run = SimpleNamespace(formal_started_at=1792136000 + index, _formal_origin=322000 + index)
            probe._capture_formal_origin(run)
            probe._capture_formal_origin(run)
        snapshot = probe.snapshot()
        self.assertEqual(len(snapshot['formal_time_origins']), FORMAL_ORIGIN_LIMIT)
        self.assertEqual(snapshot['formal_origin_limit'], FORMAL_ORIGIN_LIMIT)
        self.assertTrue(snapshot['formal_origin_limit_reached'])

    def test_generic_wrapper_does_not_inspect_arguments_for_origins(self):
        class DoNotInspect:
            @property
            def formal_started_at(self):
                raise AssertionError('generic wrapper must not inspect arguments')
        value = DoNotInspect()
        probe = RuntimeProbe()
        self.assertIs(probe.wrap('Handler._json', lambda data: data)(value), value)
        self.assertEqual(probe.snapshot()['formal_time_origins'], [])

    def test_frequent_fast_boundaries_do_not_acquire_lock_or_allocate_statistics(self):
        probe = RuntimeProbe()
        class ForbiddenLock:
            def __enter__(self):
                raise AssertionError('fast micro-boundary acquired lock')
        original_lock = probe._lock
        probe._lock = ForbiddenLock()
        wrapped = probe.wrap('CompletedRecords.append', lambda value: value, slow_only=True)
        try:
            with patch('tools.tpm_runtime_probe.time.perf_counter_ns', side_effect=[0, 1000]):
                self.assertEqual(wrapped('secret'), 'secret')
        finally:
            probe._lock = original_lock
        snapshot = probe.snapshot()
        self.assertEqual(snapshot['events'], [])
        self.assertEqual(snapshot['statistics'], {})
        self.assertIn('CompletedRecords.append', snapshot['slow_only_labels'])

    def test_sleep_records_numeric_anchors_and_splits_callback_from_resume(self):
        probe = RuntimeProbe()
        secret = 'NO_PAYLOAD_OR_RETURN_VALUE_IN_PROBE'
        loop = SimpleNamespace(time=Mock(side_effect=[10., 10.015, 10.017]))
        timeout = SimpleNamespace(when=lambda: 10.01)
        callback = probe.wrap_timeout_callback(lambda instance: secret)
        async def original(instance, seconds):
            self.assertEqual(callback(timeout), secret)
            return secret
        async def invoke():
            with patch('tools.tpm_runtime_probe.asyncio.get_running_loop', return_value=loop), \
                 patch('tools.tpm_runtime_probe.time.monotonic', side_effect=[20., 20.017]), \
                 patch('tools.tpm_runtime_probe.time.perf_counter_ns', side_effect=[0, 17_000_000]):
                self.assertEqual(await probe.wrap_sleep(original)(object(), .01), secret)
        asyncio.run(invoke())
        snapshot = probe.snapshot()
        self.assertNotIn(secret, json.dumps(snapshot))
        event = snapshot['events'][0]
        self.assertEqual(event['requested_seconds'], .01)
        self.assertAlmostEqual(event['loop_overrun_ms'], 7)
        self.assertAlmostEqual(event['timer_callback_overrun_ms'], 5)
        self.assertAlmostEqual(event['timer_to_resume_ms'], 2)
        self.assertEqual(event['started_time_monotonic'], 20)
        self.assertEqual(event['timer_deadline_loop_time'], 10.01)
        self.assertIsNone(probe._sleep_scope.get())
        self.assertEqual(snapshot['loop_delay_histograms']['TpmRun._sleep.callback_to_resume']['count'], 1)

    def test_moderate_sleep_delay_updates_histogram_without_retaining_event(self):
        probe = RuntimeProbe()
        loop = SimpleNamespace(time=Mock(side_effect=[10., 10.013]))
        async def original(instance, seconds):
            return 1
        async def invoke():
            with patch('tools.tpm_runtime_probe.asyncio.get_running_loop', return_value=loop), \
                 patch('tools.tpm_runtime_probe.time.monotonic', side_effect=[20., 20.013]):
                self.assertEqual(await probe.wrap_sleep(original)(object(), .01), 1)
        asyncio.run(invoke())
        snapshot = probe.snapshot()
        self.assertEqual(snapshot['events'], [])
        self.assertEqual(snapshot['loop_delay_histograms']['TpmRun._sleep.overrun']['count'], 1)
        self.assertEqual(snapshot['statistics'], {})

    def test_sleep_context_is_reset_and_cancellation_preserved(self):
        probe = RuntimeProbe()
        callback = probe.wrap_timeout_callback(lambda value: value)
        sentinel = object()
        self.assertIs(callback(sentinel), sentinel)  # unrelated callback has no inspected deadline
        async def cancelled(instance, seconds):
            self.assertIsNotNone(probe._sleep_scope.get())
            raise asyncio.CancelledError
        async def invoke():
            with self.assertRaises(asyncio.CancelledError):
                await probe.wrap_sleep(cancelled)(sentinel, .01)
            self.assertIsNone(probe._sleep_scope.get())
        asyncio.run(invoke())
        self.assertEqual(probe.snapshot()['events'], [])

    def test_arrival_records_only_formal_numeric_due_and_uses_separate_event_threshold(self):
        probe = RuntimeProbe()
        secret = 'DO_NOT_RECORD_ARGUMENTS_OR_RESULT'
        async def original(*args, **kwargs):
            return secret
        entry = probe.wrap_arrival('TpmRun._request.entry', original, request_entry=True)
        resume = probe.wrap_arrival('TpmRun._wait_for_slot.resume', original)
        async def invoke():
            with patch('tools.tpm_runtime_probe.time.monotonic', return_value=10.003):
                self.assertEqual(await entry(object(), 'formal', 999, 10., secret), secret)
            with patch('tools.tpm_runtime_probe.time.monotonic', return_value=10.007):
                self.assertEqual(await resume(object(), 10., secret, payload=secret), secret)
            with patch('tools.tpm_runtime_probe.time.monotonic', side_effect=AssertionError('must not inspect')):
                await entry(object(), 'calibration', due=10., payload=secret)
                await entry(object(), 'formal', due=secret)
        asyncio.run(invoke())
        snapshot = probe.snapshot()
        self.assertEqual([item['label'] for item in snapshot['events']], ['TpmRun._wait_for_slot.resume'])
        self.assertAlmostEqual(snapshot['events'][0]['lateness_ms'], 7)
        self.assertEqual(snapshot['loop_delay_histograms']['TpmRun._request.entry']['count'], 1)
        self.assertEqual(snapshot['arrival_event_threshold_ms'], 5)
        self.assertNotIn(secret, json.dumps(snapshot))

    def test_delay_histograms_are_fixed_bounded_and_distinguish_overflow(self):
        probe = RuntimeProbe(event_limit=1)
        label = 'TpmRun._request.entry'
        for seconds in (-.001, 0, .00125, .00305, .010, 100, float('nan'), float('inf')):
            probe._observe_delay(label, seconds)
        histogram = probe.snapshot()['loop_delay_histograms'][label]
        self.assertEqual(histogram['count'], 6)
        self.assertEqual(len(histogram['bucket_counts']), 102)
        self.assertEqual(histogram['negative_count'], 1)
        self.assertEqual(histogram['overflow_count'], 2)
        self.assertEqual(histogram['p50_bounds'], {'lower_ms': 1.2, 'upper_ms': 1.3})
        self.assertEqual(histogram['p95_bounds'], {'lower_ms': 10., 'upper_ms': None})
        self.assertEqual(probe.snapshot()['events'], [])

    def test_selector_timeout_and_outcome_categories_preserve_original_result(self):
        probe = RuntimeProbe()
        target, other = object(), object()
        probe._selector = target
        result = []
        observed = []
        def original(selector, timeout):
            observed.append((selector, timeout))
            return result
        measured = probe.wrap_selector(original)
        for timeout, elapsed_ns, outcome in ((None, 1000, 'empty_unbounded'), (0, 1000, 'empty_expired'),
                                            (.01, 9_000_000, 'empty_early'), (.01, 11_000_000, 'empty_expired')):
            with patch('tools.tpm_runtime_probe.time.perf_counter_ns', side_effect=[0, elapsed_ns]):
                self.assertIs(measured(target, timeout), result)
            kind = 'none' if timeout is None else 'zero' if timeout == 0 else 'positive'
            self.assertEqual(probe._selector_counts[kind][outcome], 1)
        result.append('NEVER_RECORD_READY_PAYLOAD')
        with patch('tools.tpm_runtime_probe.time.perf_counter_ns', side_effect=[0, 1000]):
            self.assertIs(measured(target, .01), result)
        self.assertEqual(probe._selector_counts['positive']['io_ready'], 1)
        with patch('tools.tpm_runtime_probe.time.perf_counter_ns', side_effect=AssertionError('other selector measured')):
            self.assertIs(measured(other, 123.), result)
        self.assertEqual(observed[-1], (other, 123.))
        self.assertNotIn('NEVER_RECORD_READY_PAYLOAD', json.dumps(probe.snapshot()))
        self.assertEqual(probe.snapshot()['loop_delay_histograms'][SELECTOR_HISTOGRAM_LABEL]['count'], 1)

    def test_selector_exceptions_and_unknown_result_are_not_changed_or_inspected(self):
        probe = RuntimeProbe()
        target = object()
        probe._selector = target
        class UnknownResult:
            def __len__(self):
                raise AssertionError('must not inspect custom result')
        value = UnknownResult()
        with patch('tools.tpm_runtime_probe.time.perf_counter_ns', side_effect=[0, 1000]):
            self.assertIs(probe.wrap_selector(lambda selector, timeout: value)(target, .01), value)
        self.assertEqual(probe._selector_counts['positive']['unknown_result'], 1)
        error = ValueError('NEVER_RECORD_EXCEPTION_DETAILS')
        def fail(selector, timeout):
            raise error
        with patch('tools.tpm_runtime_probe.time.perf_counter_ns', side_effect=[0, 1000]):
            with self.assertRaises(ValueError) as caught:
                probe.wrap_selector(fail)(target, .01)
        self.assertIs(caught.exception, error)
        self.assertEqual(probe._selector_counts['positive']['raised'], 1)
        self.assertNotIn('NEVER_RECORD_EXCEPTION_DETAILS', json.dumps(probe.snapshot()))

    def test_slow_empty_expired_selector_keeps_wait_and_overrun_separate(self):
        probe = RuntimeProbe()
        target = object()
        probe._selector = target
        probe._capture_formal_origin(SimpleNamespace(formal_started_at=1000., _formal_origin=20.))
        with patch('tools.tpm_runtime_probe.time.monotonic', return_value=25.), \
             patch('tools.tpm_runtime_probe.time.perf_counter_ns', side_effect=[0, 18_000_000, 18_000_000]):
            self.assertEqual(probe.wrap_selector(lambda selector, timeout: [])(target, .01), [])
        snapshot = probe.snapshot()
        event = snapshot['events'][0]
        self.assertEqual(event['interval_kind'], 'selector_wait')
        self.assertEqual(event['elapsed_ms'], 18)
        self.assertAlmostEqual(event['overrun_ms'], 8)
        self.assertEqual(event['requested_timeout_seconds'], .01)
        self.assertEqual(event['ready_count'], 0)
        bucket = snapshot['formal_delay_buckets']['buckets'][0]
        self.assertEqual(bucket['start_offset_seconds'], 0)
        self.assertEqual(bucket['histograms'][SELECTOR_HISTOGRAM_LABEL]['count'], 1)

    def test_selector_install_is_once_for_current_loop_and_close_restores_type(self):
        class Selector:
            def select(self, timeout=None):
                return []
        original = Selector.select
        probe = RuntimeProbe()
        target = Selector()
        try:
            probe._ensure_selector(SimpleNamespace(_selector=target))
            replacement = Selector.select
            self.assertIsNot(replacement, original)
            probe._ensure_selector(SimpleNamespace(_selector=Selector()))
            self.assertIs(Selector.select, replacement)
            with patch('tools.tpm_runtime_probe.time.perf_counter_ns', side_effect=[0, 1000]):
                self.assertEqual(target.select(0), [])
            with patch('tools.tpm_runtime_probe.time.perf_counter_ns', side_effect=AssertionError('other loop measured')):
                self.assertEqual(Selector().select(0), [])
            self.assertEqual(probe.snapshot()['selector']['status'], 'available')
        finally:
            probe.close()
        self.assertIs(Selector.select, original)

    def test_missing_or_unpatchable_selector_is_explicitly_unknown(self):
        probe = RuntimeProbe()
        probe._ensure_selector(SimpleNamespace())
        self.assertEqual(probe.snapshot()['selector']['status'], 'unknown')
        self.assertEqual(probe.snapshot()['selector']['reason'], 'loop_selector_unavailable')
        class Locked(type):
            def __setattr__(cls, name, value):
                raise TypeError('immutable')
        class Selector(metaclass=Locked):
            def select(self, timeout=None):
                return []
        probe = RuntimeProbe()
        probe._ensure_selector(SimpleNamespace(_selector=Selector()))
        self.assertEqual(probe.snapshot()['selector']['status'], 'unknown')
        self.assertEqual(probe.snapshot()['selector']['reason'], 'selector_type_not_patchable')

    def test_formal_delay_buckets_are_fixed_and_align_due_without_wall_clock(self):
        probe = RuntimeProbe()
        callback = 'TpmRun._sleep.callback_lateness'
        entry = 'TpmRun._request.entry'
        probe._observe_delay(callback, .001, due=99.)
        probe._capture_formal_origin(SimpleNamespace(formal_started_at=1000., _formal_origin=100.))
        probe._observe_delay(callback, .00125, due=109.999)
        probe._observe_delay(entry, .00305, due=110.)
        probe._observe_delay(SELECTOR_HISTOGRAM_LABEL, .002, due=119.999)
        probe._observe_delay(callback, .001, due=99.)
        probe._observe_delay(callback, .001, due=100. + FORMAL_BUCKET_LIMIT * 10)
        probe._observe_delay(callback, .001, due=None)
        result = probe.snapshot()['formal_delay_buckets']
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['not_bucketed'], {'unknown_origin': 1, 'conflicting_origin': 0,
                                                'before_formal': 1, 'after_limit': 1, 'invalid_due': 1})
        self.assertEqual([bucket['start_offset_seconds'] for bucket in result['buckets']], [0, 10])
        self.assertEqual(result['buckets'][0]['histograms'][callback]['p95_bounds'],
                         {'lower_ms': 1.2, 'upper_ms': 1.3})
        self.assertEqual(result['buckets'][1]['histograms'][entry]['count'], 1)
        self.assertEqual(result['buckets'][1]['histograms'][SELECTOR_HISTOGRAM_LABEL]['count'], 1)
        self.assertEqual(len(probe._formal_histograms), FORMAL_BUCKET_LIMIT)
        self.assertTrue(all(len(values) == 102 for bucket in probe._formal_histograms for values in bucket.values()))
        probe._capture_formal_origin(SimpleNamespace(formal_started_at=1001., _formal_origin=101.))
        probe._observe_delay(callback, .001, due=105.)
        self.assertEqual(probe.snapshot()['formal_delay_buckets']['status'], 'conflicting_origins')
        self.assertEqual(probe.snapshot()['formal_delay_buckets']['not_bucketed']['conflicting_origin'], 1)

    def test_detailed_install_patches_aliases_only_and_restores_async_boundaries(self):
        class Run:
            __module__ = 'probe_fixture_engine'
            def snap(self): return 1
            def _cache_protection(self): return 2
            async def _sleep(self, seconds): return 3
            async def _wait_for_slot(self, due, stage_end, **kwargs): return 4
            async def _request(self, phase, stage_index=None, due=None, group=0, **kwargs): return 5
        class Metrics:
            def update(self, records): return records
            def snapshot(self): return 6
        class Records:
            def append(self, record): return record
        class Store:
            def write(self, entries): return entries
        class Handler:
            def _json(self, data): return data
        shared_timing = lambda **kwargs: kwargs
        engine = SimpleNamespace(clone_snapshot=lambda value: value, request_timing=shared_timing,
                                 LiveMetrics=Metrics, CompletedRecords=Records)
        service = SimpleNamespace(plan_id=lambda value: value)
        app = SimpleNamespace(Handler=Handler, HistoryStore=Store, persist_history=lambda: True,
                              _history_safe_snapshot=lambda value: value)
        targets = [(Run, name) for name in ('snap', '_cache_protection', '_sleep', '_wait_for_slot', '_request')]
        targets += [(Metrics, 'update'), (Metrics, 'snapshot'), (Records, 'append'), (Store, 'write'),
                    (engine, 'clone_snapshot'), (engine, 'request_timing'), (service, 'plan_id'),
                    (asyncio.Timeout, '_on_timeout')]
        originals = [(owner, name, getattr(owner, name)) for owner, name in targets]
        history_filter = app._history_safe_snapshot
        probe = RuntimeProbe()
        with patch.dict('sys.modules', {'probe_fixture_engine': engine, 'tpm_service': service}):
            try:
                probe.install(app, Run)
                for owner, name, original in originals:
                    self.assertIsNot(getattr(owner, name), original)
                self.assertIs(app._history_safe_snapshot, history_filter)
                self.assertIsNot(engine.request_timing, shared_timing)
                self.assertEqual(shared_timing(raw='secret'), {'raw': 'secret'})
                self.assertEqual(asyncio.run(Run()._request('calibration')), 5)
                self.assertEqual(asyncio.run(Run()._sleep(0)), 3)
            finally:
                probe.close()
        for owner, name, original in originals:
            self.assertIs(getattr(owner, name), original)

    def test_install_and_close_restore_hooks_and_never_change_gc_configuration(self):
        class Run:
            formal_started_at = 1792136219.594569
            _formal_origin = 322993.5567
            def snap(self):
                return 1
        class Handler:
            def _json(self, data):
                return data
        app = SimpleNamespace(Handler=Handler, persist_history=lambda: True)
        original = (Run.snap, Handler._json, app.persist_history)
        callbacks = list(gc.callbacks)
        configuration = (gc.isenabled(), gc.get_threshold())
        probe = RuntimeProbe()
        try:
            probe.install(app, Run)
            self.assertIn(probe.gc_callback, gc.callbacks)
            self.assertEqual(Run().snap(), 1)
            self.assertEqual(probe.snapshot()['formal_time_origins'], [
                {'formal_started_at': 1792136219.594569, 'formal_origin_monotonic': 322993.5567}])
            self.assertEqual(Handler()._json({'synthetic': 1}), {'synthetic': 1})
            self.assertTrue(app.persist_history())
        finally:
            probe.close()
        self.assertEqual((Run.snap, Handler._json, app.persist_history), original)
        self.assertEqual(gc.callbacks, callbacks)
        self.assertEqual((gc.isenabled(), gc.get_threshold()), configuration)


if __name__ == '__main__':
    unittest.main()
