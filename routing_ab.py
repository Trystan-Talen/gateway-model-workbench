#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""智能路由 vs 固定路由的黑盒对照统计。

这个模块只做算术，不发请求、不读密钥、不碰网络——因此可以离线复算、可以单测。

为什么不能只看两个百分比相减
--------------------------------
两侧各跑一千条、成功率 92% 对 94%，这个 2 个百分点里有多少是路由差异、有多少是
这一千条请求恰好抽到的运气？没人能从一次观测里分辨。所以这里给出的是**区间**：
Wilson 区间在样本量小的时候不会像正态近似那样给出越界或过窄的假精度；当两侧区间
重叠时，本模块会明确标注「本次观测不足以归因于路由」，而不是硬报一个差值。

配对为什么重要
--------------
同一份请求清单在两侧各跑一遍，同一条请求的两侧结果天然成对。按 ``item_id`` 配对后
可以只看「同一题在 A 组失败、在 B 组成成功」的那部分请求（McNemar 检验的思路），
这比拿两个独立的总体比例相比要敏感得多，也更贴近「同一批请求，换了路由」这个问题本身。

缓存命中率的诚实边界
--------------------
命中率的分母只算**真的返回了 usage 且带 cached_tokens 字段**的请求。没有 usage 的
请求必须排除，不能当成未命中——否则「某个网关不回 usage」会被误读成「缓存变差了」。
本模块会把这个排除数量单独报出来，让读者知道结论建立在多少条证据上。
"""
import math
import re
from collections import Counter, deque


# 归因等级：本次观测能不能支撑「差异来自路由」这句话。
ATTRIBUTION_OK = "attributable"
ATTRIBUTION_WEAK = "not_attributable"
ATTRIBUTION_UNKNOWN = "insufficient_evidence"

# 新增的序列统计另行标版本，不改已有报告的成功率、缓存与显著性口径。
STABILITY_PROTOCOL_VERSION = "routing-stability-v1"
TTFT_EWMA_WINDOW = 5
TTFT_EWMA_ALPHA = 0.5

# 报告里固定出现的指标口径说明。界面直接读这里，避免前后端各写一份、慢慢对不上。
METRIC_LABELS = [
    {"key": "success", "label": "成功率", "basis": "完整 2xx 响应 / 已收到结果"},
    {"key": "cache", "label": "缓存命中率", "basis": "usage.cached_tokens>0 / 返回了 usage 的请求"},
    {"key": "ttft", "label": "TTFT p95（首字耗时）", "basis": "仅统计成功请求"},
    {"key": "total_latency", "label": "总耗时 p95", "basis": "仅统计成功请求"},
    {"key": "max_consecutive_failures", "label": "最大连续失败次数",
     "basis": "按请求清单序号；未返回结果与编号间断打断连续性"},
    {"key": "ttft_ewma_max", "label": "TTFT（首字耗时）最大 EWMA（指数加权移动平均）",
     "basis": "连续 5 条有效成功请求；窗口首值初始化，平滑系数 0.5，取窗口最终值最大"},
    {"key": "reliability", "label": "配对一致性", "basis": "按 item_id 配对，未配对整对剔除"},
]


def wilson_interval(successes, total, z=1.96):
    """Wilson 得分区间。返回 ``(low, high)`` 的 0~1 比例，样本为 0 时返回 ``(None, None)``。

    选 Wilson 而不是 ``p ± 1.96·sqrt(p(1-p)/n)``：后者在 p 接近 0 或 1、或 n 很小时
    会算出负数或大于 1 的边界，报告里出现这种东西会直接毁掉可信度。
    """
    successes = int(successes or 0)
    total = int(total or 0)
    if total <= 0:
        return None, None
    successes = max(0, min(successes, total))
    phat = successes / total
    denom = 1 + z * z / total
    center = (phat + z * z / (2 * total)) / denom
    margin = z * math.sqrt(phat * (1 - phat) / total + z * z / (4 * total * total)) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


def fisher_exact_two_sided(a, b, c, d):
    """2×2 表的双侧 Fisher 精确检验，返回 p 值。

    表结构：
        A 组成功 a / 失败 b
        B 组成功 c / 失败 d

    用精确检验而不是卡方：路由对照里经常出现「两侧几乎都成功、只有个位数失败」的
    表格，卡方在这种稀疏表上的近似会严重失真。行和列和为 0 时返回 1.0（无信息）。
    """
    a, b, c, d = (max(0, int(x or 0)) for x in (a, b, c, d))
    row1, row2 = a + b, c + d
    col1 = a + c
    total = row1 + row2
    if total == 0 or row1 == 0 or row2 == 0 or col1 == 0 or col1 == total:
        return 1.0

    def _log_factorial(n):
        return math.lgamma(n + 1)

    def _log_choose(n, k):
        if k < 0 or k > n:
            return float("-inf")
        return _log_factorial(n) - _log_factorial(k) - _log_factorial(n - k)

    log_normalizer = _log_choose(total, row1)

    def _prob(x):
        return math.exp(_log_choose(col1, x) + _log_choose(total - col1, row1 - x)
                        - log_normalizer)

    lo = max(0, row1 - (total - col1))
    hi = min(row1, col1)
    observed = _prob(a)
    # 双侧定义为「所有概率不超过观测表概率的表」，加上一点容差吸收浮点误差。
    tolerance = 1e-9
    p_value = 0.0
    for x in range(lo, hi + 1):
        probability = _prob(x)
        if probability <= observed * (1 + tolerance):
            p_value += probability
    return min(1.0, max(0.0, p_value))


def rate_stat(successes, total):
    """比例 + Wilson 区间 + 可用样本量的统一包装。"""
    successes = int(successes or 0)
    total = int(total or 0)
    low, high = wilson_interval(successes, total)
    return {
        "successes": successes,
        "total": total,
        "rate": (successes / total) if total else None,
        "low": low,
        "high": high,
    }


def _percent(value):
    return None if value is None else round(value * 100, 2)


def public_rate(stat):
    """把内部比例转成报告用的百分数字段，并保留样本量。"""
    return {
        "successes": stat["successes"], "total": stat["total"],
        "pct": _percent(stat["rate"]),
        "ci_low_pct": _percent(stat["low"]), "ci_high_pct": _percent(stat["high"]),
    }


def compare_rates(base_success, base_total, cand_success, cand_total):
    """比较两侧比例：给差值、p 值，并判断本次观测是否足以归因。"""
    base = rate_stat(base_success, base_total)
    cand = rate_stat(cand_success, cand_total)
    result = {
        "baseline": public_rate(base),
        "candidate": public_rate(cand),
        "delta_pct": None,
        "p_value": None,
        "ci_overlap": None,
        "attribution": ATTRIBUTION_UNKNOWN,
        "note": "",
    }
    if base["rate"] is None or cand["rate"] is None:
        result["note"] = "至少一侧没有可用样本，无法比较。"
        return result

    result["delta_pct"] = round((cand["rate"] - base["rate"]) * 100, 2)
    result["p_value"] = round(fisher_exact_two_sided(
        base_success, base_total - base_success,
        cand_success, cand_total - cand_success), 6)
    overlap = not (cand["low"] > base["high"] or base["low"] > cand["high"])
    result["ci_overlap"] = overlap

    if overlap:
        result["attribution"] = ATTRIBUTION_WEAK
        result["note"] = "两侧 95% 置信区间重叠，本次观测不足以把差异归因于路由。"
    elif result["p_value"] is not None and result["p_value"] > 0.05:
        result["attribution"] = ATTRIBUTION_WEAK
        result["note"] = "置信区间虽不重叠，但精确检验 p>0.05，差异仍可能来自抽样波动。"
    else:
        result["attribution"] = ATTRIBUTION_OK
        result["note"] = "置信区间不重叠且 p<=0.05；差异在本次观测中可归因于路由，但仍限于本清单与本时段。"
    return result


def pair_outcomes(pairs):
    """按 ``item_id`` 配对后的 2×2 计数。

    ``pairs`` 是 ``{"item_id", "baseline_ok", "candidate_ok"}`` 的序列。返回四格与
    只统计口径不一致的请求数——两侧若有一侧缺结果，这条配对必须整条丢弃，
    否则 2×2 表里会混进「另一边根本没跑」的伪失败。
    """
    counts = Counter()
    unpaired = []
    for pair in pairs:
        base = pair.get("baseline_ok")
        cand = pair.get("candidate_ok")
        if base is None or cand is None:
            unpaired.append(pair.get("item_id"))
            continue
        counts[(bool(base), bool(cand))] += 1
    return {
        "both_ok": counts[(True, True)],
        "baseline_only_ok": counts[(True, False)],
        "candidate_only_ok": counts[(False, True)],
        "both_fail": counts[(False, False)],
        "paired_total": sum(counts.values()),
        "unpaired": sorted(str(x) for x in unpaired if x is not None),
        "unpaired_count": len(unpaired),
    }


def mcnemar_exact(baseline_only_ok, candidate_only_ok):
    """配对差异的精确检验（McNemar 精确版）。

    只看两组「不一致」的请求：A 成功 B 失败、以及 A 失败 B 成功。两侧一致的部分
    （都成功或都失败）不携带路由差异信息，参与计算只会稀释结论。因为在稀疏表上
    卡方近似不可靠，这里直接用二项精确检验。
    """
    b = max(0, int(baseline_only_ok or 0))
    c = max(0, int(candidate_only_ok or 0))
    discordant = b + c
    if discordant == 0:
        return {"discordant": 0, "p_value": 1.0}
    tail = sum(math.comb(discordant, k) for k in range(0, min(b, c) + 1)) / (2 ** discordant)
    return {"discordant": discordant, "p_value": round(min(1.0, 2 * tail), 6)}


def cache_hit_rate(records):
    """按平台口径统计命中率：``usage.cached_tokens > 0`` 即命中。

    没有 usage 的请求不进分母，单独计数。这个口径必须与 ``analyze.cache_stats``
    和在线累加器三方一致，任何一处漂移都会让缓存结论失真。
    """
    eligible = hits = 0
    for record in records:
        cached = record.get("cached_tokens")
        if cached is not None:
            eligible += 1
            hits += int((cached or 0) > 0)
    return {
        "hits": hits,
        "eligible": eligible,
        "total": len(records),
        "no_usage": len(records) - eligible,
    }


# 无法用抢跑解释的段首命中达到这个比例（且不少于 CONTAMINATION_MIN_HITS 段）才判定为
# 缓存被实验之外预热。留一点余量，避免个别上游把极短的公共片段计入 cached_tokens 时误报。
CONTAMINATION_THRESHOLD = 0.1
CONTAMINATION_MIN_HITS = 2


def cold_start_hits(records):
    """统计段首请求的命中情况，并区分「本侧抢跑」与「外部预热」。

    每段的第一条请求在本侧、本次运行里没有先行者（前缀带本侧隔离标记），理应未命中。
    它若命中，有两种来源：
    1. 本侧抢跑：并发下同段后续请求先到达上游、先写入缓存。只有当某条同段请求在
       段首请求拿到首字之前就已发出时才可能发生——用发送时间 ``ts`` 与段首请求的
       首字耗时（缺失时用总耗时）判断，属于正常现象，不算污染；
    2. 外部预热：没有任何同段请求来得及抢跑，缓存只能来自实验之外（别人同时在测、
       网关剥离了提示词开头、上次运行残留）。只有这一类计入 ``unexplained``。
    没有 ``burst_first`` 字段的旧记录不参与统计。
    """
    firsts = [r for r in records if r.get("burst_first") is True and r.get("cached_tokens") is not None]
    sibling_sent = {}
    for r in records:
        burst, sent = r.get("step_index"), r.get("ts")
        if r.get("burst_first") is False and burst is not None and isinstance(sent, (int, float)):
            sibling_sent[burst] = min(sent, sibling_sent.get(burst, sent))
    hits = raced = 0
    for r in firsts:
        if (r.get("cached_tokens") or 0) <= 0:
            continue
        hits += 1
        sent, burst = r.get("ts"), r.get("step_index")
        answered = sent + float(r.get("ttft_ms") or r.get("total_ms") or 0) if isinstance(sent, (int, float)) else None
        if answered is not None and burst in sibling_sent and sibling_sent[burst] <= answered:
            raced += 1
    return {"hits": hits, "raced": raced, "unexplained": hits - raced, "eligible": len(firsts),
            "pct": _percent(hits / len(firsts)) if firsts else None}


def compare_cache(baseline_records, candidate_records):
    """比较两侧缓存命中率，并把「无 usage」当作不可比而非未命中。"""
    base = cache_hit_rate(baseline_records)
    cand = cache_hit_rate(candidate_records)
    return _compare_cache_counts(base, cand, baseline_records, candidate_records)


def _compare_cache_counts(base, cand, baseline_records, candidate_records):
    """最终报告复用侧汇总的计数，段首污染判定仍读取完整记录。"""
    cold = {"baseline": cold_start_hits(baseline_records), "candidate": cold_start_hits(candidate_records)}
    contaminated = [side for side, stat in cold.items()
                    if stat["unexplained"] >= CONTAMINATION_MIN_HITS
                    and stat["unexplained"] / stat["eligible"] >= CONTAMINATION_THRESHOLD]
    result = {
        "baseline": public_rate(rate_stat(base["hits"], base["eligible"])),
        "candidate": public_rate(rate_stat(cand["hits"], cand["eligible"])),
        "baseline_no_usage": base["no_usage"],
        "candidate_no_usage": cand["no_usage"],
        "baseline_total": base["total"],
        "candidate_total": cand["total"],
        "delta_pct": None,
        "p_value": None,
        "attribution": ATTRIBUTION_UNKNOWN,
        "note": "",
        "cold_start": cold,
        "contamination_suspected": bool(contaminated),
    }
    if not base["eligible"] or not cand["eligible"]:
        result["note"] = ("至少一侧没有请求返回 usage 统计，缓存命中率不可比；"
                          "这通常说明该网关不支持 stream_options.include_usage，"
                          "而不是缓存没有命中。")
        return result
    if base["no_usage"] or cand["no_usage"]:
        result["note"] = (f"两侧分别有 {base['no_usage']} / {cand['no_usage']} 条请求没有 usage，"
                          "已从命中率分母中排除；排除比例过高时结论只代表能取到 usage 的那部分。")

    comparison = compare_rates(base["hits"], base["eligible"], cand["hits"], cand["eligible"])
    for key in ("delta_pct", "p_value", "ci_overlap", "attribution"):
        result[key] = comparison[key]
    usage_note = result["note"]
    result["note"] = (comparison["note"] + (" " + usage_note if usage_note else "")).strip()
    if contaminated:
        # 命中里混进了外部预热，差值不再只由路由决定：降级归因，而不是照常下结论。
        labels = {"baseline": "固定路由", "candidate": "智能路由"}
        detail = "、".join(f"{labels[side]} {cold[side]['unexplained']}/{cold[side]['eligible']}" for side in contaminated)
        result["attribution"] = ATTRIBUTION_WEAK
        result["note"] = (f"段首请求出现无法用同段抢跑解释的缓存命中（{detail}）：这些请求在本侧本次运行里"
                          "没有先行者，命中只能来自实验之外的预热，缓存命中率差值不能归因于路由。"
                          + (" " + usage_note if usage_note else "")).strip()
    return result


def percentile(values, ratio):
    """线性插值分位数；空输入返回 None。与 ``analyze.percentile`` 同口径。"""
    numbers = sorted(float(v) for v in values if isinstance(v, (int, float))
                     and not isinstance(v, bool) and math.isfinite(float(v)))
    if not numbers:
        return None
    if len(numbers) == 1:
        return numbers[0]
    position = (len(numbers) - 1) * float(ratio)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return numbers[int(position)]
    return numbers[low] + (numbers[high] - numbers[low]) * (position - low)


def compare_latency(baseline_values, candidate_values, ratio=0.95):
    """比较延迟分位数。延迟不报 p 值——分布形状未知，用区间会假装精确。"""
    base = percentile(baseline_values, ratio)
    cand = percentile(candidate_values, ratio)
    return _compare_latency_points(base, cand, ratio)


def _compare_latency_points(base, cand, ratio=0.95):
    """复用已算出的分位点；差值仍用未四舍五入的值计算。"""
    return {
        "ratio": ratio,
        "baseline_ms": None if base is None else round(base, 1),
        "candidate_ms": None if cand is None else round(cand, 1),
        "delta_pct": (round((cand - base) / base * 100, 2)
                      if base not in (None, 0) and cand is not None else None),
        "note": "延迟只报分位点与差值方向；不同请求的耗时差异很大，不对分布做显著性断言。",
    }


def _record_ok(record):
    """沿用历史报告的完整响应口径，实时观测与最终报告共用。"""
    if record.get("ok") is not None:
        return bool(record.get("ok"))
    cls = record.get("class") or record.get("cls")
    if cls is not None:
        return cls == "ok"
    status = record.get("status_code")
    return bool(status and 200 <= status < 300 and record.get("completed"))


def request_index(record):
    """读取清单序号；旧记录只从可验证的路由语料编号恢复，不推测完成顺序。"""
    for field in ("index", "request_index"):
        value = record.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    item_id = record.get("item_id")
    match = re.fullmatch(r"routing-ab-v[0-9]+-([0-9]{6,12})", item_id) if isinstance(item_id, str) else None
    return int(match.group(1)) if match else None


def ordered_records(records):
    """按清单序号排列；未知序号放末尾，不能据其位置统计连续性。"""
    def order_key(record):
        index = request_index(record)
        return (index is None, index if index is not None else 0)
    return sorted(records, key=order_key)


def stability_protocol():
    """返回独立的统计口径说明，供实时、最终报告与界面共用。"""
    return {
        "version": STABILITY_PROTOCOL_VERSION,
        "order_by": "index",
        "failure_basis": "same_as_success_rate",
        "missing_result_policy": "break_sequence",
        "unordered_result_policy": "excluded",
        "duplicate_index_policy": "excluded",
        "ttft_ewma": {
            "window_size": TTFT_EWMA_WINDOW,
            "alpha": TTFT_EWMA_ALPHA,
            "seed": "first_sample",
            "max_basis": "final_value_of_each_complete_window",
            "sample_basis": "consecutive_successful_requests_with_finite_nonnegative_ttft",
        },
    }


def sequence_stability(records):
    """按清单序号算连续失败与 5 次首字耗时平滑峰值，不跨越尚未返回的请求。

    并发结果按完成顺序落盘，因此必须先恢复请求顺序。未知编号与重复编号不具备
    唯一的请求位置，不能拿落盘位置冒充清单位置；重复编号的全部结果均排除。
    每个窗口的平滑值从首个有效样本初始化，只比较第 5 次递推后的最终值。
    """
    indexed = [(request_index(record), record) for record in records]
    index_counts = Counter(index for index, _ in indexed if index is not None)
    ordered = sorted((index, record) for index, record in indexed
                     if index is not None and index_counts[index] == 1)
    unordered_count = sum(index is None for index, _ in indexed)
    duplicate_count = sum(count for count in index_counts.values() if count > 1)
    previous_index = None
    consecutive_failures = max_failures = eligible_ttft = window_count = 0
    ttft_window = deque(maxlen=TTFT_EWMA_WINDOW)
    maximum_ewma = None
    for index, record in ordered:
        if previous_index is None or index != previous_index + 1:
            consecutive_failures = 0
            ttft_window.clear()
        previous_index = index
        if not _record_ok(record):
            consecutive_failures += 1
            max_failures = max(max_failures, consecutive_failures)
            ttft_window.clear()
            continue
        consecutive_failures = 0
        ttft = record.get("ttft_ms")
        if not isinstance(ttft, (int, float)) or isinstance(ttft, bool):
            ttft_window.clear()
            continue
        try:
            ttft = float(ttft)
        except OverflowError:
            ttft_window.clear()
            continue
        if not math.isfinite(ttft) or ttft < 0:
            ttft_window.clear()
            continue
        eligible_ttft += 1
        ttft_window.append(ttft)
        if len(ttft_window) < TTFT_EWMA_WINDOW:
            continue
        window_count += 1
        smoothed = ttft_window[0]
        for sample in list(ttft_window)[1:]:
            smoothed = TTFT_EWMA_ALPHA * sample + (1 - TTFT_EWMA_ALPHA) * smoothed
        maximum_ewma = smoothed if maximum_ewma is None else max(maximum_ewma, smoothed)
    return {
        "max_consecutive_failures": max_failures if ordered else None,
        "ttft_ewma_max_ms": None if maximum_ewma is None else round(maximum_ewma, 1),
        "ordered_requests": len(ordered),
        "unordered_requests": unordered_count,
        "excluded_duplicate_requests": duplicate_count,
        "ttft_eligible_requests": eligible_ttft,
        "ttft_ewma_windows": window_count,
    }


def _stability_comparison(baseline, candidate):
    return {"baseline": baseline, "candidate": candidate, "protocol": stability_protocol(),
            "note": "按请求清单序号统计；未返回请求不作失败，编号间断打断连续性。"}


def response_diagnostics(records):
    """只汇总可验证的状态和失败类型；绝不返回请求地址、正文或异常详情。"""
    statuses, failures = Counter(), Counter()
    http_2xx = complete_2xx = completed_responses = 0
    known_types = {"TIMEOUT", "NETWORK", "PROTOCOL", "HTTP_429", "HTTP_5XX",
                   "HTTP_4XX", "HTTP_OTHER"}
    for record in records:
        status = record.get("status_code")
        if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
            status = None
        statuses[str(status) if status is not None else "no_status"] += 1
        is_2xx = status is not None and 200 <= status < 300
        completed = bool(record.get("completed"))
        http_2xx += int(is_2xx)
        complete_2xx += int(is_2xx and completed)
        completed_responses += int(completed)
        if _record_ok(record):
            continue
        error_type = record.get("error_type")
        if error_type not in known_types:
            if is_2xx:
                error_type = "PROTOCOL"
            elif status == 408:
                error_type = "TIMEOUT"
            elif status == 429:
                error_type = "HTTP_429"
            elif status is not None and status >= 500:
                error_type = "HTTP_5XX"
            elif status is not None and status >= 400:
                error_type = "HTTP_4XX"
            elif status is not None:
                error_type = "HTTP_OTHER"
            else:
                error_type = "UNKNOWN"
        failures[error_type] += 1
    return {
        "http_2xx": http_2xx,
        "complete_2xx": complete_2xx,
        "incomplete_2xx": http_2xx - complete_2xx,
        "completed_responses": completed_responses,
        "failures": sum(failures.values()),
        "failure_types": dict(sorted(failures.items())),
        "status_codes": dict(sorted(statuses.items())),
    }


def _summarize_side(records):
    """实时与最终报告共用侧统计，每条成功请求只收集一次延迟。"""
    successes = 0
    ttft_values, total_values = [], []
    for record in records:
        if _record_ok(record):
            successes += 1
            ttft_values.append(record.get("ttft_ms"))
            total_values.append(record.get("total_ms"))
    ttft = percentile(ttft_values, .95)
    total = percentile(total_values, .95)
    cache = cache_hit_rate(records)
    stability = sequence_stability(records)
    return {
        "ttft": ttft, "total": total, "cache_counts": cache,
        "public": {
            "requests": len(records),
            "success": public_rate(rate_stat(successes, len(records))),
            "cache": public_rate(rate_stat(cache["hits"], cache["eligible"])),
            "ttft_p95_ms": None if ttft is None else round(ttft, 1),
            "total_p95_ms": None if total is None else round(total, 1),
            "diagnostics": response_diagnostics(records),
            "max_consecutive_failures": stability["max_consecutive_failures"],
            "ttft_ewma_max_ms": stability["ttft_ewma_max_ms"],
            "stability": stability,
        },
    }


def build_live_comparison(baseline_records, candidate_records):
    """运行中已返回结果的描述统计；不检验显著性，也不作路由归因。

    两侧完成速度可能不同，因此各侧用自己的已返回结果作分母，未返回请求不算失败。
    保留最终报告常用的字段形状，便于界面展示同一组指标。
    """
    summaries = {side: _summarize_side(records) for side, records in (
        ("baseline", baseline_records), ("candidate", candidate_records))}
    sides = {side: summary["public"] for side, summary in summaries.items()}
    cache_counts = {side: summary["cache_counts"] for side, summary in summaries.items()}

    def observed_rate(metric):
        base, cand = sides["baseline"][metric], sides["candidate"][metric]
        base_rate = base["successes"] / base["total"] if base["total"] else None
        cand_rate = cand["successes"] / cand["total"] if cand["total"] else None
        return {"baseline": base, "candidate": cand,
                "delta_pct": (round((cand_rate - base_rate) * 100, 2)
                              if base_rate is not None and cand_rate is not None else None),
                "note": "按各侧已返回结果统计；运行中样本不完整，差值仅为当前观测。"}

    cache = observed_rate("cache")
    for side in ("baseline", "candidate"):
        cache[f"{side}_no_usage"] = cache_counts[side]["no_usage"]
        cache[f"{side}_total"] = cache_counts[side]["total"]
    if not cache_counts["baseline"]["eligible"] or not cache_counts["candidate"]["eligible"]:
        cache["note"] = "至少一侧尚无可用缓存统计，缓存命中率暂不可比；无用量记录不计为未命中。"
    return {
        **sides,
        "success": observed_rate("success"),
        "cache": cache,
        "ttft": _compare_latency_points(summaries["baseline"]["ttft"], summaries["candidate"]["ttft"]),
        "total_latency": _compare_latency_points(summaries["baseline"]["total"], summaries["candidate"]["total"]),
        "stability": _stability_comparison(sides["baseline"]["stability"], sides["candidate"]["stability"]),
        "method": {"statistics_protocol": STABILITY_PROTOCOL_VERSION},
    }


def build_comparison(baseline_records, candidate_records):
    """把一次 A/B 两侧的逐条结果汇总成报告数据。

    ``baseline_records`` / ``candidate_records`` 都是 ``smart_route_load`` 产出的
    结果字典序列，必须带 ``item_id`` 以便配对。
    """
    baseline_by_id = {r.get("item_id"): r for r in baseline_records if r.get("item_id")}
    candidate_by_id = {r.get("item_id"): r for r in candidate_records if r.get("item_id")}
    shared_ids = sorted(set(baseline_by_id) & set(candidate_by_id))

    pairs = [{"item_id": item_id,
              "baseline_ok": _record_ok(baseline_by_id[item_id]),
              "candidate_ok": _record_ok(candidate_by_id[item_id])} for item_id in shared_ids]

    base_summary = _summarize_side(baseline_records)
    cand_summary = _summarize_side(candidate_records)
    base_ok = base_summary["public"]["success"]["successes"]
    cand_ok = cand_summary["public"]["success"]["successes"]
    paired = pair_outcomes(pairs)
    mcnemar = mcnemar_exact(paired["baseline_only_ok"], paired["candidate_only_ok"])

    success = compare_rates(base_ok, len(baseline_records), cand_ok, len(candidate_records))
    cache = _compare_cache_counts(base_summary["cache_counts"], cand_summary["cache_counts"],
                                  baseline_records, candidate_records)
    base_stability = base_summary["public"]["stability"]
    cand_stability = cand_summary["public"]["stability"]

    reliability = {
        "paired_total": paired["paired_total"],
        "unpaired_count": paired["unpaired_count"],
        "unpaired_item_ids": paired["unpaired"][:20],
        "mcnemar": mcnemar,
    }
    if paired["unpaired_count"]:
        reliability["note"] = (f"有 {paired['unpaired_count']} 条请求只有一侧有结果，"
                               "已从配对分析中整条剔除；未配对的请求不能当作失败计入任何一侧。")
    else:
        reliability["note"] = "两侧请求清单完全一一对应。"

    # 只有配对样本足够时，McNemar 才配参与归因判断。
    if paired["paired_total"] >= 20 and mcnemar["discordant"] > 0 and mcnemar["p_value"] <= 0.05:
        success["mcnemar_supports"] = True
    elif paired["paired_total"] >= 20 and mcnemar["discordant"] > 0:
        success["mcnemar_supports"] = False
    else:
        success["mcnemar_supports"] = None

    limitations = [
        "本对照只说明本次清单、本次时段、本次并发下的黑盒观测，不等于长期稳定性证明。",
        "两侧共用同一批请求但缓存互相隔离；连接池和上游负载仍可能随时间漂移。",
        "缓存命中率只统计能取到 usage 的请求；命中只证明本次命中，不能证明缓存还能存活多久。",
    ]
    if success.get("attribution") != ATTRIBUTION_OK:
        limitations.insert(0, "成功率差异未通过归因判定：不要据此宣布智能路由更优或更差。")

    return {
        "baseline": base_summary["public"],
        "candidate": cand_summary["public"],
        "success": success,
        "cache": cache,
        "ttft": _compare_latency_points(base_summary["ttft"], cand_summary["ttft"]),
        "total_latency": _compare_latency_points(base_summary["total"], cand_summary["total"]),
        "stability": _stability_comparison(base_stability, cand_stability),
        "reliability": reliability,
        "metrics": [item["label"] for item in METRIC_LABELS],
        "method": {
            "pairs_by": "item_id",
            "interleaved": True,
            "confidence": "Wilson 95%",
            "test": "Fisher 精确检验 / McNemar 精确检验",
            "cache_basis": "usage.cached_tokens>0",
            "cache_isolation": "每侧每次运行独立前缀标记；段首命中用于检测外部预热",
            "statistics_protocol": STABILITY_PROTOCOL_VERSION,
        },
        "limitations": limitations,
    }
