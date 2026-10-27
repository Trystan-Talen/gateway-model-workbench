"""TPM task admission, confirmation, history and HTTP dispatch.

The HTTP layer supplies the existing credential resolver, async loop, resource
limiter and history writer. No upstream request is made by preview or restore.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path
import re
import threading
import time
import uuid

from task_status import TERMINAL_STATES, apply_transition
import tpm_config
from tpm_engine import TpmRun
from tpm_records import project_request_summary
from tpm_snapshot import clone_snapshot
from time_record import request_timing, task_timing, normalize_timing_tree


_SUMMARY_KEYS = ('id', 'name', 'status', 'phase', 'created', 'finished_at', 'stop_reason',
                 'metrics', 'fees', 'resource_exclusive', 'model', 'target_tpm', 'cache_mode', 'timing')
_SNAPSHOT_METADATA_KEYS = ('request_id', 'request_fingerprint', 'actor', 'error', 'request_count')


class TpmError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def plan_id(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def _restore_request_timing(row):
    row = normalize_timing_tree(row)
    if isinstance(row.get("timing"), dict):
        row["timing"] = request_timing(**row["timing"])
    else:
        def milliseconds(key):
            value = row.get(key)
            return value * 1000 if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        row["timing"] = request_timing(source="legacy", scheduled_at=row.get("scheduled_at"),
            enqueued_at=row.get("enqueued_at"), started_at=row.get("started_at"),
            first_token_at=row.get("first_token_at"), ended_at=row.get("ended_at"),
            schedule_lag_ms=milliseconds("schedule_delay_seconds"),
            ttft_ms=milliseconds("ttft_seconds"), total_ms=milliseconds("latency_seconds"))
    return row


class FrozenRun:
    """Read-only recovered evidence; even a confirmed plan cannot resume billing."""
    def __init__(self, snapshot):
        self.snapshot = normalize_timing_tree(snapshot)
        self.id = str(snapshot['id'])
        self.config = dict(snapshot.get('config') or {})
        self.config.pop('api_key', None)
        self.output_path = snapshot.get('out_path')
        self.created = snapshot.get('created', 0)
        self.status = snapshot.get('status', 'error')
        timing = self.snapshot.get('timing')
        self.snapshot['timing'] = task_timing(**timing) if isinstance(timing, dict) else task_timing(
            source='legacy', created_at=snapshot.get('created'), started_at=snapshot.get('started_at'),
            ended_at=snapshot.get('finished_at'), dispatch_ended_at=snapshot.get('dispatch_ended_at'))
        for field in ('recent_requests', 'requests'):
            if isinstance(self.snapshot.get(field), list):
                self.snapshot[field] = [_restore_request_timing(row) for row in self.snapshot[field] if isinstance(row, dict)]
        if self.status not in TERMINAL_STATES:
            self.status = 'error'
            self.snapshot.update(status='error', phase='interrupted', stop_reason='interrupted',
                                 finished_at=None,
                                 unknown_bill_risk=True,
                                 billing_state='uncertain_after_interrupt',
                                 error='服务在任务结束前重启；已保留费用预留，未自动恢复收费请求。')
            self.snapshot['timing'] = task_timing(**{**self.snapshot['timing'],
                'ended_at': None, 'elapsed_ms': None, 'drain_ms': None,
                'interruption_detected_at': time.time(), 'source': 'partial'})
            report = dict(self.snapshot.get('report') or {})
            report.update(verdict='insufficient_evidence', label='证据不足/发压端受限',
                          summary='服务中断，不能据此认定目标通过。', stable=False,
                          unknown_bill_risk=True)
            self.snapshot['report'] = report
        if isinstance(self.snapshot.get('report'), dict):
            self.snapshot['report']['timing'] = copy.deepcopy(self.snapshot['timing'])
        self.snapshot['history_only'] = True
        self.snapshot['config'] = self.config

    def snap(self, *, summary=False, display=False, include_requests=False, **_):
        # Select before copying: list polling must never traverse long recovered evidence.
        value = ({key: self.snapshot[key] for key in _SUMMARY_KEYS + _SNAPSHOT_METADATA_KEYS
                  if key in self.snapshot} if summary else self.snapshot)
        result = clone_snapshot(value)
        if display and not summary and not include_requests:
            if isinstance(result.get('recent_requests'), list):
                result['recent_requests'] = [project_request_summary(row) for row in result['recent_requests']]
            result['recent_requests_view'] = 'display-v1'
        return result


class TpmManager:
    def __init__(self, base_dir, *, loop, acquire, release, persist, resolve, run_factory=TpmRun):
        self.base_dir = Path(base_dir)
        self.loop = loop
        self.acquire = acquire
        self.release = release
        self.persist = persist
        self.resolve = resolve
        self.run_factory = run_factory
        self.lock = threading.RLock()
        self.runs = {}
        self.requests = {}
        self.futures = {}
        self.active_id = None
        self._closed = threading.Event()
        self._monitor = None

    def _watch(self):
        while not self._closed.wait(1):
            if self.active_id:
                if self.persist() is False:
                    with self.lock:
                        run = self.runs.get(self.active_id)
                    if run:
                        run.persistence_error = '历史索引写入失败，已停止发新请求；请检查磁盘和权限。'
                        run.stop(reason='history_write_error')
                        if run.status in TERMINAL_STATES:
                            self._release(run)

    def _ensure_monitor(self):
        if self._monitor is None:
            self._monitor = threading.Thread(target=self._watch, name='tpm-history', daemon=True)
            self._monitor.start()

    def get(self, identifier):
        with self.lock:
            run = self.runs.get(identifier)
        if run is None:
            raise TpmError('任务不存在', 404)
        return run

    def snapshot(self, run, *, summary=False, display=False):
        if summary:
            data = run.snap(summary=True)
        elif display:
            data = run.snap(display=True)
        else:
            data = run.snap()
        if not summary:
            data['plan_id'] = plan_id(data.get('plan') or {})
        data['out_path'] = str(self.base_dir / f'tpm_run_{run.id}.jsonl')
        data['resource_exclusive'] = self.active_id == run.id
        data['request_id'] = getattr(run, '_request_id', data.get('request_id'))
        data['request_fingerprint'] = getattr(run, '_request_fingerprint', data.get('request_fingerprint'))
        data['actor'] = getattr(run, '_actor', data.get('actor'))
        data['target_tpm'] = run.config.get('target_tpm')
        data['cache_mode'] = run.config.get('cache_mode')
        data['error'] = getattr(run, 'persistence_error', None) or getattr(run, 'error', None) or data.get('error')
        data['request_count'] = len(run.requests) if hasattr(run, 'requests') else data.get('request_count')
        with self.lock:
            data['persistence_pending'] = run.id in self.futures
        if data['persistence_pending'] and run.status in TERMINAL_STATES:
            data['phase'], data['status'] = 'draining', 'stopping'
        return data

    def ordered(self, *, summary=False, display=False):
        with self.lock:
            runs = list(self.runs.values())
        options = {'summary': summary}
        if display:
            options['display'] = True
        result = [self.snapshot(run, **options) for run in runs]
        result.sort(key=lambda item: item.get('created') or 0, reverse=True)
        if summary:
            result = [{key: item[key] for key in _SUMMARY_KEYS if key in item} for item in result]
        return result

    def create(self, body, actor):
        if body.get('calibration_confirmed') is not True:
            raise TpmError('校准与预热也会收费；请先查看费用并明确确认。')
        request_id = body.get('request_id')
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', request_id):
            raise TpmError('缺少有效的防重复提交编号')
        raw = body.get('config')
        if not isinstance(raw, dict):
            raise TpmError('配置必须为对象')
        # Preview uses no credential access. Only explicit calibration resolves it.
        config = tpm_config.validate_config(self.resolve(raw), require_secret=True)
        safe = {k: v for k, v in config.items() if k != 'api_key'}
        fingerprint = plan_id(safe)
        key = (actor, request_id)
        with self.lock:
            previous = self.requests.get(key)
            if previous:
                run = self.runs[previous]
                old = self.snapshot(run)
                if old.get('request_fingerprint') != fingerprint:
                    raise TpmError('提交编号已用于另一份配置，请重新预览。', 409)
                return run
            if self.active_id:
                raise TpmError('已有 TPM（每分钟词元）任务占用资源；请先停止并等待收尾。', 409)
            identifier = uuid.uuid4().hex
            if not self.acquire(identifier):
                raise TpmError('其他测试正在运行或排队；本模块需要独占本机发压资源。', 409)
            try:
                run = self.run_factory(config, task_id=identifier,
                                       output_path=str(self.base_dir / f'tpm_run_{identifier}.jsonl'))
                run._request_id, run._request_fingerprint, run._actor = request_id, fingerprint, actor
                self.runs[identifier] = run
                self.requests[key] = identifier
                self.active_id = identifier
            except Exception:
                self.release(identifier)
                raise
        # Persist consent and identity before scheduling any billable work.
        self._persist_before_launch(run)
        self._ensure_monitor()
        self._launch(run, 'calibrate')
        return run

    def _persist_before_launch(self, run):
        if self.persist() is not False:
            return
        run.persistence_error = '历史索引无法保存，已阻止启动收费请求；请检查磁盘和权限。'
        apply_transition(run, 'error', force=True)
        run.phase, run.stop_reason = 'finished', 'history_write_error'
        if hasattr(run, 'complete_timing'):
            run.complete_timing()
        with self.lock:
            self.futures.pop(run.id, None)
        self._release(run)
        raise TpmError(run.persistence_error, 503)

    def _launch(self, run, method):
        coroutine = getattr(run, method)()
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, self.loop())
        except Exception:
            coroutine.close()
            apply_transition(run, 'error', force=True)
            run.phase, run.error = 'finished', '后台执行循环不可用，未启动新请求。'
            if hasattr(run, 'complete_timing'):
                run.complete_timing()
            self._release(run)
            self.persist()
            raise TpmError('后台执行循环不可用', 503) from None
        with self.lock:
            self.futures[run.id] = future

        def done(result):
            try:
                result.result()
            except BaseException:
                if run.status != 'stopped':
                    apply_transition(run, 'error', force=True)
                    run.phase, run.error = 'interrupted', '执行意外中断；用量不明请求保留费用风险。'
            finally:
                with self.lock:
                    self.futures.pop(run.id, None)
                if run.status in TERMINAL_STATES:
                    self._release(run)
                self.persist()
        future.add_done_callback(done)

    def _release(self, run):
        run.config.pop('api_key', None)
        with self.lock:
            if self.active_id == run.id:
                self.active_id = None
        self.release(run.id)

    def start(self, identifier, body):
        with self.lock:
            run = self.get(identifier)
            if isinstance(run, FrozenRun):
                raise TpmError('历史任务不能恢复发压；请按原参数新建并重新校准。', 409)
            if body.get('confirmed') is not True:
                raise TpmError('正式发压会产生费用，请确认校准后的冻结方案。')
            if body.get('plan_id') != self.snapshot(run)['plan_id']:
                raise TpmError('方案已变化，请刷新并重新确认费用。', 409)
            if run.phase != 'awaiting_run' or identifier in self.futures:
                raise TpmError('任务尚未完成校准、已经启动或已经结束。', 409)
            # Mark admission synchronously; another HTTP start cannot race the loop.
            self.futures[identifier] = None
        self._persist_before_launch(run)
        self._launch(run, 'run')
        return run

    def stop(self, identifier):
        run = self.get(identifier)
        if not isinstance(run, FrozenRun) and run.status not in TERMINAL_STATES:
            run.stop()
            if run.status in TERMINAL_STATES:
                self._release(run)
            self.persist()
        return run

    def revise_plan(self, identifier, body):
        """Only duration can change after calibration; every revision invalidates consent."""
        if set(body) != {'target_duration_seconds'}:
            raise TpmError('校准后仅允许调整目标档时长；模式、价格或预算变化请新建任务。')
        with self.lock:
            run = self.get(identifier)
            if isinstance(run, FrozenRun) or run.phase != 'awaiting_run' or identifier in self.futures:
                raise TpmError('仅校准完成、尚未开始的任务可以重新估价。', 409)
            config = tpm_config.validate_config({**run.config, **body})
            updated = tpm_config.preview(config, run.calibration)
            config.update(concurrency=updated['concurrency'], pool_connections=updated['pool_connections'])
            reserve = max(updated['request_reserve_usd'], run.plan['request_reserve_usd'])
            warmup = config['template_groups'] * config['warmup_requests'] if config['cache_mode'] == 'prefer_cache' else 0
            updated['request_reserve_usd'] = reserve
            updated['worst_case_cost_usd'] = reserve * (updated['planned_requests'] + config['calibration_requests'] + warmup)
            updated['reserve_basis'] = run.plan.get('reserve_basis')
            run.config, run.plan = config, updated
            if hasattr(run, 'refresh_plan_reserve_warning'):
                run.refresh_plan_reserve_warning()
            run.plan_id = plan_id(run.plan)
        self._persist_before_launch(run)
        return run

    def restore(self, snapshots):
        with self.lock:
            for data in snapshots:
                if not isinstance(data, dict) or not re.fullmatch(r'[a-f0-9]{32}', str(data.get('id', ''))):
                    continue
                run = FrozenRun(data)
                self.runs[run.id] = run
                if data.get('request_id'):
                    self.requests[(data.get('actor'), data['request_id'])] = run.id

    def export(self, identifier):
        run = self.get(identifier)
        result = self.snapshot(run)
        rows, damaged = [], 0
        path = self.base_dir / f'tpm_run_{run.id}.jsonl'
        if path.exists():
            with path.open(encoding='utf-8') as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        damaged += 1
                        continue
                    if not isinstance(row, dict) or not str(row.get('id', '')).startswith(run.id + '-'):
                        damaged += 1
                        continue
                    rows.append(_restore_request_timing(row))
        expected = result.get('request_count')
        complete = (run.status in TERMINAL_STATES and not result['persistence_pending']
                    and expected is not None and len(rows) == expected and not damaged and path.exists())
        result.update(requests=rows, evidence_missing=not path.exists(), damaged_rows=damaged,
                      evidence_complete=complete, expected_rows=expected, read_rows=len(rows),
                      evidence_note='证据已完整落盘' if complete else '证据尚未完整落盘、缺失或损坏；不可作为完整证据使用',
                      format='workbench-tpm-v1')
        if not complete and isinstance(result.get('report'), dict):
            result['report'].update(stable=False, evidence_incomplete=True)
        return result

    def close(self):
        self._closed.set()
        if self._monitor:
            self._monitor.join(2)
        with self.lock:
            active = self.runs.get(self.active_id)
            future = self.futures.get(self.active_id)
        if active:
            active.stop(reason='interrupted')
            if future:
                future.cancel()
            apply_transition(active, 'error', force=True)
            active.phase, active.stop_reason = 'interrupted', 'interrupted'
            self._release(active)
            self.persist()


def get_response(manager, path):
    if path == '/tpm/defaults':
        config = tpm_config.validate_config(copy.deepcopy(tpm_config.DEFAULTS), require_secret=False)
        return {'config': config, 'preview': tpm_config.preview(config)}, 200
    if path == '/tpm/runs':
        return {'runs': manager.ordered(summary=True)}, 200
    parts = path.strip('/').split('/')
    if len(parts) in (3, 4) and parts[:2] == ['tpm', 'runs']:
        if len(parts) == 4 and parts[3] == 'export':
            return manager.export(parts[2]), 200
        if len(parts) == 4 and parts[3] == 'live':
            return manager.snapshot(manager.get(parts[2]), display=True), 200
        if len(parts) == 3:
            return manager.snapshot(manager.get(parts[2])), 200
    raise TpmError('接口不存在', 404)


def post_response(manager, path, body, actor):
    if path == '/tpm/preview':
        config = tpm_config.validate_config(body.get('config') or {}, require_secret=False)
        return tpm_config.preview(config), 200
    if path == '/tpm/runs':
        run = manager.create(body, actor)
        return manager.snapshot(run), 201
    parts = path.strip('/').split('/')
    if len(parts) == 4 and parts[:2] == ['tpm', 'runs']:
        if parts[3] == 'start':
            return manager.snapshot(manager.start(parts[2], body)), 200
        if parts[3] == 'stop':
            return manager.snapshot(manager.stop(parts[2])), 200
        if parts[3] == 'plan':
            return manager.snapshot(manager.revise_plan(parts[2], body)), 200
    raise TpmError('接口不存在', 404)
