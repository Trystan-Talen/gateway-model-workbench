"""Finite full-workbench loopback TPM benchmark; never accepts a remote URL/key."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone, timedelta
import json
import math
import os
from pathlib import Path
import platform
import resource
import re
import socket
import statistics
import subprocess
import sys
import tempfile
import time

import httpx

if __package__:
    from .tpm_observation import ObservationRecorder
else:
    from tpm_observation import ObservationRecorder

ROOT = Path(__file__).resolve().parents[1]
# Freeze the existing acceptance policy independently from product defaults. A
# default change must not silently make this local performance check less strict.
FROZEN_CRITERIA = {
    'min_success_rate': .99, 'near_target_ratio': .95, 'max_missed_ratio': .01,
    'max_backlog_growth_ratio': .1, 'max_latency_degradation_ratio': 2.0,
}
# Evidence consistency tolerance, not a pressure/throughput acceptance tolerance.
# Task timestamps have millisecond precision; allow their rounding plus the two
# separate wall/monotonic clock reads, and fail closed when they disagree.
CLOCK_EVIDENCE_TOLERANCE_MS = 5


def _timestamp(value):
    return datetime.fromtimestamp(value, timezone(timedelta(hours=8))).isoformat(timespec='milliseconds')


def admission_diagnostics(run, records):
    """Check actual admission against immutable slots, including each stage end.

    Signed delays and remaining deadlines are captured at the same monotonic
    admission point. Do not infer early-start safety from the legacy clamped lag.
    The identity tolerance below only covers floating-point evidence arithmetic;
    neither early admission nor a zero/negative deadline margin is tolerated.
    """
    definitions = {stage['index']: stage for stage in run['plan']['stages']}
    actual = {stage['index']: stage for stage in run['stages']}
    formal = [row for row in records if row.get('phase') == 'formal']
    missing = early = after_end = inconsistent = 0

    def finite(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    for row in formal:
        stage, observed = definitions.get(row.get('stage_index')), actual.get(row.get('stage_index'))
        delay, margin = row.get('signed_schedule_delay_seconds'), row.get('stage_deadline_margin_seconds')
        scheduled = row.get('scheduled_at')
        if not stage or not observed or not all(finite(v) for v in (delay, margin, scheduled)):
            missing += 1
            continue
        early += delay < 0
        after_end += margin <= 0
        position = (scheduled - observed['started_at']) * stage['rps']
        slot = round(position)
        if not (0 <= slot < stage['planned_requests'] and abs(position - slot) <= .01):
            inconsistent += 1
            continue
        remaining = stage['duration_seconds'] - slot / stage['rps'] - delay
        # The identity tolerance cannot excuse a frozen-plan cutoff violation.
        after_end += margin > 0 and remaining <= 0
        if abs(remaining - margin) > .000001:
            inconsistent += 1
    complete = bool(formal) and missing == 0
    return {'formal_requests': len(formal), 'missing_evidence': missing,
            'early_starts': early, 'starts_at_or_after_stage_end': after_end,
            'inconsistent_evidence': inconsistent,
            'note': '同一单调准入时刻记录有符号延迟和档末余量；不容许提前或档末及之后准入。',
            'checks': {'stage_admission_evidence_complete': complete,
                       'no_early_starts': complete and early == 0,
                       'no_starts_at_or_after_stage_end': complete and after_end == 0,
                       'stage_admission_matches_frozen_plan': complete and inconsistent == 0}}


def scheduling_diagnostics(run, records, observer_samples=(), bucket_seconds=10):
    """Reconstruct absent scheduled slots; do not infer a cause from a time gap.

    Missing slots are matched to frozen planned time, not completion time or a
    counter's later sampling time. Actual starts are projected from the frozen
    scheduled offset plus the engine's measured monotonic scheduling delay.
    """
    formal = [row for row in records if row.get('phase') == 'formal']
    origin = run.get('formal_started_at')
    if origin is None:
        raise ValueError('formal_started_at is required for scheduling diagnostics')
    actual_stages = {stage['index']: stage for stage in run['stages']}
    rates = {stage['index']: stage['rps'] for stage in run['plan']['stages']}
    stages, buckets, missing_ranges = [], [], []
    unmapped = duplicates = 0
    offset = 0
    for definition in run['plan']['stages']:
        index = definition['index']
        actual = actual_stages.get(index, {})
        begin = actual.get('started_at', origin + offset)
        duration, rate, planned = (definition[key] for key in ('duration_seconds', 'rps', 'planned_requests'))
        rows = [row for row in formal if row.get('stage_index') == index]
        slots = {}
        for row in rows:
            scheduled = row.get('scheduled_at')
            position = (scheduled - begin) * rate if scheduled is not None else -1
            slot = round(position)
            # Epoch rounding is far below 1% of a slot at this benchmark's rate.
            if slot < 0 or slot >= planned or abs(position - slot) > .01:
                unmapped += 1
            elif slot in slots:
                duplicates += 1
            else:
                slots[slot] = row
        missing = [slot for slot in range(planned) if slot not in slots]
        groups = []
        for slot in missing:
            if groups and groups[-1][-1] + 1 == slot:
                groups[-1].append(slot)
            else:
                groups.append([slot])
        for group in groups:
            missing_ranges.append({
                'stage_index': index, 'first_slot': group[0], 'last_slot': group[-1],
                'count': len(group), 'offset_seconds': begin - origin + group[0] / rate,
                'scheduled_at': _timestamp(begin + group[0] / rate),
            })
        gaps = _start_gaps(rows, origin, rates)
        stages.append({
            'stage_index': index, 'target_tpm': definition['target_tpm'],
            'duration_seconds': duration, 'planned': planned, 'started': len(rows),
            'missed': len(missing), 'missed_ratio': len(missing) / planned if planned else None,
            'actual_rps': len(rows) / duration, 'max_start_gap_ms': max((gap['gap_ms'] for gap in gaps), default=None),
            'schedule_delay_p95_ms': _row_percentile(rows, 'schedule_delay_seconds', .95),
            'schedule_delay_max_ms': _row_percentile(rows, 'schedule_delay_seconds', 1),
            'pool_wait_p95_ms': _row_percentile(rows, 'pool_wait_seconds', .95),
        })
        for bucket in range(math.ceil(duration / bucket_seconds)):
            start = begin + bucket * bucket_seconds
            end = min(begin + duration, start + bucket_seconds)
            selected_slots = [slot for slot in range(planned) if start <= begin + slot / rate < end]
            scheduled_rows = [slots[slot] for slot in selected_slots if slot in slots]
            samples = [point for point in run.get('series', []) if start <= point['at'] < end]
            polls = [point for point in observer_samples if start <= point['at'] < end]
            buckets.append({
                'stage_index': index, 'start_offset_seconds': start - origin, 'end_offset_seconds': end - origin,
                'planned': len(selected_slots), 'started_for_scheduled_slots': len(scheduled_rows),
                'missed': len(selected_slots) - len(scheduled_rows),
                'actual_starts': sum(start - origin <= value < end - origin
                                     for row in rows if (value := _start_offset(row, origin)) is not None),
                'schedule_delay_p95_ms': _row_percentile(scheduled_rows, 'schedule_delay_seconds', .95),
                'schedule_delay_max_ms': _row_percentile(scheduled_rows, 'schedule_delay_seconds', 1),
                'sampled_peak_inflight': max((point.get('in_flight', 0) for point in samples), default=None),
                'observer_http_p95_ms': percentile([point['elapsed_ms'] for point in polls], .95),
                'observer_http_max_ms': max((point['elapsed_ms'] for point in polls), default=None),
            })
        offset += duration
    return {
        'source': '冻结计划与已落盘请求的 scheduled_at（计划发送时间）逐时隙比对；不把未发出归因为上游错误。',
        'time_zone': 'Asia/Singapore', 'bucket_seconds': bucket_seconds,
        'gap_note': '开始间隔与开始分桶使用冻结计划偏移加单调时钟调度延迟，按超出本档计划间隔排序；跨档间隔不比较。',
        'unmapped_requests': unmapped, 'duplicate_slots': duplicates,
        'reconstructed_missed': sum(stage['missed'] for stage in stages),
        'stages': stages, 'time_buckets': buckets,
        'missing_slot_ranges': missing_ranges,
        'engine_missed_reasons': run.get('scheduling'),
        'clock_evidence': clock_evidence(run, formal),
        'largest_start_gaps': sorted(_start_gaps(formal, origin, rates),
                                     key=lambda gap: gap['gap_over_interval_ms'], reverse=True)[:20],
    }


def _row_percentile(rows, field, quantile):
    value = percentile([row[field] for row in rows if row.get(field) is not None], quantile)
    return value * 1000 if value is not None else None


def _start_offset(row, origin):
    if row.get('scheduled_at') is None or row.get('schedule_delay_seconds') is None:
        return None
    return row['scheduled_at'] - origin + row['schedule_delay_seconds']


def _start_gaps(rows, origin, rates):
    ordered = sorted(((value, row) for row in rows if (value := _start_offset(row, origin)) is not None),
                     key=lambda pair: pair[0])
    return [{'gap_ms': (right_at - left_at) * 1000,
             'expected_interval_ms': 1000 / rates[left['stage_index']],
             'gap_over_interval_ms': (right_at - left_at - 1 / rates[left['stage_index']]) * 1000,
             'from_offset_seconds': left_at, 'to_offset_seconds': right_at,
             'from_request_id': left['id'], 'to_request_id': right['id'],
             'from_stage_index': left['stage_index'], 'to_stage_index': right['stage_index']}
            for (left_at, left), (right_at, right) in zip(ordered, ordered[1:])
            if left['stage_index'] == right['stage_index']]


def clock_evidence(run, formal):
    """Do not label clock disagreement as a late start or an engine defect."""
    origin = run['formal_started_at']
    offsets = [(row['started_at'] - origin - value) * 1000 for row in formal
               if (value := _start_offset(row, origin)) is not None]
    timing = run.get('timing', {})
    differences = {}
    for label, begin, end, duration in (
        ('task', 'started_at', 'ended_at', 'elapsed_ms'),
        ('drain', 'dispatch_ended_at', 'ended_at', 'drain_ms'),
    ):
        if timing.get(begin) and timing.get(end) and timing.get(duration) is not None:
            wall_ms = (datetime.fromisoformat(timing[end]) - datetime.fromisoformat(timing[begin])).total_seconds() * 1000
            differences[label + '_wall_minus_monotonic_ms'] = wall_ms - timing[duration]
    maximum = max((abs(value) for value in offsets), default=None)
    complete = len(offsets) == len(formal) and bool(formal) and len(differences) == 2
    consistent = complete and maximum <= CLOCK_EVIDENCE_TOLERANCE_MS and all(
        abs(value) <= CLOCK_EVIDENCE_TOLERANCE_MS for value in differences.values())
    return {'consistent': consistent, 'complete': complete,
            'tolerance_ms': CLOCK_EVIDENCE_TOLERANCE_MS,
            'request_max_abs_wall_offset_ms': maximum, **differences,
            'note': '墙钟与单调时间投影不一致时，停发后启动的墙钟核查记为不可确定并阻止验收通过；不将校时或证据缺失归因为违规启动。'}


def missed_reasons_reconcile(run):
    scheduling = run.get('scheduling') or {}
    total = scheduling.get('missed_by_reason')
    by_stage = scheduling.get('missed_by_stage')
    if not isinstance(total, dict) or not isinstance(by_stage, dict):
        return False
    reasons = set(total) | {reason for counts in by_stage.values() for reason in counts}
    return (all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in total.values())
            and all(isinstance(value, int) and not isinstance(value, bool) and value >= 0
                    for counts in by_stage.values() for value in counts.values())
            and sum(total.values()) == run['metrics']['missed']
            and all(total.get(reason, 0) == sum(counts.get(reason, 0) for counts in by_stage.values()) for reason in reasons))


def upstream_evidence_checks(received_body_sizes, records):
    """Compare independently observed loopback receipts with persisted evidence."""
    sizes = [row.get('body_bytes') for row in records]
    complete = all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in sizes)
    return {
        'upstream_request_count_reconciled': len(received_body_sizes) == len(records),
        'upstream_request_bytes_reconciled': complete and sum(received_body_sizes) == sum(sizes),
    }


def acceptance_diagnostics(run):
    """Enforce frozen acceptance independently of the legacy 5% rate check.

    Short diagnostic runs cannot establish a complete-minute throughput result;
    they still must satisfy usage, success, scheduling and stability requirements.
    """
    report = run['report']
    formal_ratio = report['missed'] / report['planned'] if report['planned'] else None
    target_ratio = report['target_missed'] / report['target_planned'] if report['target_planned'] else None
    criteria = run['plan']['criteria']
    preserved = criteria == FROZEN_CRITERIA and all(run['config'].get(key) == value for key, value in FROZEN_CRITERIA.items())
    limit = FROZEN_CRITERIA['max_missed_ratio']
    window = report.get('primary_window', {})
    full_window_required = run['config']['target_duration_seconds'] >= 60
    window_accepted = (
        window.get('valid') is True and report.get('verdict') in {'near', 'reached'}
        and window.get('ratio') is not None and window['ratio'] >= FROZEN_CRITERIA['near_target_ratio']
        and window.get('success_rate') is not None and window['success_rate'] >= FROZEN_CRITERIA['min_success_rate']
    )
    checks = {
        'frozen_criteria_preserved': preserved,
        'formal_missed_within_frozen_limit': formal_ratio is not None and formal_ratio <= limit,
        'target_missed_within_frozen_limit': target_ratio is not None and target_ratio <= limit,
        'target_success_rate_meets_frozen_minimum': report.get('target_success_rate') is not None
            and report['target_success_rate'] >= FROZEN_CRITERIA['min_success_rate'],
        'successful_usage_complete': report.get('successful_usage_complete_rate') == 1,
        'report_not_local_limited': report.get('local_limited') is False,
        'no_sustained_backlog': report.get('sustained_backlog') is False,
        'no_latency_degradation': report.get('latency_worsened') is False,
        'no_error_degradation': report.get('errors_worsened') is False,
        'target_window_meets_frozen_acceptance': window_accepted if full_window_required or window.get('valid') is True
            else window.get('valid') is False,
    }
    return {'frozen_criteria': dict(FROZEN_CRITERIA), 'formal_missed_ratio': formal_ratio,
            'target_missed_ratio': target_ratio, 'full_window_required': full_window_required,
            'window_note': '完整目标档须有完整60秒窗口并满足接近或达到原始标准；短诊断无窗口只豁免分钟吞吐证明，不豁免成功率、用量、错过和稳定性要求。',
            'checks': checks}


class SyntheticUpstream:
    def __init__(self, latency):
        self.latency = latency
        self.body_sizes = []
        self.active = 0
        self.peak_active = 0
        self.received = []
        self.tasks = set()

    async def connection(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            while True:
                headers = await reader.readuntil(b'\r\n\r\n')
                fields = dict(line.split(':', 1) for line in headers.decode().split('\r\n')[1:] if ':' in line)
                length = int(next(v for k, v in fields.items() if k.lower() == 'content-length'))
                body = await reader.readexactly(length)
                payload = json.loads(body)
                assert payload.get('stream') is True
                assert len(json.dumps(payload['messages']).encode()) > 8000, 'real calibration body required'
                self.body_sizes.append(length)
                self.received.append(time.time())
                self.active += 1
                self.peak_active = max(self.peak_active, self.active)
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n')
                def chunk(value):
                    data = ('data: ' + (value if isinstance(value, str) else json.dumps(value)) + '\n\n').encode()
                    writer.write(f'{len(data):x}\r\n'.encode() + data + b'\r\n')
                chunk({'choices': [{'delta': {'content': '本地合成摘要。'}, 'finish_reason': None}]})
                await writer.drain()
                await asyncio.sleep(self.latency)
                chunk({'choices': [{'delta': {}, 'finish_reason': 'stop'}]})
                # These are deliberately synthetic counters, not tokenizer measurements.
                synthetic_input = len(re.findall(r'[A-Za-z]+', payload['messages'][0]['content'])) + 64
                usage = {'prompt_tokens': synthetic_input, 'completion_tokens': 500, 'total_tokens': synthetic_input + 500,
                         'prompt_tokens_details': {'cached_tokens': int(synthetic_input * .95)}}
                chunk({'choices': [], 'usage': usage})
                chunk({'choices': [], 'usage': usage})  # repeated cumulative snapshot
                chunk('[DONE]')
                writer.write(b'0\r\n\r\n')
                await writer.drain()
                self.active -= 1
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            self.tasks.discard(task)


def percentile(values, quantile):
    values = sorted(values)
    return values[min(len(values) - 1, int((len(values) - 1) * quantile))] if values else None


def workbench_command(args, output):
    sparse = getattr(args, 'sparse_diagnostics', False)
    if args.diagnostics or sparse:
        return [sys.executable, '-u', str(ROOT / 'tools' / 'tpm_runtime_probe.py'),
                '--output', str(output / 'runtime-probe.json'), '--mode', 'sparse' if sparse else 'full']
    return [sys.executable, '-u', str(ROOT / 'route_test_ui.py')]


async def benchmark(args, output):
    observation = ObservationRecorder()
    upstream = SyntheticUpstream(args.latency)
    server = await asyncio.start_server(upstream.connection, '127.0.0.1', 0, limit=1024 * 1024)
    upstream_port = server.sockets[0].getsockname()[1]
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        app_port = reservation.getsockname()[1]
    env = dict(os.environ, ROUTE_TEST_PORT=str(app_port), ROUTE_TEST_HOST='127.0.0.1',
               ROUTE_TEST_DATA_DIR=str(output / 'data'), ROUTE_TEST_DISABLE_CREDENTIAL_STORE='1')
    app = None
    observer = None
    poll_ms, payload_bytes, rss_kib, observer_samples = [], [], [], []
    base = f'http://127.0.0.1:{app_port}'
    with (output / 'server.log').open('w') as log:
        try:
            command = workbench_command(args, output)
            app = subprocess.Popen(command, cwd=ROOT,
                                   env=env, stdout=log, stderr=log)
            async with httpx.AsyncClient(base_url=base, trust_env=False, timeout=15) as client:
                for _ in range(200):
                    try:
                        response = await client.get('/health')
                        if response.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(.05)
                else:
                    raise RuntimeError('isolated workbench did not start')
                async def post(path, data):
                    response = await client.post(path, json=data)
                    response.raise_for_status()
                    return response.json()
                await post('/auth/setup', {'username': 'tpm-benchmark', 'password': 'Synthetic-TPM-benchmark-2026'})
                config = {'url': f'http://127.0.0.1:{upstream_port}/v1', 'api_key': 'synthetic-local-key',
                          'model': 'tpm-synthetic', 'cache_write_price_mode': 'input',
                          'target_duration_seconds': args.seconds, 'ramp_duration_seconds': args.ramp_seconds,
                          'calibration_requests': 20, 'warmup_requests': 1,
                          'concurrency': args.concurrency, 'pool_connections': args.concurrency,
                          'budget_usd': 80, **FROZEN_CRITERIA}
                preview = await post('/tpm/preview', {'config': config})
                assert not upstream.body_sizes, 'preview must never request upstream'
                run = await post('/tpm/runs', {'config': config, 'calibration_confirmed': True,
                                             'request_id': 'synthetic-benchmark'})
                identifier = run['id']
                if args.browser:
                    observer = subprocess.Popen(['node', str(ROOT / 'tools' / 'observe_tpm.cjs'), base,
                                                 str(output / 'browser.json'), str(args.seconds + 3 * args.ramp_seconds + 180)],
                                                cwd=ROOT, stdout=log, stderr=log)
                next_memory = 0
                async def poll():
                    nonlocal next_memory
                    before = time.perf_counter()
                    at = time.time()
                    response = await client.get('/tpm/runs/' + identifier + '/live')
                    response.raise_for_status()
                    poll_ms.append((time.perf_counter() - before) * 1000)
                    payload_bytes.append(len(response.content))
                    result = response.json()
                    observer_samples.append({'at': at, 'started_at': _timestamp(at), 'elapsed_ms': poll_ms[-1],
                                             'phase': result['phase'], 'payload_bytes': payload_bytes[-1],
                                             'missed': result['metrics']['missed'],
                                             'in_flight': result['metrics']['in_flight']})
                    if time.monotonic() >= next_memory:
                        next_memory = time.monotonic() + 2
                        value = await observation.sample_rss(
                            result, lambda: asyncio.to_thread(
                                subprocess.check_output, ['ps', '-o', 'rss=', '-p', str(app.pid)]))
                        if value is not None:
                            rss_kib.append(value)
                    return result
                deadline = time.monotonic() + 180 + args.seconds + 3 * args.ramp_seconds
                while run['phase'] != 'awaiting_run':
                    if run['status'] in {'error', 'stopped', 'finished', 'finished_with_errors'}:
                        raise RuntimeError('calibration failed: ' + json.dumps(run.get('calibration')))
                    if time.monotonic() > deadline:
                        raise RuntimeError('calibration deadline exceeded')
                    await asyncio.sleep(.25)
                    run = await poll()
                await post('/tpm/runs/' + identifier + '/start', {'confirmed': True, 'plan_id': run['plan_id']})
                while True:
                    run = await poll()
                    if run['phase'] in {'finished', 'interrupted'} and not run.get('persistence_pending'):
                        break
                    if time.monotonic() > deadline:
                        raise RuntimeError('formal benchmark deadline exceeded')
                    await asyncio.sleep(.25)
                observation.mark('export_before', run)
                exported = (await client.get('/tpm/runs/' + identifier + '/export')).json()
                observation.mark('export_after', exported)
                (output / 'evidence.json').write_text(json.dumps(exported, ensure_ascii=False), encoding='utf-8')
                (output / 'observer-samples.json').write_text(json.dumps(observer_samples, ensure_ascii=False), encoding='utf-8')
                if observer:
                    try:
                        await asyncio.to_thread(observer.wait, 15)
                    except subprocess.TimeoutExpired:
                        observer.terminate()
                        await asyncio.to_thread(observer.wait, 5)
                rows = [row for row in exported['requests'] if row.get('phase') == 'formal']
                target_rows = [row for row in rows if row.get('stage_index') == 4]
                applied_rps = len(target_rows) / args.seconds
                scheduling = scheduling_diagnostics(run, exported['requests'], observer_samples)
                acceptance = acceptance_diagnostics(run)
                admission = admission_diagnostics(run, exported['requests'])
                observation_metadata = observation.save(output / 'observation.json', benchmark_complete=True)
                summary = {'scope': '本机完整工作台 + 真实长请求体 + 合成流式上游；50000词元/请求是模拟计数，不是上游容量或真实分词验证。',
                           'environment': {'platform': platform.platform(), 'machine': platform.machine(),
                                           'python': platform.python_version(), 'httpx': httpx.__version__,
                                           'logical_cpus': os.cpu_count(), 'open_file_limits': resource.getrlimit(resource.RLIMIT_NOFILE)},
                           'parameters': {**vars(args), 'upstream': 'loopback only', 'synthetic_prompt_tokens': 49500,
                                          'synthetic_output_tokens': 500, 'synthetic_cached_tokens': 47025},
                           'target_rps': run['plan']['target_rps'], 'actual_target_rps': applied_rps,
                           'observer_view': 'display-v1',
                           'body_bytes_min': min(upstream.body_sizes), 'body_bytes_max': max(upstream.body_sizes),
                           'formal_body_bytes_min': min((row['body_bytes'] for row in rows), default=0),
                           'upstream_body_bytes_total': sum(upstream.body_sizes), 'upstream_peak_inflight': upstream.peak_active,
                           'persisted_formal_requests': len(rows), 'persisted_total_rows': len(exported['requests']),
                           'evidence_bytes': (output / 'evidence.json').stat().st_size,
                           'observer_http_p50_ms': statistics.median(poll_ms), 'observer_http_p95_ms': percentile(poll_ms, .95),
                           'observer_payload_max_bytes': max(payload_bytes),
                           'app_peak_observed_rss_mib': max(rss_kib) / 1024 if rss_kib else None,
                           'observation': observation_metadata,
                           'metrics': run['metrics'], 'fees': run['fees'], 'report': run['report'],
                           'scheduling': scheduling, 'acceptance': acceptance, 'admission': admission,
                           'browser': json.loads((output / 'browser.json').read_text()) if (output / 'browser.json').exists() else None}
                summary['checks'] = {**acceptance['checks'], **admission['checks'],
                                     'observation_evidence_saved': observation_metadata['complete'] is True
                                         and observation_metadata['saved'] is True,
                                     'live_observer_view_used': run.get('recent_requests_view') == 'display-v1',
                                     **upstream_evidence_checks(upstream.body_sizes, exported['requests']),
                                     'calibration_before_pressure': bool(run['calibration']['passed']),
                                     'rate_within_5_percent': abs(applied_rps / summary['target_rps'] - 1) <= .05,
                                     'scheduled_slots_reconciled': scheduling['unmapped_requests'] == 0
                                         and scheduling['duplicate_slots'] == 0
                                         and scheduling['reconstructed_missed'] == run['metrics']['missed'],
                                     'missed_reasons_reconciled': missed_reasons_reconcile(run),
                                     'all_planned_stages_executed': len(run['stages']) == len(run['plan']['stages']) == 4,
                                     'complete_target_observation_window': args.seconds < 60 or run['report']['primary_window']['valid'],
                                     'clock_evidence_consistent': scheduling['clock_evidence']['consistent'],
                                     'no_starts_after_dispatch': all(row['started_at'] <= run['dispatch_ended_at'] for row in rows)
                                         if scheduling['clock_evidence']['consistent'] else None,
                                     'no_lost_request_summaries': len(rows) == run['metrics']['started'],
                                     'evidence_complete': exported.get('evidence_complete') is True,
                                     'no_inflight_after_drain': run['metrics']['all_in_flight'] == 0,
                                     'no_duplicate_usage': all(row['usage']['total_tokens'] == row['usage']['raw']['total_tokens'] for row in rows),
                                     'request_timing_complete': all(
                                         row.get('timing', {}).get('started_at') and row['timing'].get('ended_at')
                                         and row['timing'].get('total_ms') is not None
                                         for row in exported['requests']),
                                     'task_timing_complete': bool(run.get('timing', {}).get('started_at')
                                                                  and run['timing'].get('ended_at')
                                                                  and run['timing'].get('dispatch_ended_at')),
                                     'real_long_body': summary['formal_body_bytes_min'] > 200000,
                                     'bounded_observation_latency': summary['observer_http_p95_ms'] < 1000,
                                     'browser_observed': not args.browser or bool(summary['browser'] and not summary['browser'].get('errors'))}
                return summary
        finally:
            if not observation.save_attempted:
                observation.save(output / 'observation.json', benchmark_complete=False)
            if observer and observer.poll() is None:
                observer.terminate()
                await asyncio.to_thread(observer.wait, 5)
            if app:
                app.terminate()
                try:
                    await asyncio.to_thread(app.wait, 8)
                except subprocess.TimeoutExpired:
                    app.kill()
                    await asyncio.to_thread(app.wait)
            server.close()
            await server.wait_closed()
            for task in list(upstream.tasks):
                task.cancel()
            await asyncio.gather(*upstream.tasks, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=90)
    parser.add_argument('--ramp-seconds', type=float, default=20)
    parser.add_argument('--latency', type=float, default=1)
    parser.add_argument('--concurrency', type=int, default=256)
    parser.add_argument('--browser', action='store_true')
    diagnostics = parser.add_mutually_exclusive_group()
    diagnostics.add_argument('--diagnostics', action='store_true', help='Record bounded runtime timing probes; does not change load or GC settings')
    diagnostics.add_argument('--sparse-diagnostics', action='store_true', help='Sample each 32nd sleep, request entry and selector call; does not change load or GC settings')
    args = parser.parse_args()
    if not (1 <= args.seconds <= 180 and .1 <= args.ramp_seconds <= 30 and .01 <= args.latency <= 5 and 1 <= args.concurrency <= 1024):
        parser.error('benchmark bounds: duration 1–180, ramp .1–30, latency .01–5, concurrency 1–1024')
    base = ROOT / 'artifacts' / 'tpm-performance'
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S-')
    output = Path(tempfile.mkdtemp(prefix=stamp, dir=base))
    print(f'本地性能证据目录：{output}', flush=True)
    summary = asyncio.run(benchmark(args, output))
    if args.diagnostics or args.sparse_diagnostics:
        probe_path = output / 'runtime-probe.json'
        probe = json.loads(probe_path.read_text(encoding='utf-8')) if probe_path.exists() else {}
        summary['runtime_probe'] = {'path': str(probe_path), **{key: probe.get(key) for key in (
            'complete', 'mode', 'sampling', 'threshold_ms', 'event_limit', 'retained_events', 'dropped_events', 'statistics',
            'gc_configuration_at_start', 'gc_configuration_at_end', 'note')}}
        summary['checks']['diagnostics_saved'] = probe.get('complete') is True
        summary['checks']['diagnostics_gc_settings_unchanged'] = bool(probe) and (
            probe.get('gc_configuration_at_start') == probe.get('gc_configuration_at_end'))
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if all(summary['checks'].values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
