"""TPM（每分钟 Token 数）完成时间统计；不把计划或收尾当作容量证据。"""
from __future__ import annotations

import math
from collections import Counter

from tpm_config import CLOCK_EVIDENCE_TOLERANCE_MS


def _finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def report_clock_evidence(records, *, required=False):
    """Verify wall timestamps against the independent monotonic request evidence.

    Keep observed timestamps intact: a clock step must invalidate a window, not
    move requests into another window. Old imported rows can be explicitly
    unknown; every new live run requires these fields even if all are missing.
    """
    formal = [row for row in records if row.get("phase") == "formal"]
    new_fields = any("signed_schedule_delay_seconds" in row or "stage_deadline_margin_seconds" in row
                     for row in formal)
    enforced = required or new_fields
    missing = invalid = checked = 0
    start_offsets, end_offsets = [], []
    fields = ("scheduled_at", "started_at", "ended_at", "signed_schedule_delay_seconds",
              "stage_deadline_margin_seconds", "latency_seconds")
    for row in formal:
        if any(row.get(key) is None for key in fields):
            missing += 1
            continue
        if (not all(_finite_number(row[key]) for key in fields) or
                row["signed_schedule_delay_seconds"] < 0 or row["latency_seconds"] < 0 or
                row["stage_deadline_margin_seconds"] <= 0):
            invalid += 1
            continue
        projected_start = row["scheduled_at"] + row["signed_schedule_delay_seconds"]
        projected_end = projected_start + row["latency_seconds"]
        start_offsets.append((row["started_at"] - projected_start) * 1000)
        end_offsets.append((row["ended_at"] - projected_end) * 1000)
        checked += 1
    start_mismatches = sum(abs(value) > CLOCK_EVIDENCE_TOLERANCE_MS for value in start_offsets)
    end_mismatches = sum(abs(value) > CLOCK_EVIDENCE_TOLERANCE_MS for value in end_offsets)
    complete = bool(formal) and checked == len(formal)
    status = ("inconsistent" if start_mismatches or end_mismatches else
              "consistent" if complete else "unknown")
    return {"status": status, "required": enforced, "complete": complete,
            "consistent": status == "consistent" if status != "unknown" else None,
            "source": "monotonic_request_projection" if enforced else "legacy_missing",
            "tolerance_ms": CLOCK_EVIDENCE_TOLERANCE_MS,
            "formal_requests": len(formal), "checked_requests": checked,
            "missing_requests": missing, "invalid_requests": invalid,
            "start_mismatches": start_mismatches, "end_mismatches": end_mismatches,
            "max_abs_start_offset_ms": max(map(abs, start_offsets), default=None),
            "max_abs_end_offset_ms": max(map(abs, end_offsets), default=None)}


def percentile(values, quantile):
    values = sorted(float(value) for value in values if value is not None)
    if not values:
        return None
    position = (len(values) - 1) * quantile
    low = math.floor(position)
    high = math.ceil(position)
    return values[low] + (values[high] - values[low]) * (position - low)


def metrics(records, *, now, formal_started_at=None, planned=0, missed=0, in_flight=0):
    formal = [row for row in records if row.get("phase") == "formal"]
    window = [row for row in formal if now - 60 < row["ended_at"] <= now]
    successes = [row for row in window if row.get("success") and row["usage"].get("complete")]
    totals = {}
    for key, name in (("total_tokens", "full_token_tpm"), ("full_input_tokens", "input_tpm"),
                      ("output_tokens", "output_tpm"), ("cache_read_tokens", "cache_read_tpm"),
                      ("cache_write_tokens", "cache_write_tpm")):
        values = [row["usage"].get(key) for row in successes]
        totals[name] = sum(value for value in values if value is not None)
        if key.startswith("cache_") and any(value is None for value in values):
            totals[name] = None
    reliable = [row for row in formal if row["usage"].get("complete")]
    cache_reliable = [row for row in reliable if row["usage"].get("cache_read_tokens") is not None]
    full_input = sum(row["usage"].get("full_input_tokens", 0) for row in cache_reliable)
    started_window = [row for row in formal if now - 10 < row["started_at"] <= now]
    seconds = max(0, min(60, now - formal_started_at)) if formal_started_at else 0
    elapsed = max(.001, min(10, now - formal_started_at)) if formal_started_at else 10
    errors = Counter(row.get("error") for row in formal if not row.get("success"))
    return {
        **totals, "planned": planned, "started": len(formal) + in_flight,
        "succeeded": sum(bool(row.get("success")) for row in formal),
        "failed": sum(not row.get("success") for row in formal), "missed": missed,
        "in_flight": in_flight, "actual_start_rps": len(started_window) / elapsed,
        "window_seconds": seconds, "window_full": seconds >= 60,
        "success_rate": sum(bool(row.get("success")) for row in formal) / len(formal) if formal else None,
        "usage_complete_rate": len(reliable) / len(formal) if formal else None,
        "cache_hit_rate": sum(row["usage"]["cache_read_tokens"] for row in cache_reliable) / full_input if full_input else None,
        "ttft_p50": percentile([row.get("ttft_seconds") for row in formal], .5),
        "ttft_p95": percentile([row.get("ttft_seconds") for row in formal], .95),
        "latency_p50": percentile([row.get("latency_seconds") for row in formal], .5),
        "latency_p95": percentile([row.get("latency_seconds") for row in formal], .95),
        "schedule_delay_p95": percentile([row.get("schedule_delay_seconds") for row in formal], .95),
        "pool_wait_p95": percentile([row.get("pool_wait_seconds") for row in formal], .95),
        "pool_wait_source": "client_admission_semaphore", "transport_pool_wait": None,
        "http_429": errors["http_429"], "timeouts": errors["timeout"],
        "output_limited": sum(bool(row.get("output_limited")) for row in formal),
        "errors": dict(errors),
    }


