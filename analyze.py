#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分析 JSONL 中客户端可观测的请求结果。"""
import argparse
import json
import math
from collections import Counter, defaultdict


def percentile(values, p):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    return values[max(0, min(len(values) - 1, int(p * len(values)) - 1))]


def pct(n, d):
    return f"{n * 100 / d:.2f}%" if d else "—"


def category(record):
    if record.get("completed") and 200 <= (record.get("status_code") or 0) < 300:
        return "成功"
    mapping = {
        "TIMEOUT": "超时", "NETWORK": "网络错误", "PROTOCOL": "响应不完整/协议异常",
        "HTTP_429": "HTTP 429", "HTTP_5XX": "HTTP 5xx",
        "HTTP_4XX": "其它 HTTP 4xx", "HTTP_OTHER": "其它 HTTP 状态",
    }
    return mapping.get(record.get("error_type"), "未知失败")


def cache_stats(records):
    """同时统计 Token（词元）命中率和请求命中率。

    请求命中率的主口径为 cached_tokens > 0 的请求数 ÷ 总请求数；Token（词元）
    命中率只在同时有 prompt_tokens 和 cached_tokens 时计算。没有 cached_tokens
    的请求计入请求命中率分母，并单独保留在 no_usage 计数中。
    """
    def cached_number(record):
        value = record.get("cached_tokens")
        if value is None or isinstance(value, bool):
            return None
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    def prompt_number(record):
        value = record.get("prompt_tokens")
        if value is None or isinstance(value, bool):
            return None
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) and value > 0 else None

    eligible_values = [cached_number(r) for r in records]
    eligible = [value for value in eligible_values if value is not None]
    hits = sum(value > 0 for value in eligible)
    token_pairs = [(cached, prompt_number(record)) for record, cached in zip(records, eligible_values)
                   if cached is not None and cached >= 0 and prompt_number(record) is not None]
    prompt_tokens = sum(prompt for _, prompt in token_pairs)
    cached_tokens = sum(cached for cached, _ in token_pairs)
    return {"eligible": len(eligible), "hits": hits,
            "total": len(records), "no_usage": len(records) - len(eligible),
            "token_eligible": len(token_pairs), "prompt_tokens": prompt_tokens,
            "cached_tokens": cached_tokens,
            "token_rate_pct": cached_tokens * 100 / prompt_tokens if prompt_tokens else None,
            "request_rate_pct": hits * 100 / len(records) if records else None}


def summarize(records, title):
    n = len(records)
    ok = sum(category(r) == "成功" for r in records)
    ttft = [r.get("ttft_ms") for r in records if r.get("ttft_ms") is not None]
    total = [r.get("total_ms") for r in records if r.get("total_ms") is not None]
    print(f"\n=== {title}（样本 {n}）===")
    print(f"请求成功率：{pct(ok, n)}（{ok}/{n}）")
    print(f"TTFT p50/p95（首字耗时）：{percentile(ttft, .5) or '—'} ms / {percentile(ttft, .95) or '—'} ms")
    print(f"总耗时 p50/p95：{percentile(total, .5) or '—'} ms / {percentile(total, .95) or '—'} ms")
    cache = cache_stats(records)
    if cache["token_eligible"]:
        print(f"缓存 Token（词元）命中率：{cache['token_rate_pct']:.2f}%"
              f"（缓存 {cache['cached_tokens']:.0f} / 输入 {cache['prompt_tokens']:.0f} Token（词元））")
    else:
        print("缓存 Token（词元）命中率：不可得（本批没有请求同时返回输入与缓存用量）")
    if cache["total"]:
        print(f"缓存命中请求率：{pct(cache['hits'], cache['total'])}"
              f"（{cache['hits']}/{cache['total']}，口径：命中请求数 / 总请求数）")
    if cache["no_usage"]:
        print(f"  其中 {cache['no_usage']}/{n} 条无 usage 数据，按请求命中率计为未命中")
    print("结果分布：")
    for name, count in Counter(category(r) for r in records).most_common():
        print(f"  {name:<18} {count:>7}  {pct(count, n)}")


def main():
    ap = argparse.ArgumentParser(description="分析网关黑盒压测 JSONL")
    ap.add_argument("jsonl")
    ap.add_argument("--by-step", action="store_true")
    ap.add_argument("--bucket", type=int, default=0)
    args = ap.parse_args()
    records = []
    bad = 0
    with open(args.jsonl, encoding="utf-8") as f:
        for line in f:
            try:
                records.append(json.loads(line))
            except (ValueError, TypeError):
                bad += 1
    if bad:
        print(f"警告：跳过 {bad} 条无效记录")
    if not records:
        print("文件为空或无有效记录。")
        return
    summarize(records, "整体")
    if args.by_step:
        groups = defaultdict(list)
        for record in records:
            groups[record.get("step_index")].append(record)
        for step in sorted(groups, key=lambda x: (x is None, x)):
            summarize(groups[step], f"第 {step} 档")
    if args.bucket:
        valid = [r for r in records if isinstance(r.get("ts"), (int, float))]
        if valid:
            t0 = min(r["ts"] for r in valid)
            groups = defaultdict(list)
            for record in valid:
                groups[int((record["ts"] - t0) / 1000 // args.bucket)].append(record)
            for index in sorted(groups):
                summarize(groups[index], f"{index * args.bucket}s~{(index + 1) * args.bucket}s")


if __name__ == "__main__":
    main()
