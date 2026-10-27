"""Opt-in timing-only probes for the isolated loopback TPM benchmark process."""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import functools
import gc
import json
import math
import os
from pathlib import Path
import sys
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_ROOT = ROOT / 'artifacts' / 'tpm-performance'
EVENT_LIMIT = 4096
FORMAL_ORIGIN_LIMIT = 8
THRESHOLD_MS = 2
ARRIVAL_EVENT_THRESHOLD_MS = 5
HISTOGRAM_BIN_MS = .1
HISTOGRAM_MAX_MS = 10
FORMAL_BUCKET_SECONDS = 10
FORMAL_BUCKET_LIMIT = 36
SELECTOR_HISTOGRAM_LABEL = 'selector.positive_empty_expired.overrun'
_HISTOGRAM_LABELS = ('TpmRun._sleep.overrun', 'TpmRun._sleep.callback_lateness',
                     'TpmRun._sleep.callback_to_resume', 'TpmRun._wait_for_slot.resume',
                     'TpmRun._request.entry', SELECTOR_HISTOGRAM_LABEL)
_BUCKET_HISTOGRAM_LABELS = ('TpmRun._sleep.callback_lateness', 'TpmRun._sleep.callback_to_resume',
                          'TpmRun._request.entry', SELECTOR_HISTOGRAM_LABEL)
_SELECTOR_TIMEOUT_KINDS = ('none', 'zero', 'positive', 'unknown')
_SELECTOR_OUTCOMES = ('io_ready', 'empty_early', 'empty_expired', 'empty_unbounded',
                      'unknown_result', 'raised')


def validate_isolation(output, environ, *, benchmark_root=BENCHMARK_ROOT):
    """Check isolation before importing the application or opening any app data."""
    output = Path(output).resolve()
    root = Path(benchmark_root).resolve()
    if output.parent.parent != root or output.name != 'runtime-probe.json':
        raise ValueError('probe output must be in one isolated benchmark artifact directory')
    if not output.parent.is_dir() or output.exists():
        raise ValueError('probe requires a fresh benchmark output file')
    if environ.get('ROUTE_TEST_HOST') != '127.0.0.1':
        raise ValueError('probe requires the explicit loopback host')
    if environ.get('ROUTE_TEST_DISABLE_CREDENTIAL_STORE') != '1':
        raise ValueError('probe requires the credential store to be disabled')
    configured = environ.get('ROUTE_TEST_DATA_DIR')
    if not configured or Path(configured).resolve() != output.parent / 'data':
        raise ValueError('probe requires the isolated benchmark data directory')
    data = Path(configured)
    if data.exists() and (not data.is_dir() or any(data.iterdir())):
        raise ValueError('probe refuses existing application data')
    return output


def _timestamp(value):
    return datetime.fromtimestamp(value, timezone(timedelta(hours=8))).isoformat(timespec='milliseconds')


