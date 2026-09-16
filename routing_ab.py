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
from collections import Counter


# 归因等级：本次观测能不能支撑「差异来自路由」这句话。
ATTRIBUTION_OK = "attributable"
ATTRIBUTION_WEAK = "not_attributable"
ATTRIBUTION_UNKNOWN = "insufficient_evidence"

# 报告里固定出现的指标口径说明。界面直接读这里，避免前后端各写一份、慢慢对不上。
METRIC_LABELS = [
    {"key": "success", "label": "成功率", "basis": "完整 2xx 响应 / 已收到结果"},
    {"key": "cache", "label": "缓存命中率", "basis": "usage.cached_tokens>0 / 返回了 usage 的请求"},
    {"key": "ttft", "label": "TTFT p95（首字耗时）", "basis": "仅统计成功请求"},
    {"key": "total_latency", "label": "总耗时 p95", "basis": "仅统计成功请求"},
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

    def _prob(x):
        return math.exp(_log_choose(col1, x) + _log_choose(total - col1, row1 - x)
                        - _log_choose(total, row1))

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
    eligible = [r for r in records if r.get("cached_tokens") is not None]
    hits = sum(1 for r in eligible if (r.get("cached_tokens") or 0) > 0)
    return {
        "hits": hits,
        "eligible": len(eligible),
        "total": len(records),
        "no_usage": len(records) - len(eligible),
    }


def compare_cache(baseline_records, candidate_records):
    """比较两侧缓存命中率，并把「无 usage」当作不可比而非未命中。"""
    base = cache_hit_rate(baseline_records)
    cand = cache_hit_rate(candidate_records)
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
    result["note"] = (comparison["note"] + (" " + result["note"] if result["note"] else "")).strip()
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
    return {
        "ratio": ratio,
        "baseline_ms": None if base is None else round(base, 1),
        "candidate_ms": None if cand is None else round(cand, 1),
        "delta_pct": (round((cand - base) / base * 100, 2)
                      if base not in (None, 0) and cand is not None else None),
        "note": "延迟只报分位点与差值方向；不同请求的耗时差异很大，不对分布做显著性断言。",
    }


def build_comparison(baseline_records, candidate_records):
    """把一次 A/B 两侧的逐条结果汇总成报告数据。

    ``baseline_records`` / ``candidate_records`` 都是 ``smart_route_load`` 产出的
    结果字典序列，必须带 ``item_id`` 以便配对。
    """
    def _ok(record):
        if record.get("ok") is not None:
            return bool(record.get("ok"))
        cls = record.get("class") or record.get("cls")
        if cls is not None:
            return cls == "ok"
        status = record.get("status_code")
        return bool(status and 200 <= status < 300 and record.get("completed"))

    baseline_by_id = {r.get("item_id"): r for r in baseline_records if r.get("item_id")}
    candidate_by_id = {r.get("item_id"): r for r in candidate_records if r.get("item_id")}
    shared_ids = sorted(set(baseline_by_id) & set(candidate_by_id))

    pairs = [{"item_id": item_id,
              "baseline_ok": _ok(baseline_by_id[item_id]),
              "candidate_ok": _ok(candidate_by_id[item_id])} for item_id in shared_ids]

    base_ok = sum(1 for r in baseline_records if _ok(r))
    cand_ok = sum(1 for r in candidate_records if _ok(r))
    paired = pair_outcomes(pairs)
    mcnemar = mcnemar_exact(paired["baseline_only_ok"], paired["candidate_only_ok"])

    base_ttft = [r.get("ttft_ms") for r in baseline_records if _ok(r)]
    cand_ttft = [r.get("ttft_ms") for r in candidate_records if _ok(r)]
    base_total = [r.get("total_ms") for r in baseline_records if _ok(r)]
    cand_total = [r.get("total_ms") for r in candidate_records if _ok(r)]

    success = compare_rates(base_ok, len(baseline_records), cand_ok, len(candidate_records))
    cache = compare_cache(baseline_records, candidate_records)

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
        "两侧共用同一批请求；但路由两侧的缓存状态、连接池和上游负载可能随时间漂移。",
        "缓存命中率只统计能取到 usage 的请求；命中只证明本次命中，不能证明缓存还能存活多久。",
    ]
    if success.get("attribution") != ATTRIBUTION_OK:
        limitations.insert(0, "成功率差异未通过归因判定：不要据此宣布智能路由更优或更差。")

    return {
        "baseline": {
            "requests": len(baseline_records),
            "success": public_rate(rate_stat(base_ok, len(baseline_records))),
            "ttft_p95_ms": None if percentile(base_ttft, .95) is None else round(percentile(base_ttft, .95), 1),
            "total_p95_ms": None if percentile(base_total, .95) is None else round(percentile(base_total, .95), 1),
            "cache": cache["baseline"],
        },
        "candidate": {
            "requests": len(candidate_records),
            "success": public_rate(rate_stat(cand_ok, len(candidate_records))),
            "ttft_p95_ms": None if percentile(cand_ttft, .95) is None else round(percentile(cand_ttft, .95), 1),
            "total_p95_ms": None if percentile(cand_total, .95) is None else round(percentile(cand_total, .95), 1),
            "cache": cache["candidate"],
        },
        "success": success,
        "cache": cache,
        "ttft": compare_latency(base_ttft, cand_ttft, .95),
        "total_latency": compare_latency(base_total, cand_total, .95),
        "reliability": reliability,
        "metrics": [item["label"] for item in METRIC_LABELS],
        "method": {
            "pairs_by": "item_id",
            "interleaved": True,
            "confidence": "Wilson 95%",
            "test": "Fisher 精确检验 / McNemar 精确检验",
            "cache_basis": "usage.cached_tokens>0",
        },
        "limitations": limitations,
    }