def build_report(config, records, stages, series, *, dispatch_ended_at=None, last_ended_at=None,
                 stop_reason=None, missed=0, planned=0, require_clock_evidence=False):
    """Last full target-stage minute is frozen at dispatch end, excluding drain completions."""
    target = next((stage for stage in stages if stage.get("target_tpm") == config["target_tpm"]), None)
    formal = [row for row in records if row.get("phase") == "formal"]
    clock = report_clock_evidence(formal, required=require_clock_evidence)
    clock_blocked = clock["required"] and clock["status"] != "consistent"
    target_rows = [row for row in formal if target and row.get("stage_index") == target["index"]]
    start = target.get("started_at") if target else None
    end = min(target.get("ended_at") or dispatch_ended_at or 0, dispatch_ended_at or float("inf")) if target else None
    full_window = bool(start is not None and end is not None and end - start >= 60)
    valid_window = full_window and not clock_blocked
    window_start = end - 60 if full_window else None
    window = [row for row in target_rows if full_window and _finite_number(row.get("ended_at"))
              and window_start < row["ended_at"] <= end]
    complete = [row for row in window if row.get("success") and row["usage"].get("complete")]
    total = sum(row["usage"]["total_tokens"] for row in complete)
    success_rate = sum(bool(row.get("success")) for row in target_rows) / len(target_rows) if target_rows else 0
    usage_rate = sum(bool(row["usage"].get("complete")) for row in target_rows) / len(target_rows) if target_rows else 0
    target_successes = [row for row in target_rows if row.get("success")]
    successful_usage_rate = (sum(bool(row["usage"].get("complete")) for row in target_successes) /
                             len(target_successes)) if target_successes else 0
    target_planned = target.get("planned_requests", len(target_rows)) if target else 0
    target_missed = max(0, target_planned - len(target_rows))
    local_limited = bool((planned and missed / planned > config.get("max_missed_ratio", .01)) or
                         (target_planned and target_missed / target_planned > config.get("max_missed_ratio", .01)))
    samples = [point for point in series if start and start <= point["at"] <= (end or 0)]
    quarters = max(1, len(samples) // 4)
    first = samples[:quarters]
    last = samples[-quarters:]
    initial_inflight = sum(point.get("in_flight", 0) for point in first) / len(first) if first else 0
    final_inflight = sum(point.get("in_flight", 0) for point in last) / len(last) if last else 0
    sustained_backlog = len(samples) >= 8 and final_inflight > initial_inflight + max(5, initial_inflight * config.get("max_backlog_growth_ratio", .1))
    midpoint = (start + end) / 2 if start is not None and end is not None else 0
    early = [row for row in target_rows if _finite_number(row.get("started_at")) and row["started_at"] < midpoint]
    late = [row for row in target_rows if _finite_number(row.get("started_at")) and row["started_at"] >= midpoint]
    early_p95 = percentile([row["latency_seconds"] for row in early if _finite_number(row.get("latency_seconds"))], .95)
    late_p95 = percentile([row["latency_seconds"] for row in late if _finite_number(row.get("latency_seconds"))], .95)
    latency_worsened = bool(early_p95 and late_p95 and len(early) >= 5 and len(late) >= 5 and late_p95 > early_p95 * config.get("max_latency_degradation_ratio", 2.0))
    early_error_rate = sum(not row.get("success") for row in early) / len(early) if early else None
    late_error_rate = sum(not row.get("success") for row in late) / len(late) if late else None
    errors_worsened = bool(len(early) >= 5 and len(late) >= 5 and
                           late_error_rate - early_error_rate > 1 - config.get("min_success_rate", .99))
    window_success_rate = sum(bool(row.get("success")) for row in window) / len(window) if window else 0
    stable = (success_rate >= config.get("min_success_rate", .99) and
              window_success_rate >= config.get("min_success_rate", .99) and
              not sustained_backlog and not latency_worsened and not errors_worsened)
    # Retain the original wall-window arithmetic for audit even when it cannot
    # support a verified minute or a capacity verdict. Never silently re-bucket.
    ratio = total / config["target_tpm"] if full_window else None
    if not valid_window or successful_usage_rate < 1 or local_limited:
        verdict = "insufficient_evidence"
        label = "证据不足/发压端受限"
    elif ratio >= 1 and stable:
        verdict, label = "reached", "达到目标"
    elif ratio >= config.get("near_target_ratio", .95) and stable:
        verdict, label = "near", "接近目标"
    else:
        verdict, label = "not_reached", "未达到"
    limitations = ["客户端完成时间口径；供应商内部容量、内部重试和最终账单未知。",
                   "连接池等待仅测量客户端准入信号量；传输库内部等待不可单独观测。"]
    if config.get("cache_mode") == "prefer_cache":
        limitations.append("仅覆盖缓存优先、长输入短输出场景，不外推非缓存或其他业务。")
    if not full_window:
        limitations.append("目标档没有完整 60 秒发压观察窗口；收尾完成量不用于补足窗口。")
    if clock["status"] == "inconsistent":
        limitations.append(f"正式请求的墙上时间与单调时间投影偏差超过 {CLOCK_EVIDENCE_TOLERANCE_MS} 毫秒，无法核验完整 60 秒窗口；"
                           "保留的窗口用量、比例和滚动峰值仅供核查，不构成达到目标的证据，原始时间和用量未改写。")
    elif clock["status"] == "unknown":
        limitations.append("正式请求缺少或具有无效的独立时间证据，时间一致性未知；无法核验完整 60 秒窗口。"
                           "保留的窗口用量和比例仅供核查。" if clock["required"] else
                           "旧记录缺少独立单调时间证据，时间一致性未知；历史窗口及结论未经当前时间证据标准核验。")
    if successful_usage_rate < 1:
        limitations.append("成功请求存在缺失或不可靠的用量，不能确认真实完整 Token TPM（每分钟完整用量）。")
    elif usage_rate < 1:
        limitations.append("部分失败请求缺少用量，影响总费用完整性；失败请求不计入成功完成吞吐。")
    if local_limited:
        limitations.append("错过时隙超过冻结阈值，计划压力未充分施加。")
    peaks = [point.get("full_token_tpm", 0) for point in series if point.get("window_full") and dispatch_ended_at and point["at"] <= dispatch_ended_at]
    cumulative = {}
    for key in ("input_tokens", "full_input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "total_tokens"):
        known = [row["usage"].get(key) for row in records if row["usage"].get("complete")]
        cumulative[key] = sum(value for value in known if value is not None)
        cumulative[key + "_complete"] = len(known) == len(records) and all(value is not None for value in known)
    return {"verdict": verdict, "label": label, "summary": label,
            "clock_evidence": clock,
            "stable": stable and verdict == "reached", "target_tpm": config["target_tpm"],
            "primary_window": {"valid": valid_window, "start": window_start, "end": end,
                               "full_tokens": total if full_window else None, "ratio": ratio,
                               "time_evidence_verified": clock["status"] == "consistent",
                               "measurement_status": "verified" if valid_window and clock["status"] == "consistent" else "unverified",
                               "success_rate": window_success_rate if window else None,
                               "completed_requests": len(window),
                               "stage_index": target["index"] if target else None},
            "rolling_peak_tpm": max(peaks, default=None), "target_success_rate": success_rate,
            "target_usage_complete_rate": usage_rate, "sustained_backlog": sustained_backlog,
            "successful_usage_complete_rate": successful_usage_rate,
            "latency_worsened": latency_worsened, "early_latency_p95": early_p95, "late_latency_p95": late_p95,
            "early_error_rate": early_error_rate, "late_error_rate": late_error_rate,
            "errors_increased": early_error_rate is not None and late_error_rate is not None and late_error_rate > early_error_rate,
            "errors_worsened": errors_worsened,
            "cumulative_usage": cumulative,
            "local_limited": local_limited, "actual_started": len(formal), "planned": planned,
            "target_planned": target_planned, "target_missed": target_missed,
            "missed": missed, "applied_ratio": len(formal) / planned if planned else None,
            "dispatch_ended_at": dispatch_ended_at, "last_request_ended_at": last_ended_at,
            "drain_seconds": max(0, last_ended_at - dispatch_ended_at) if last_ended_at and dispatch_ended_at else 0,
            "stop_reason": stop_reason, "limitations": limitations}
