"""统一客户端时间证据，墙上时间用于定位，耗时由调用方的单调时钟提供。

不由结束时间减去耗时补造开始时间，也不由墙钟差推导耗时。旧记录可以显式
标记 source="legacy"；缺失、无时区或无效数据保留为 None。
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import copy
import math
from numbers import Real


REQUEST_TIMESTAMPS = (
    "scheduled_at", "enqueued_at", "started_at", "first_token_at", "ended_at",
)
REQUEST_DURATIONS = ("queue_ms", "schedule_lag_ms", "ttft_ms", "total_ms")
TASK_TIMESTAMPS = (
    "created_at", "started_at", "stop_requested_at", "dispatch_ended_at",
    "ended_at", "interruption_detected_at",
)
TASK_DURATIONS = ("elapsed_ms", "queue_ms", "drain_ms")
TIMEZONE_NAME = "Asia/Singapore"
# 本工作台统一使用新加坡标准时间的固定 +08:00 偏移，不读取机器的本地时区。
SINGAPORE_TIMEZONE = timezone(timedelta(hours=8), name=TIMEZONE_NAME)


def singapore_timestamp(value):
    """把 epoch 秒或带时区时间转换为 ISO 8601 毫秒新加坡时间（+08:00）。"""
    try:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, Real):
            if not math.isfinite(value):
                return None
            parsed = datetime.fromtimestamp(value, timezone.utc)
        elif isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        else:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(SINGAPORE_TIMEZONE).isoformat(timespec="milliseconds")
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _milliseconds(value):
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return round(value, 3)


def _timing(fields, timestamps, durations, required):
    result = {"version": 1, "clock": "client", "timezone": TIMEZONE_NAME}
    result.update({key: singapore_timestamp(fields.get(key)) for key in timestamps})
    result.update({key: _milliseconds(fields.get(key)) for key in durations})
    source = fields.get("source")
    if source not in {"recorded", "legacy", "partial"}:
        source = "recorded" if all(result[key] is not None for key in required) else "partial"
    result["source"] = source
    return result


def request_timing(**fields):
    """逐请求时间。可传入已有 timing（时间记录），未识别字段会被忽略。"""
    return _timing(fields, REQUEST_TIMESTAMPS, REQUEST_DURATIONS, ("started_at", "ended_at"))


def task_timing(**fields):
    """任务生命周期时间；进行中任务尚未记录的里程碑保持为空。"""
    return _timing(fields, TASK_TIMESTAMPS, TASK_DURATIONS,
                   ("created_at", "started_at", "ended_at"))


def normalize_timing_tree(value):
    """将快照内的时间证据转换为新加坡时间；不修改原对象、原文或协议。"""
    if isinstance(value, list):
        return [normalize_timing_tree(item) for item in value]
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    result = {}
    for key, item in value.items():
        if key == "timing" and isinstance(item, dict):
            is_task = any(field in item for field in ("created_at", "dispatch_ended_at", "elapsed_ms", "drain_ms"))
            result[key] = task_timing(**item) if is_task else request_timing(**item)
        elif key in {"raw_input", "raw_output", "raw", "protocol", "config", "public_config", "repeat_config"}:
            result[key] = copy.deepcopy(item)
        else:
            result[key] = normalize_timing_tree(item)
    return result