class RuntimeProbe:
    """Bounded timing evidence; only numeric due/sleep and phase are inspected."""

    def __init__(self, *, event_limit=EVENT_LIMIT, mode='full', sample_every=32):
        if event_limit < 1:
            raise ValueError('event limit must be positive')
        if mode not in {'full', 'sparse'}:
            raise ValueError('unsupported probe mode')
        if type(sample_every) is not int or sample_every < 1:
            raise ValueError('sample interval must be a positive integer')
        self.mode, self.sample_every = mode, sample_every
        self._sparse_loop_ready = False
        self._sparse_counts = {label: [0, 0] for label in ('sleep', 'request.entry', 'selector.select')}
        self.events = deque(maxlen=event_limit)
        self.stats = {}
        self.dropped_events = 0
        self.formal_time_origins = []
        self.formal_origin_limit_reached = False
        self._lock = threading.RLock()
        self._gc_starts = {}
        self._gc_totals = {generation: {'collections': 0, 'collected': 0,
                                      'uncollectable': 0, 'complete_samples': 0}
                           for generation in range(3)}
        self._patches = []
        self._installed = False
        self._slow_only_labels = set()
        self._sleep_scope = ContextVar('tpm_probe_sleep', default=None)
        # Only the isolated benchmark's event-loop thread writes these fixed
        # counters. They are exported after shutdown, without per-sample locks.
        self._histograms = {label: [0] * 102 for label in _HISTOGRAM_LABELS}
        self._bucket_origin = None
        self._bucket_origin_conflict = False
        self._formal_histograms = [{label: [0] * 102 for label in _BUCKET_HISTOGRAM_LABELS}
                                   for _ in range(FORMAL_BUCKET_LIMIT)]
        self._not_bucketed = {reason: 0 for reason in ('unknown_origin', 'conflicting_origin',
                                                      'before_formal', 'after_limit', 'invalid_due')}
        self._selector = None
        self._selector_info = {'status': 'unknown', 'reason': 'not_observed', 'type': None}
        self._selector_counts = {kind: {outcome: 0 for outcome in _SELECTOR_OUTCOMES}
                                 for kind in _SELECTOR_TIMEOUT_KINDS}
        self.started_at = time.time()
        self.gc_configuration = {'enabled': gc.isenabled(), 'thresholds': list(gc.get_threshold())}

    def _finish(self, label, start_ns, start_wall, *, generation=None, raised=False,
                slow_only=False, extra=None):
        end_ns = time.perf_counter_ns()
        duration_ns = end_ns - start_ns
        # Frequent micro-boundaries do not allocate statistics or acquire the
        # shared lock unless they actually cross the diagnostic threshold.
        if slow_only and duration_ns < THRESHOLD_MS * 1_000_000:
            return
        end_wall = time.time()
        if start_wall is None:
            start_wall = end_wall - duration_ns / 1_000_000_000
        with self._lock:
            stats = self.stats.get(label)
            if stats is None:
                stats = self.stats[label] = {'count': 0, 'total_ns': 0, 'max_ns': 0, 'slow_count': 0}
            stats['count'] += 1
            stats['total_ns'] += duration_ns
            stats['max_ns'] = max(stats['max_ns'], duration_ns)
            if duration_ns < THRESHOLD_MS * 1_000_000:
                return
            stats['slow_count'] += 1
            if len(self.events) == self.events.maxlen:
                self.dropped_events += 1
            event = {'label': label, 'started_wall': start_wall, 'ended_wall': end_wall,
                                'started_monotonic_ns': start_ns, 'ended_monotonic_ns': end_ns,
                                'duration_ms': duration_ns / 1_000_000,
                                'generation': generation, 'raised': raised,
                                'thread_id': threading.get_ident()}
            if extra is not None:
                event.update(extra)
            self.events.append(event)

    def _capture_formal_origin(self, run):
        """Read only two engine clock values, once per distinct frozen origin.

        This optional diagnostic never records request IDs, arguments, payloads,
        configuration, or the snapshot's return value. The bounded origin list
        is independent of thresholded events, so short snapshots cannot hide it.
        """
        try:
            wall = getattr(run, 'formal_started_at', None)
            monotonic = getattr(run, '_formal_origin', None)
        except Exception:
            return
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                   and math.isfinite(value) and value >= 0 for value in (wall, monotonic)):
            return
        with self._lock:
            if self._bucket_origin is None:
                self._bucket_origin = monotonic
            elif self._bucket_origin != monotonic:
                self._bucket_origin_conflict = True
            for item in self.formal_time_origins:
                if item['formal_started_at'] == wall and item['formal_origin_monotonic'] == monotonic:
                    return
            if len(self.formal_time_origins) == FORMAL_ORIGIN_LIMIT:
                self.formal_origin_limit_reached = True
                return
            self.formal_time_origins.append({'formal_started_at': wall, 'formal_origin_monotonic': monotonic})

    def wrap(self, label, function, *, capture_formal_origin=False, slow_only=False):
        if slow_only:
            self._slow_only_labels.add(label)
        @functools.wraps(function)
        def measured(*args, **kwargs):
            start_ns = time.perf_counter_ns()
            start_wall = None if slow_only else time.time()
            raised = False
            try:
                if capture_formal_origin and args:
                    self._capture_formal_origin(args[0])
                return function(*args, **kwargs)
            except BaseException:
                raised = True
                raise
            finally:
                self._finish(label, start_ns, start_wall, raised=raised, slow_only=slow_only)
        return measured

    def wrap_sleep(self, function):
        """Inspect only the numeric requested sleep, never a run or its config."""
        self._slow_only_labels.add('TpmRun._sleep.overrun')
        @functools.wraps(function)
        async def measured(instance, seconds):
            if (not isinstance(seconds, (int, float)) or isinstance(seconds, bool)
                    or not math.isfinite(seconds) or seconds <= 0):
                return await function(instance, seconds)
            loop = None
            if self.mode == 'sparse':
                if not self._sparse_loop_ready:
                    loop = asyncio.get_running_loop()
                    self._capture_formal_origin(instance)
                    if self._installed:
                        self._ensure_selector(loop)
                    self._sparse_loop_ready = True
                if not self._sample_sparse('sleep'):
                    return await function(instance, seconds)
            loop = loop or asyncio.get_running_loop()
            if self._bucket_origin is None:
                self._capture_formal_origin(instance)
            if self._installed:
                self._ensure_selector(loop)
            # One tiny marker is shared with this wait's existing timeout
            # callback through its captured context. No task/timer is added.
            marker = [loop, None, None]
            token = self._sleep_scope.set(marker)
            start_ns = time.perf_counter_ns()
            start_wall, start_mono, start_loop = time.time(), time.monotonic(), loop.time()
            raised = False
            try:
                return await function(instance, seconds)
            except BaseException:
                raised = True
                raise
            finally:
                end_loop, end_mono = loop.time(), time.monotonic()
                self._sleep_scope.reset(token)
                elapsed = end_loop - start_loop
                overrun = elapsed - seconds
                self._observe_delay('TpmRun._sleep.overrun', overrun)
                if marker[2] is not None:
                    self._observe_delay('TpmRun._sleep.callback_lateness', marker[2] - marker[1], due=marker[1])
                    self._observe_delay('TpmRun._sleep.callback_to_resume', end_loop - marker[2], due=marker[1])
                if overrun >= ARRIVAL_EVENT_THRESHOLD_MS / 1000:
                    self._finish('TpmRun._sleep.overrun', start_ns, start_wall,
                                 raised=raised, slow_only=True,
                                 extra={'requested_seconds': seconds,
                                        'loop_elapsed_seconds': elapsed,
                                        'loop_overrun_ms': overrun * 1000,
                                        'started_loop_time': start_loop, 'ended_loop_time': end_loop,
                                        'started_time_monotonic': start_mono,
                                        'ended_time_monotonic': end_mono,
                                        'requested_loop_deadline': start_loop + seconds,
                                        'timer_deadline_loop_time': marker[1],
                                        'timer_callback_loop_time': marker[2],
                                        'timer_callback_overrun_ms': ((marker[2] - marker[1]) * 1000
                                                                       if marker[2] is not None else None),
                                        'timer_to_resume_ms': ((end_loop - marker[2]) * 1000
                                                               if marker[2] is not None else None)})
        return measured

    def _ensure_selector(self, loop):
        if self._selector_info['reason'] != 'not_observed':
            return
        selector = getattr(loop, '_selector', None)
        owner = type(selector)
        original = getattr(owner, 'select', None)
        if selector is None or not callable(original):
            self._selector_info.update(reason='loop_selector_unavailable')
            return
        replacement = self.wrap_selector(original)
        try:
            setattr(owner, 'select', replacement)
        except (TypeError, AttributeError) as exc:
            self._selector_info.update(reason='selector_type_not_patchable', error_type=type(exc).__name__)
            return
        self._selector = selector
        self._selector_info.update(status='available', reason='instrumented',
                                   type=owner.__module__ + '.' + owner.__qualname__)
        self._patches.append((owner, 'select', original, replacement))

    def wrap_selector(self, function):
        """Observe this loop only; never inspect ready keys or alter timeout."""
        self._slow_only_labels.add(SELECTOR_HISTOGRAM_LABEL)
        @functools.wraps(function)
        def measured(selector, timeout=None):
            if selector is not self._selector:
                return function(selector, timeout)
            if self.mode == 'sparse' and not self._sample_sparse('selector.select'):
                return function(selector, timeout)
            kind = ('none' if timeout is None else 'zero' if type(timeout) in (int, float) and timeout == 0
                    else 'positive' if type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0
                    else 'unknown')
            start_mono, start_ns = time.monotonic(), time.perf_counter_ns()
            result, raised = None, True
            try:
                result = function(selector, timeout)
                raised = False
                return result
            finally:
                elapsed = (time.perf_counter_ns() - start_ns) / 1e9
                ready = len(result) if not raised and type(result) in (list, tuple) else None
                outcome = ('raised' if raised else 'unknown_result' if ready is None else 'io_ready' if ready
                           else 'empty_unbounded' if kind in {'none', 'unknown'}
                           else 'empty_expired' if elapsed >= timeout else 'empty_early')
                self._selector_counts[kind][outcome] += 1
                if kind == 'positive' and outcome == 'empty_expired':
                    overrun = elapsed - timeout
                    self._observe_delay(SELECTOR_HISTOGRAM_LABEL, overrun, due=start_mono)
                    if overrun >= ARRIVAL_EVENT_THRESHOLD_MS / 1000:
                        self._finish(SELECTOR_HISTOGRAM_LABEL, start_ns, None, slow_only=True,
                                     extra={'requested_timeout_seconds': timeout, 'elapsed_ms': elapsed * 1000,
                                            'ready_count': ready, 'overrun_ms': overrun * 1000,
                                            'selector_call_started_monotonic': start_mono,
                                            'interval_kind': 'selector_wait'})
        return measured

    def wrap_timeout_callback(self, function):
        @functools.wraps(function)
        def measured(timeout):
            marker = self._sleep_scope.get()
            if marker is not None:
                deadline = timeout.when()
                if isinstance(deadline, (int, float)) and not isinstance(deadline, bool) and math.isfinite(deadline):
                    marker[1], marker[2] = deadline, marker[0].time()
            return function(timeout)
        return measured

    def _record_lateness(self, label, due, *, raised=False):
        if (not isinstance(due, (int, float)) or isinstance(due, bool)
                or not math.isfinite(due) or due < 0):
            return
        observed = time.monotonic()
        lateness = observed - due
        self._observe_delay(label, lateness, due=due)
        if lateness < ARRIVAL_EVENT_THRESHOLD_MS / 1000:
            return
        # This interval represents observed lateness, not function runtime.
        # The exact due/observed monotonic values remain alongside the clocks.
        end_ns, end_wall = time.perf_counter_ns(), time.time()
        self._finish(label, end_ns - round(lateness * 1_000_000_000), end_wall - lateness,
                     raised=raised, slow_only=True,
                     extra={'due_monotonic': due, 'observed_monotonic': observed,
                            'lateness_ms': lateness * 1000, 'interval_kind': 'lateness'})

    def wrap_arrival(self, label, function, *, request_entry=False):
        """Read only the due clock and the explicit formal-phase selector."""
        self._slow_only_labels.add(label)
        @functools.wraps(function)
        async def measured(*args, **kwargs):
            if request_entry:
                phase = args[1] if len(args) > 1 else kwargs.get('phase')
                if phase == 'formal':
                    if self.mode == 'sparse' and not self._sample_sparse('request.entry'):
                        return await function(*args, **kwargs)
                    due = args[3] if len(args) > 3 else kwargs.get('due')
                    self._record_lateness(label, due)
                return await function(*args, **kwargs)
            due = args[1] if len(args) > 1 else kwargs.get('due')
            raised = False
            try:
                return await function(*args, **kwargs)
            except BaseException:
                raised = True
                raise
            finally:
                self._record_lateness(label, due, raised=raised)
        return measured

    def _sample_sparse(self, label):
        counters = self._sparse_counts[label]
        counters[0] += 1
        if counters[0] % self.sample_every:
            return False
        counters[1] += 1
        return True

    def _sampling_snapshot(self):
        return {'mode': self.mode, 'sample_every': self.sample_every if self.mode == 'sparse' else 1,
                'counts_scope': 'eligible_calls' if self.mode == 'sparse' else 'not_counted_in_full_mode',
                'boundaries': {label: {'seen': seen, 'sampled': sampled, 'skipped': seen - sampled,
                                        'sample_fraction': sampled / seen if seen else None}
                               for label, (seen, sampled) in self._sparse_counts.items()}
                              if self.mode == 'sparse' else {},
                'gc_scope': 'all_callbacks',
                'histogram_scope': 'sampled_calls_only' if self.mode == 'sparse' else 'all_observed_calls',
                'note': '稀疏模式每第N次符合条件调用采样；分位数仅代表抽样，不代表所有调用。未抽样调用无时钟读数、无上下文标记。'}

    def _observe_delay(self, label, seconds, *, due=None):
        if math.isfinite(seconds):
            index = 0 if seconds < 0 else min(101, int(seconds * 10_000) + 1)
            self._histograms[label][index] += 1
            if label in _BUCKET_HISTOGRAM_LABELS:
                if self._bucket_origin is None:
                    reason = 'unknown_origin'
                elif self._bucket_origin_conflict:
                    reason = 'conflicting_origin'
                elif not isinstance(due, (int, float)) or isinstance(due, bool) or not math.isfinite(due):
                    reason = 'invalid_due'
                else:
                    bucket = math.floor((due - self._bucket_origin) / FORMAL_BUCKET_SECONDS)
                    reason = 'before_formal' if bucket < 0 else 'after_limit' if bucket >= FORMAL_BUCKET_LIMIT else None
                if reason is None:
                    self._formal_histograms[bucket][label][index] += 1
                else:
                    self._not_bucketed[reason] += 1

    def _histogram_snapshot(self, histograms=None):
        def bounds(index):
            return {'lower_ms': None if index == 0 else (index - 1) / 10,
                    'upper_ms': None if index == 101 else index / 10}
        result = {}
        for label, counters in (self._histograms if histograms is None else histograms).items():
            buckets = list(counters)
            count = sum(buckets)
            item = {'count': count, 'bin_width_ms': HISTOGRAM_BIN_MS,
                    'max_finite_bin_ms': HISTOGRAM_MAX_MS, 'bucket_counts': buckets,
                    'negative_count': buckets[0], 'overflow_count': buckets[-1]}
            for name, fraction in (('p50_bounds', .5), ('p95_bounds', .95)):
                rank, consumed = math.ceil(count * fraction), 0
                item[name] = None
                for index, amount in enumerate(buckets):
                    consumed += amount
                    if count and consumed >= rank:
                        item[name] = bounds(index)
                        break
            result[label] = item
        return result

    def _formal_histogram_snapshot(self):
        return {'status': ('unknown' if self._bucket_origin is None else 'conflicting_origins'
                           if self._bucket_origin_conflict else 'available'),
                'formal_origin_monotonic': self._bucket_origin,
                'bucket_seconds': FORMAL_BUCKET_SECONDS, 'bucket_limit': FORMAL_BUCKET_LIMIT,
                'sample_scope': 'sampled_calls_only' if self.mode == 'sparse' else 'all_observed_calls',
                'not_bucketed': dict(self._not_bucketed),
                'alignment': 'callback与arrival按计划due，selector按调用开始；不使用墙钟分桶。',
                'buckets': [{'start_offset_seconds': index * FORMAL_BUCKET_SECONDS,
                             'end_offset_seconds': (index + 1) * FORMAL_BUCKET_SECONDS,
                             'histograms': self._histogram_snapshot(histograms)}
                            for index, histograms in enumerate(self._formal_histograms)
                            if any(any(values) for values in histograms.values())]}

    def gc_callback(self, phase, info):
        generation = info.get('generation')
        if generation not in (0, 1, 2):
            return
        key = (threading.get_ident(), generation)
        if phase == 'start':
            with self._lock:
                self._gc_starts[key] = (time.perf_counter_ns(), time.time(),
                                       sys.getallocatedblocks() if generation == 2 else None)
        elif phase == 'stop':
            with self._lock:
                started = self._gc_starts.pop(key, None)
                totals = self._gc_totals[generation]
                totals['collections'] += 1
                counts = {name: info.get(name) for name in ('collected', 'uncollectable')}
                if all(type(value) is int and value >= 0 for value in counts.values()):
                    totals['complete_samples'] += 1
                    for name, value in counts.items():
                        totals[name] += value
            if started is not None:
                start_ns, start_wall, blocks = started
                self._finish('gc.generation_' + str(generation), start_ns, start_wall, generation=generation,
                             extra={'collected': counts['collected'], 'uncollectable': counts['uncollectable'],
                                    'allocated_blocks_before': blocks,
                                    'allocated_blocks_after': sys.getallocatedblocks() if generation == 2 else None})

    def install(self, app, run_class):
        if self._installed:
            raise RuntimeError('probe already installed')
        boundaries = [] if self.mode == 'sparse' else [
            (run_class, 'snap', 'TpmRun.snap'),
            (app, 'persist_history', 'persist_history'),
            (app.Handler, '_json', 'Handler._json'),
        ]
        # Patch only already-loaded aliases. Importing the app/data or broadly
        # instrumenting the shared timing module would violate isolation.
        engine = sys.modules.get(run_class.__module__)
        optional = [
            (engine, 'clone_snapshot', 'tpm_engine.clone_snapshot'),
            (engine, 'request_timing', 'tpm_engine.request_timing'),
            (getattr(engine, 'LiveMetrics', None), 'update', 'LiveMetrics.update'),
            (getattr(engine, 'LiveMetrics', None), 'snapshot', 'LiveMetrics.snapshot'),
            (getattr(engine, 'CompletedRecords', None), 'append', 'CompletedRecords.append'),
            (sys.modules.get('tpm_service'), 'plan_id', 'tpm_service.plan_id'),
            (getattr(app, 'HistoryStore', None), 'write', 'HistoryStore.write'),
            (run_class, '_cache_protection', 'TpmRun._cache_protection'),
            (run_class, '_finish', 'TpmRun._finish'),
            (run_class, '_build_report', 'TpmRun._build_report'),
        ]
        if self.mode != 'sparse':
            boundaries.extend((owner, name, label) for owner, name, label in optional
                              if owner is not None and callable(getattr(owner, name, None)))
        try:
            for owner, name, label in boundaries:
                original = getattr(owner, name)
                replacement = self.wrap(
                    label, original, capture_formal_origin=owner is run_class and name == 'snap',
                    slow_only=label not in {'TpmRun.snap', 'persist_history', 'Handler._json'})
                setattr(owner, name, replacement)
                self._patches.append((owner, name, original, replacement))
            if callable(getattr(run_class, '_sleep', None)):
                original = run_class._sleep
                replacement = self.wrap_sleep(original)
                run_class._sleep = replacement
                self._patches.append((run_class, '_sleep', original, replacement))
                original = asyncio.Timeout._on_timeout
                replacement = self.wrap_timeout_callback(original)
                asyncio.Timeout._on_timeout = replacement
                self._patches.append((asyncio.Timeout, '_on_timeout', original, replacement))
            for name, label, request_entry in (
                ('_wait_for_slot', 'TpmRun._wait_for_slot.resume', False),
                ('_request', 'TpmRun._request.entry', True),
            ):
                if self.mode == 'sparse' and not request_entry:
                    continue
                original = getattr(run_class, name, None)
                if callable(original):
                    replacement = self.wrap_arrival(label, original, request_entry=request_entry)
                    setattr(run_class, name, replacement)
                    self._patches.append((run_class, name, original, replacement))
        except BaseException:
            self.close()
            raise
        gc.callbacks.append(self.gc_callback)
        self._installed = True

    def close(self):
        if self.gc_callback in gc.callbacks:
            gc.callbacks.remove(self.gc_callback)
        for owner, name, original, replacement in reversed(self._patches):
            if getattr(owner, name) is replacement:
                setattr(owner, name, original)
        self._patches.clear()
        self._installed = False

    def snapshot(self):
        with self._lock:
            events = [dict(event) for event in self.events]
            stats = {label: {'count': item['count'], 'total_ms': item['total_ns'] / 1_000_000,
                             'max_ms': item['max_ns'] / 1_000_000, 'slow_count': item['slow_count']}
                     for label, item in self.stats.items()}
            dropped = self.dropped_events
            origins = [dict(item) for item in self.formal_time_origins]
            origins_limited = self.formal_origin_limit_reached
            gc_totals = {str(generation): dict(values) for generation, values in self._gc_totals.items()}
        # Date formatting and JSON serialization occur only after the server exits.
        for event in events:
            event['started_at'] = _timestamp(event.pop('started_wall'))
            event['ended_at'] = _timestamp(event.pop('ended_wall'))
        return {'format': 1, 'complete': True, 'timezone': 'Asia/Singapore',
                'mode': self.mode, 'sampling': self._sampling_snapshot(),
                'started_at': _timestamp(self.started_at), 'ended_at': _timestamp(time.time()),
                'threshold_ms': THRESHOLD_MS, 'event_limit': self.events.maxlen,
                'arrival_event_threshold_ms': ARRIVAL_EVENT_THRESHOLD_MS,
                'retained_events': len(events), 'dropped_events': dropped, 'statistics': stats,
                'loop_delay_histograms': self._histogram_snapshot(),
                'formal_delay_buckets': self._formal_histogram_snapshot(),
                'selector': {**self._selector_info,
                             'count_scope': 'sampled_select_calls_only' if self.mode == 'sparse' else 'all_observed_select_calls',
                             'counts': {kind: dict(values) for kind, values in self._selector_counts.items()},
                             'note': '仅当前基准事件循环的select调用。耗时包含Python包装与线程/GIL交接，不能称为纯内核等待；不记录就绪描述符或数据。'},
                'histogram_note': ('固定0.1毫秒桶；首桶为负值、末桶为≥10毫秒，分位数仅给桶范围。单一本地基准事件循环写入，退出后导出。'
                                   + ('仅被抽样睡眠设置小上下文标记；确定性抽样可能有周期相位偏置，不能当无偏全量分布。' if self.mode == 'sparse'
                                      else '每次观测睡眠设置一个小上下文标记。') + '不新增任务或定时器。'),
                'slow_only_labels': sorted(self._slow_only_labels),
                'statistics_note': ('slow_only_labels（仅慢调用标签）的计数及总耗时只包含达到事件阈值的调用；睡眠与到达迟到仅保存至少 5 毫秒的事件，其他粗阶段为至少 2 毫秒。'
                                    + ('直方图仅记录抽样调用的有效时长；回收回调仍全量，不包装历史等粗阶段。' if self.mode == 'sparse'
                                       else '直方图记录全部观测调用的有效时长；未包装递归历史过滤，历史保存总耗时与写入耗时之差还包含摘要收集等处理。')
                                    + '迟到事件的区间表示迟到量。'),
                'formal_time_origins': origins, 'formal_origin_limit': FORMAL_ORIGIN_LIMIT,
                'formal_origin_limit_reached': origins_limited,
                'gc_configuration_at_start': self.gc_configuration,
                'gc_totals': gc_totals,
                'gc_configuration_at_end': {'enabled': gc.isenabled(), 'thresholds': list(gc.get_threshold())},
                'note': '仅记录本地合成验证的耗时和计数；探针本身有开销，相关性不等于因果。未修改垃圾回收设置或主动触发回收。',
                'events': events}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--mode', choices=('full', 'sparse'), default='full')
    parser.add_argument('--sample-every', type=int, default=32)
    args = parser.parse_args()
    if args.sample_every < 1:
        parser.error('sample interval must be positive')
    output = validate_isolation(args.output, os.environ)
    sys.path.insert(0, str(ROOT))
    import route_test_ui as app
    from tpm_engine import TpmRun
    probe = RuntimeProbe(mode=args.mode, sample_every=args.sample_every)
    probe.install(app, TpmRun)
    try:
        app.main()
    finally:
        probe.close()
        temporary = output.with_suffix('.tmp')
        temporary.write_text(json.dumps(probe.snapshot(), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(output)


if __name__ == '__main__':
    main()
