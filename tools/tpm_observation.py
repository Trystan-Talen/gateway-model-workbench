"""Small, phase-labelled evidence around the benchmark's existing ps samples.

This module does not schedule samples, inspect request bodies, or alter clocks.
All task context is taken from the preceding live response and can be stale
while ps is running. Missing counters and unavailable raw clocks remain unknown.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import time


SAMPLE_LIMIT = 2048
MARKER_LIMIT = 16


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _counter(value):
    return value if type(value) is int and value >= 0 else None


def _timestamp(nanoseconds):
    return datetime.fromtimestamp(nanoseconds / 1e9, timezone(timedelta(hours=8))).isoformat(timespec='milliseconds')


class ClockReader:
    """Paired reads with their own capture span; raw never aliases monotonic."""

    def __init__(self, clock=time):
        self.clock = clock
        self.raw_id = getattr(clock, 'CLOCK_MONOTONIC_RAW', None)
        self.raw_status = 'unknown'
        self.raw_error_type = None
        if self.raw_id is not None and callable(getattr(clock, 'clock_gettime_ns', None)):
            try:
                clock.clock_gettime_ns(self.raw_id)
            except (OSError, ValueError, AttributeError) as exc:
                self.raw_error_type = type(exc).__name__
            else:
                self.raw_status = 'available'

    def metadata(self):
        return {'status': self.raw_status,
                'source': 'time.clock_gettime_ns(CLOCK_MONOTONIC_RAW)' if self.raw_status == 'available' else None,
                'error_type': self.raw_error_type,
                'note': '未知不等于零；不以普通单调时钟冒充系统原始时钟。'}

    def point(self):
        begin = self.clock.perf_counter_ns()
        wall, monotonic = self.clock.time_ns(), self.clock.monotonic_ns()
        raw = None
        if self.raw_status == 'available':
            try:
                raw = self.clock.clock_gettime_ns(self.raw_id)
            except (OSError, ValueError, AttributeError) as exc:
                self.raw_status, self.raw_error_type = 'unknown', type(exc).__name__
        end = self.clock.perf_counter_ns()
        return {'wall_ns': wall, 'monotonic_ns': monotonic,
                'raw_monotonic_ns': raw, 'capture_begin_perf_ns': begin,
                'capture_end_perf_ns': end, 'capture_span_ns': end - begin}


def task_context(run):
    """Copy only explicit stage/count fields, never config or response data."""
    phase = run.get('phase')
    phase = phase if isinstance(phase, str) else None
    metrics = run.get('metrics') if isinstance(run.get('metrics'), dict) else {}
    stages = run.get('stages') if isinstance(run.get('stages'), list) else []
    latest = next((stage for stage in reversed(stages) if isinstance(stage, dict)), {})
    planned = (run.get('plan') or {}).get('stages', []) if isinstance(run.get('plan'), dict) else []
    targets = [stage.get('target_tpm') for stage in planned
               if isinstance(stage, dict) and _finite(stage.get('target_tpm'))]
    target = max(targets) if targets else None
    succeeded, failed = _counter(metrics.get('succeeded')), _counter(metrics.get('failed'))
    active_index = latest.get('index') if phase == 'running' and latest.get('ended_at') is None else None
    category = {'running': 'dispatching', 'draining': 'draining', 'finished': 'final_report',
                'interrupted': 'interrupted', 'calibrating': 'calibration',
                'awaiting_run': 'awaiting_confirmation', 'awaiting_calibration': 'awaiting_confirmation'}.get(phase, 'unknown')
    return {'source': 'preceding_live_response', 'phase': phase, 'category': category,
            'stage_index': _counter(active_index), 'latest_stage_index': _counter(latest.get('index')),
            'stage_target_tpm': latest.get('target_tpm') if _finite(latest.get('target_tpm')) else None,
            'is_target_stage': (latest.get('target_tpm') == target
                                if phase == 'running' and target is not None and active_index is not None else None),
            'formal_started': _counter(metrics.get('started')),
            'formal_completed': succeeded + failed if succeeded is not None and failed is not None else None,
            'completed_record_count': _counter(run.get('request_count')),
            'formal_in_flight': _counter(metrics.get('in_flight')),
            'all_in_flight': _counter(metrics.get('all_in_flight')),
            'formal_started_at': run.get('formal_started_at') if _finite(run.get('formal_started_at')) else None,
            'dispatch_ended_at': run.get('dispatch_ended_at') if _finite(run.get('dispatch_ended_at')) else None}


class ObservationRecorder:
    def __init__(self, *, clocks=None, sample_limit=SAMPLE_LIMIT):
        if type(sample_limit) is not int or sample_limit < 1:
            raise ValueError('sample limit must be positive')
        self.clocks = clocks or ClockReader()
        self.origin = self.clocks.point()
        self.samples, self.markers, self.failures = [], [], []
        self.sample_limit = sample_limit
        self.dropped_samples = self.dropped_markers = 0
        self.save_attempted = False

    def _point(self):
        point = self.clocks.point()
        point['at'] = _timestamp(point['wall_ns'])
        point['monotonic_offset_ns'] = point['monotonic_ns'] - self.origin['monotonic_ns']
        point['wall_elapsed_minus_monotonic_ns'] = ((point['wall_ns'] - self.origin['wall_ns'])
                                                  - point['monotonic_offset_ns'])
        origin_raw, raw = self.origin['raw_monotonic_ns'], point['raw_monotonic_ns']
        point['monotonic_elapsed_minus_raw_ns'] = (point['monotonic_offset_ns'] - (raw - origin_raw)
                                                  if raw is not None and origin_raw is not None else None)
        return point

    def _failure(self, operation, error_type):
        if len(self.failures) < 16:
            self.failures.append({'operation': operation, 'error_type': error_type})

    async def sample_rss(self, run, read_output):
        """Wrap exactly one pre-existing ps call; no retry or extra sample."""
        context = task_context(run)
        before = self._point()
        value, error_type = None, None
        try:
            output = await read_output()
            value = int(output.strip())
            if value < 0:
                raise ValueError('negative RSS')
        except Exception as exc:
            value = None
            error_type = type(exc).__name__
            self._failure('ps_rss', error_type)
        finally:
            after = self._point()
        sample = {'before': before, 'after': after, 'context': context, 'rss_kib': value,
                  'status': 'known' if error_type is None else 'unknown', 'error_type': error_type,
                  'read_duration_ms': (after['monotonic_ns'] - before['monotonic_ns']) / 1e6,
                  'read_wall_minus_monotonic_ns': ((after['wall_ns'] - before['wall_ns'])
                                                 - (after['monotonic_ns'] - before['monotonic_ns']))}
        if len(self.samples) < self.sample_limit:
            self.samples.append(sample)
        else:
            self.dropped_samples += 1
        return value if error_type is None else None

    def mark(self, label, run):
        if label not in {'export_before', 'export_after'}:
            raise ValueError('unsupported observation marker')
        if len(self.markers) >= MARKER_LIMIT:
            self.dropped_markers += 1
            return
        self.markers.append({'label': label, 'clock': self._point(), 'context': task_context(run)})

    def save(self, output, *, benchmark_complete):
        """Fail closed in metadata; a failed write never claims saved evidence."""
        self.save_attempted = True
        output = Path(output)
        complete = bool(benchmark_complete and self.samples and not self.failures
                        and not self.dropped_samples and not self.dropped_markers
                        and [item['label'] for item in self.markers] == ['export_before', 'export_after'])
        known = [item for item in self.samples if item['status'] == 'known']
        peaks = {}
        for sample in known:
            category = sample['context']['category']
            peaks[category] = max(peaks.get(category, 0), sample['rss_kib'])
        target = [item['rss_kib'] for item in known if item['context']['is_target_stage'] is True]
        metadata = {'path': str(output), 'complete': complete, 'sample_count': len(self.samples),
                    'known_rss_samples': len(known), 'dropped_samples': self.dropped_samples,
                    'marker_count': len(self.markers), 'raw_clock': self.clocks.metadata(),
                    'peak_rss_kib_by_category': peaks,
                    'target_peak_rss_kib': max(target) if target else None}
        document = {'format': 1, 'timezone': 'Asia/Singapore', **metadata,
                    'sample_period_seconds': 2, 'origin': self.origin,
                    'rss_scope': '原有poll之后每2秒至多一次ps；export标记不读取RSS，不混入原峰值。',
                    'context_note': '阶段与计数来自采样前最近一次live响应；ps期间可能变化，不把快照视为读取结束时的状态。缺失计数保持未知。',
                    'clock_note': '成对读数非原子同时，capture_span_ns标明每个锚点自身不确定范围；偏移仅是观测证据，不单独归因为系统校时。',
                    'failures': list(self.failures), 'dropped_markers': self.dropped_markers,
                    'samples': self.samples, 'markers': self.markers}
        temporary = output.with_suffix(output.suffix + '.tmp')
        try:
            temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            temporary.replace(output)
        except (OSError, ValueError, TypeError) as exc:
            metadata.update(complete=False, saved=False, save_error_type=type(exc).__name__)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        else:
            metadata['saved'] = True
        return metadata
