"""Reproducible, stateless behavioral fingerprint protocol and evidence analysis.

Probe battery and normalization adapted from ToseaAI/llm-fingerprint-detector
(MIT; see third_party/llm-fingerprint-detector.LICENSE). This is a separately
versioned implementation, not a claim of equivalence to the paper's harness.
No networking or filesystem writes occur in this module.
"""
from __future__ import annotations

from time_record import task_timing

import copy
import hashlib
import json
import math
import random
import re
import statistics
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

SOURCE_URL = "https://github.com/ToseaAI/llm-fingerprint-detector"
SOURCE_COMMIT = "94dc44686a553595581b6d06405c17f070df9b8d"
PAPER_URL = "https://arxiv.org/abs/2607.10252"
METHOD_VERSION = "workbench-one-word/1"
REASONING_METHOD_VERSION = "workbench-reasoning-one-word/1"
SUPPORTED_METHOD_VERSIONS = frozenset({METHOD_VERSION, REASONING_METHOD_VERSION})
NORMALIZER_VERSION = "workbench-normalizer/2"
REFERENCE_MAX_AGE_DAYS = 30
DEFAULT_CONCURRENCY = 20
DEFAULT_RPM = 100
MAX_CONCURRENCY = 20
MAX_RPM = 600000
USAGE_KEYS = ("prompt_tokens", "completion_tokens", "reasoning_tokens", "cached_tokens", "reported_cost")
USAGE_LABELS = {"prompt_tokens": "输入词元", "completion_tokens": "输出词元", "reasoning_tokens": "推理词元", "cached_tokens": "缓存词元", "reported_cost": "费用"}
SYSTEM_PROMPTS = {
    "en": "Answer with exactly one word. No punctuation, no explanation.",
    "zh": "只回答一个词，不要标点，不要解释。",
}
BATTERY = {
    "random-number-1-100": {"label": "随机数 1–100", "domain": {"kind": "int", "min": 1, "max": 100},
        "en": ["Name a random number between 1 and 100.", "Pick a random number from 1 to 100.", "Give me a random number between 1 and 100.", "Choose any number between 1 and 100 at random."],
        "zh": ["说一个 1 到 100 之间的随机数。", "随机挑一个 1 到 100 的数字。", "给我一个 1 至 100 之间的随机数字。", "从 1 到 100 里随便选一个数。"]},
    "random-number-1-10": {"label": "随机数 1–10", "domain": {"kind": "int", "min": 1, "max": 10},
        "en": ["Name a random number between 1 and 10.", "Pick a random number from 1 to 10.", "Give me a random number between 1 and 10."],
        "zh": ["说一个 1 到 10 之间的随机数。", "随机挑一个 1 到 10 的数字。", "从 1 到 10 里随便选一个数。"]},
    "random-letter": {"label": "随机字母", "domain": {"kind": "letter"},
        "en": ["Name a random letter of the alphabet.", "Pick a random letter from A to Z.", "Give me one random letter."],
        "zh": ["说一个随机的英文字母。", "从 A 到 Z 里随便挑一个字母。", "随机给我一个英文字母。"]},
    "random-color": {"label": "随机颜色", "domain": {"kind": "color"},
        "en": ["Name a random color.", "Pick a color at random.", "Give me one random color."],
        "zh": ["说一个随机的颜色。", "随便说一种颜色。", "随机挑一个颜色告诉我。"]},
    "coin-flip": {"label": "硬币正反面", "domain": {"kind": "coin"},
        "en": ["Flip a coin. Answer heads or tails.", "Toss a coin and tell me the result: heads or tails.", "Imagine flipping a coin. Which side came up, heads or tails?"],
        "zh": ["抛一枚硬币，回答正面还是反面。", "掷一次硬币，告诉我结果：正面或反面。", "想象抛硬币，落地是正面还是反面？"]},
    "random-animal": {"label": "随机动物", "domain": {"kind": "word"},
        "en": ["Name a random animal.", "Pick an animal at random.", "Give me one random animal."],
        "zh": ["说一个随机的动物。", "随便说一种动物。", "随机挑一个动物告诉我。"]},
    "random-city": {"label": "随机城市", "domain": {"kind": "word"},
        "en": ["Name a random city.", "Pick a city at random.", "Give me the name of one random city."],
        "zh": ["说一个随机的城市。", "随便说一座城市。", "随机挑一个城市告诉我。"]},
    "favorite-number": {"label": "偏好数字", "domain": {"kind": "int", "min": 0, "max": 10000},
        "en": ["What is your favorite number?", "Tell me your favourite number.", "If you had to pick a favorite number, what would it be?"],
        "zh": ["你最喜欢的数字是什么？", "说说你最爱的数字。", "如果必须选一个最喜欢的数字，你选哪个？"]},
}
CELL_PRIORITY_ORDER = [
    "random-number-1-100:en", "random-number-1-100:zh", "random-color:en", "random-animal:en",
    "random-number-1-10:en", "random-letter:en", "random-color:zh", "coin-flip:en",
    "favorite-number:en", "random-city:en", "random-number-1-10:zh", "coin-flip:zh",
    "random-letter:zh", "random-animal:zh", "random-city:zh", "favorite-number:zh",
]
PRESETS = [
    {"id": "quick", "label": "快速自检", "cells": 4, "samples_per_cell": 15},
    {"id": "standard", "label": "标准采集", "cells": 8, "samples_per_cell": 25},
    {"id": "strict", "label": "扩展采集", "cells": 16, "samples_per_cell": 25},
]
ADAPTERS = [
    {"id": "standard", "label": "标准兼容接口", "extra_body": {}, "token_field": "max_tokens"},
    {"id": "openrouter", "label": "OpenRouter（聚合接口）：关闭推理", "extra_body": {"reasoning": {"enabled": False}}, "token_field": "max_tokens"},
    {"id": "deepseek", "label": "DeepSeek（深度求索）：关闭思考", "extra_body": {"thinking": {"type": "disabled"}}, "token_field": "max_tokens"},
    {"id": "zhipu", "label": "智谱接口：关闭思考", "extra_body": {"thinking": {"type": "disabled"}}, "token_field": "max_tokens"},
    {"id": "openai", "label": "OpenAI（开放人工智能）兼容接口：不使用推理", "extra_body": {"reasoning_effort": "none"}, "token_field": "max_completion_tokens"},
    {"id": "openai_reasoning", "label": "OpenAI（开放人工智能）兼容接口：保留思考", "extra_body": {"reasoning_effort": "low"}, "token_field": "max_completion_tokens", "max_tokens": 4096, "temperature": None, "top_p": None, "method_version": REASONING_METHOD_VERSION},
    {"id": "deepseek_reasoning", "label": "DeepSeek（深度求索）：保留思考", "extra_body": {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}, "token_field": "max_tokens", "max_tokens": 4096, "temperature": None, "top_p": None, "method_version": REASONING_METHOD_VERSION},
    {"id": "zhipu_reasoning", "label": "智谱 GLM（通用语言模型）：保留思考", "extra_body": {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}, "token_field": "max_tokens", "max_tokens": 4096, "temperature": None, "top_p": None, "method_version": REASONING_METHOD_VERSION},
]


def _canonical_hash_value(value):
    """Normalize numerically identical JSON values across Python and browsers.

    JSON.parse/JSON.stringify serializes 1.0 as 1. All configurable protocol
    numbers are integral, so preserve their value rather than Python's numeric
    representation. Booleans remain distinct from numbers in the hash input.
    """
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _canonical_hash_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_hash_value(item) for item in value]
    return value


def _digest(value):
    canonical = _canonical_hash_value(value)
    return hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


BATTERY_HASH = _digest(BATTERY)


def cell_label(cell_id):
    task, lang = cell_id.rsplit(":", 1)
    return f"{BATTERY[task]['label']} · {'中文' if lang == 'zh' else '英文'}"


def catalog():
    return {
        "method_version": METHOD_VERSION, "supported_method_versions": sorted(SUPPORTED_METHOD_VERSIONS), "source_url": SOURCE_URL, "source_commit": SOURCE_COMMIT,
        "paper_url": PAPER_URL, "license": "MIT（宽松开源许可证）", "presets": copy.deepcopy(PRESETS),
        "adapters": [{"max_tokens": 16, "temperature": 1, "top_p": 1, "method_version": METHOD_VERSION, **copy.deepcopy(adapter)} for adapter in ADAPTERS], "reference_max_age_days": REFERENCE_MAX_AGE_DAYS,
        "limits": {"min_samples_per_cell": 10, "max_samples_per_cell": 100, "min_concurrency": 1, "max_concurrency": MAX_CONCURRENCY, "default_concurrency": DEFAULT_CONCURRENCY, "min_rpm": 1, "max_rpm": MAX_RPM, "default_rpm": DEFAULT_RPM, "min_timeout_seconds": 5, "max_timeout_seconds": 120, "max_tokens": 16, "temperature": 1.0},
        "notes": [
            "每次仅发送一条固定系统提示和一条探针问题；不带历史、记忆或工具。",
            "直接回答协议固定温度 1、输出上限 16；保留思考协议不传温度与核采样参数，推理与最终正文合计上限 4096。不会自动切换协议、重试或增大上限。",
            "保留思考协议有独立版本，只采集最终正文，可建立独立参考并查看探索性距离；不能与直接回答参考混比，也不套用其一致性判定阈值。",
            "0.25／0.35 为借鉴上游的启发式界线，未经本渠道校准；分布差异不能证明模型身份或供应商造假。",
            "分半自检异常只是复测线索；两半同分布仍可能包含稳定比例的混合后端。",
            "前缀计算缓存与完整响应重放不同；缓存用量仅用于延迟诊断，重复答案本身不能证明缓存。",
            "中文开放词域仅做确定性形式校验，不能保证回答在语义上确为单个动物或城市。",
            "参考有效期 30 天是本地工程策略，非论文给出的通用期限。外部历史参考仅供探索。",
        ],
        "probes": [{"cell_id": cell, "label": cell_label(cell), "language": cell.rsplit(":", 1)[1],
                    "system_prompt": SYSTEM_PROMPTS[cell.rsplit(":", 1)[1]],
                    "paraphrases": BATTERY[cell.rsplit(":", 1)[0]][cell.rsplit(":", 1)[1]]} for cell in CELL_PRIORITY_ORDER],
    }


def _integer(value, low, high, label):
    if isinstance(value, bool):
        raise ValueError(f"{label}必须为 {low}–{high} 的整数")
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f"{label}必须为 {low}–{high} 的整数") from None
    if not math.isfinite(number) or not number.is_integer() or not low <= number <= high:
        raise ValueError(f"{label}必须为 {low}–{high} 的整数")
    return int(number)


def make_protocol(config):
    if not isinstance(config, dict):
        raise ValueError("采集配置必须为对象")
    preset = next((x for x in PRESETS if x["id"] == config.get("preset", "standard")), None)
    adapter = next((x for x in ADAPTERS if x["id"] == config.get("adapter", "standard")), None)
    if preset is None or adapter is None:
        raise ValueError("采集方案或推理协议不受支持")
    protocol = {
        "version": adapter.get("method_version", METHOD_VERSION), "battery_hash": BATTERY_HASH, "normalizer_version": NORMALIZER_VERSION,
        "cells": CELL_PRIORITY_ORDER[:preset["cells"]],
        "samples_per_cell": _integer(config.get("samples_per_cell", preset["samples_per_cell"]), 10, 100, "每格样本数"),
        "concurrency": _integer(config.get("concurrency", DEFAULT_CONCURRENCY), 1, MAX_CONCURRENCY, "并发上限"),
        "rpm": _integer(config.get("rpm", DEFAULT_RPM), 1, MAX_RPM, "每分钟请求数"),
        "timeout_seconds": _integer(config.get("timeout_seconds", 30), 5, 120, "请求超时"),
        "temperature": adapter.get("temperature", 1), "top_p": adapter.get("top_p", 1), "max_tokens": adapter.get("max_tokens", 16), "max_retries": 0, "stream": False,
        "adapter": adapter["id"], "extra_body": copy.deepcopy(adapter["extra_body"]), "token_field": adapter["token_field"],
        "system_prompts": copy.deepcopy(SYSTEM_PROMPTS), "shuffle_version": "python-mt19937/1",
    }
    protocol["id"] = _digest(protocol)
    return protocol


def make_plan(protocol, seed):
    rng = random.Random(seed)
    plan = []
    for cell in protocol["cells"]:
        task, lang = cell.rsplit(":", 1)
        for repetition in range(protocol["samples_per_cell"]):
            index = rng.randrange(len(BATTERY[task][lang]))
            plan.append({"cell_id": cell, "paraphrase_index": index, "repetition_index": repetition,
                         "system_prompt": protocol["system_prompts"][lang], "user_prompt": BATTERY[task][lang][index]})
    rng.shuffle(plan)
    for index, item in enumerate(plan):
        item["index"] = index
    return plan


REFUSAL = re.compile(r"\bas an ai\b|\bi (?:cannot|can't|can not|won't|will not)\b|\bi'?m (?:unable|not able|sorry)\b|\bsorry[, ]|我不能|我无法|无法回答|不能回答|抱歉|对不起|作为(?:一个)?(?:AI|人工智能)", re.I)
EN_ONES = dict(zip("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split(), range(20)))
EN_TENS = dict(zip("twenty thirty forty fifty sixty seventy eighty ninety".split(), range(20, 100, 10)))
CN_DIGITS = dict(zip("零〇一二两三四五六七八九", [0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9]))
LETTER_NAMES = dict(zip("bee cee dee gee jay kay el ell em en oh pee cue queue ar es ess tee vee ex why zee zed".split(), "b c d g j k l l m n o p q q r s s t v x y z z".split()))


def _number(word):
    if re.fullmatch(r"[+-]?\d+", word):
        return int(word)
    if word in EN_ONES:
        return EN_ONES[word]
    if word in EN_TENS:
        return EN_TENS[word]
    if word in ("hundred", "onehundred"):
        return 100
    for tens, value in EN_TENS.items():
        if word.startswith(tens) and word[len(tens):] in EN_ONES and 1 <= EN_ONES[word[len(tens):]] <= 9:
            return value + EN_ONES[word[len(tens):]]
    if re.fullmatch(r"[零〇一二两三四五六七八九十百千]+", word):
        if not any(ch in "十百千" for ch in word):
            return int("".join(str(CN_DIGITS[ch]) for ch in word))
        total, current = 0, 0
        for ch in word:
            if ch in CN_DIGITS:
                current = CN_DIGITS[ch]
            else:
                total += (current or 1) * {"十": 10, "百": 100, "千": 1000}[ch]
                current = 0
        return total + current
    return None


def normalize_answer(raw, cell_id):
    if cell_id not in CELL_PRIORITY_ORDER:
        raise ValueError("未知探针单元")
    value = unicodedata.normalize("NFC", str(raw or "")).strip()
    result = {"normalized": None, "category": "empty", "extra_text": False}
    if not value:
        return result
    if REFUSAL.search(value):
        return {**result, "category": "refusal"}
    value = "".join(str(unicodedata.decimal(ch)) if ch.isdecimal() else ch for ch in value).lower()
    domain = BATTERY[cell_id.rsplit(":", 1)[0]]["domain"]
    # Preserve arithmetic signs/decimal notation rather than converting -7 to 7 or 4.2 to 42.
    if domain["kind"] == "int" and re.search(r"(?:[+−-]\s*\d|\d[.,．/\\]\d)", value):
        return {**result, "category": "invalid"}
    # Reject sentences before punctuation removal can concatenate an explanation.
    inner = value.strip(' \t\r\n"\'“”‘’`。.!！,，;；:：')
    if re.search(r"[，,。.!！?？;；:：\n\r]", inner) or re.search(r"因为|所以|因此|理由(?:是|为)|答案(?:是|为)|(?:我|本人)(?:选|选择|喜欢|觉得|认为|推荐)", inner):
        return {**result, "category": "invalid", "extra_text": True}
    cleaned = "".join(ch for ch in value if unicodedata.category(ch)[0] in "LN" or ch.isspace()).strip()
    if not cleaned:
        return result
    words = cleaned.split()
    word = words[0]
    result["extra_text"] = len(words) > 1
    kind = domain["kind"]
    if len(words) > 1 and (kind != "int" or _number("".join(words)) is None):
        return {**result, "category": "invalid", "extra_text": True}
    if kind == "int":
        # A spelled number is one semantic answer; any following explanation remains flagged.
        full = _number("".join(words))
        number = full if full is not None else _number(word)
        if number is not None and domain["min"] <= number <= domain["max"]:
            return {**result, "normalized": str(number), "category": "valid", "extra_text": result["extra_text"] and full is None}
    elif kind == "letter":
        mapped = LETTER_NAMES.get(word, word)
        if re.fullmatch("[a-z]", mapped):
            return {**result, "normalized": mapped, "category": "valid"}
    elif kind == "coin":
        if word in ("head", "heads", "正", "正面", "字"):
            return {**result, "normalized": "heads", "category": "valid"}
        if word in ("tail", "tails", "反", "反面", "花"):
            return {**result, "normalized": "tails", "category": "valid"}
    else:
        if kind == "color":
            word = {"grey": "gray", "aqua": "cyan"}.get(word, word)
            if re.fullmatch(r"[\u4e00-\u9fff]{2,}", word) and word.endswith("色"):
                word = word[:-1]
        if re.fullmatch(r"[a-z]{1,40}|[\u4e00-\u9fff]{1,20}", word):
            return {**result, "normalized": word, "category": "valid"}
    return {**result, "category": "invalid"}


def entropy_bits(counts):
    total = sum(counts.values())
    return max(0.0, -sum((n / total) * math.log2(n / total) for n in counts.values() if n > 0)) if total else 0.0


def jensen_shannon_divergence(a, b):
    if not a or not b or sum(a.values()) <= 0 or sum(b.values()) <= 0:
        return None
    na, nb = sum(a.values()), sum(b.values())
    value = 0.0
    for key in set(a) | set(b):
        p, q = a.get(key, 0) / na, b.get(key, 0) / nb
        middle = (p + q) / 2
        if p:
            value += p * math.log2(p / middle) / 2
        if q:
            value += q * math.log2(q / middle) / 2
    return min(1.0, max(0.0, value))


def _median(values):
    clean = [v for v in values if isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0]
    return statistics.median(clean) if clean else None


def split_half(samples):
    by_cell = defaultdict(list)
    for sample in samples:
        by_cell[sample["cell_id"]].append(sample)
    splits = {"alternating": [], "temporal": []}
    for rows in by_cell.values():
        ordered = sorted(rows, key=lambda x: x.get("index", 0))
        def timestamp(row):
            try:
                return _date(row.get("started_at")).timestamp()
            except (ValueError, OSError, OverflowError):
                return float(row.get("index", 0))
        temporal = sorted(rows, key=timestamp)
        halves = {
            "alternating": ([r for i, r in enumerate(ordered) if r.get("repetition_index", i) % 2 == 0], [r for i, r in enumerate(ordered) if r.get("repetition_index", i) % 2 == 1]),
            "temporal": (temporal[:len(temporal)//2], temporal[len(temporal)//2:]),
        }
        for kind, (left, right) in halves.items():
            a = Counter(x["normalized"] for x in left if x.get("category") == "valid" and x.get("normalized") is not None)
            b = Counter(x["normalized"] for x in right if x.get("category") == "valid" and x.get("normalized") is not None)
            if sum(a.values()) >= 5 and sum(b.values()) >= 5:
                splits[kind].append(jensen_shannon_divergence(a, b))
    summary = {kind: {"mean_jsd": statistics.mean(values) if values else None, "comparable_cells": len(values)} for kind, values in splits.items()}
    summary["warning"] = any(x["mean_jsd"] is not None and x["mean_jsd"] > .25 for x in summary.values())
    return summary


def _known_usage_values(rows, key):
    values = [(r.get("usage") or {}).get(key) for r in rows]
    return [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0 and (key == "reported_cost" or isinstance(v, int))]


def _usage_coverage_warnings(usage, coverage, total):
    warnings = []
    partial = [f"{USAGE_LABELS[key]} {coverage[key]}/{total} 次" for key in USAGE_KEYS if key != "reported_cost" and coverage[key] is not None and 0 < coverage[key] < total]
    if partial:
        warnings.append("部分用量字段未完整回报（" + "，".join(partial) + "）；展示数值仅为已报告部分之和。")
    cost_coverage = coverage["reported_cost"]
    if cost_coverage is not None and 0 < cost_coverage < total:
        warnings.append(f"费用仅有 {cost_coverage}/{total} 次请求报告；展示值是部分汇总，不能作为本次完整费用。")
    unknown = [USAGE_LABELS[key] for key in USAGE_KEYS if coverage[key] is None and usage[key] is not None]
    if unknown:
        warnings.append("未提供用量覆盖次数（" + "、".join(unknown) + "）；无法核实这些合计是否完整。")
    return warnings


def build_artifact(samples, protocol, endpoint, *, collected_at, seed, complete=True):
    cells = {}
    for cell in protocol["cells"]:
        rows = [x for x in samples if x["cell_id"] == cell]
        counts = Counter(x["normalized"] for x in rows if x.get("category") == "valid" and x.get("normalized") is not None)
        categories = Counter(x.get("category", "error") for x in rows)
        cells[cell] = {"counts": dict(counts), "valid_count": sum(counts.values()), "total_count": len(rows),
                       "entropy_bits": entropy_bits(counts), "median_latency_ms": _median([x.get("latency_ms") for x in rows if x.get("category") != "error"]),
                       **{kind + "_count": categories[kind] for kind in ("invalid", "refusal", "empty", "error")}}
    count = len(samples)
    cache_known = [x for x in samples if (x.get("usage") or {}).get("cached_tokens") is not None]
    cached = [x for x in cache_known if (x.get("usage") or {}).get("cached_tokens", 0) > 0]
    uncached = [x for x in cache_known if (x.get("usage") or {}).get("cached_tokens") == 0]
    echoed = Counter(str(x["returned_model"]) for x in samples if x.get("returned_model"))
    response_ids = [x["response_id"] for x in samples if x.get("response_id")]
    duplicate_ids = len(response_ids) - len(set(response_ids))
    known_usage = {key: _known_usage_values(samples, key) for key in USAGE_KEYS}
    def rate(predicate):
        return sum(bool(predicate(x)) for x in samples) / count if count else None
    diagnostics = {
        "valid_rate": rate(lambda x: x.get("category") == "valid"), "error_rate": rate(lambda x: x.get("category") == "error"),
        "empty_rate": rate(lambda x: x.get("category") == "empty"), "truncated_rate": rate(lambda x: x.get("truncated", False)),
        "extra_text_rate": rate(lambda x: x.get("extra_text", False)),
        "reasoning_observed": any((x.get("usage") or {}).get("reasoning_tokens", 0) or x.get("reasoning_observed") for x in samples),
        "returned_models": dict(echoed), "model_echo_mismatch_count": sum(n for model, n in echoed.items() if model != endpoint.get("model")),
        "duplicate_response_id_count": duplicate_ids, "split_half": split_half(samples),
        "token_budget_exceeded_count": sum(value > protocol["max_tokens"] for value in known_usage["completion_tokens"]),
        "cache": {"observed_samples": len(cache_known), "cached_samples": len(cached), "uncached_samples": len(uncached), "unknown_samples": count-len(cache_known),
                  "cached_median_latency_ms": _median([x.get("latency_ms") for x in cached]), "uncached_median_latency_ms": _median([x.get("latency_ms") for x in uncached])},
        "usage": {key: sum(values) if values else None for key, values in known_usage.items()},
        "usage_observed_samples": {key: len(values) for key, values in known_usage.items()},
        "warnings": [],
    }
    warnings = diagnostics["warnings"]
    if diagnostics["split_half"]["warning"]:
        warnings.append("分半自检差异较高；可能来自采样噪声、改写分配或通道变化，需增加样本复测。")
    if diagnostics["model_echo_mismatch_count"]:
        warnings.append("供应商回显模型名与请求名称不同；可能是别名映射，回显不能证明模型身份。")
    if duplicate_ids:
        warnings.append("存在重复响应标识；请排查响应重放或供应商标识策略，不能仅据此认定缓存。")
    if count and all(x.get("truncated") and not x.get("raw", "").strip() and x.get("reasoning_observed") for x in samples):
        warnings.append("所有已返回回答均为空且在推理阶段耗尽输出预算；请核对所选适配与固定输出上限，不要将本次结果用于参考对照。")
    if protocol.get("version") == REASONING_METHOD_VERSION:
        warnings.append("本次为保留思考的独立协议；仅最终正文进入分布，距离仅供探索，不沿用直接回答协议的一致性阈值。")
    if diagnostics["reasoning_observed"]:
        warnings.append("观测到推理内容或推理用量，与直接回答指纹不宜混比。")
    if diagnostics["token_budget_exceeded_count"]:
        warnings.append(f"有 {diagnostics['token_budget_exceeded_count']} 次请求回报的输出词元超过固定上限 {protocol['max_tokens']}；这是参数回报不一致线索，不能据此断言隐藏推理。")
    warnings.extend(_usage_coverage_warnings(diagnostics["usage"], diagnostics["usage_observed_samples"], count))
    if any(x["valid_count"] >= 10 and len(x["counts"]) == 1 for k, x in cells.items() if not k.startswith("coin-flip:")):
        warnings.append("部分单元答案高度集中；这可能是模型偏好，重复答案本身不能证明完整响应缓存。")
    return {
        "format": "workbench-fingerprint", "format_version": 1, "protocol": copy.deepcopy(protocol),
        "endpoint": {k: endpoint.get(k) for k in ("url", "model")}, "collected_at": collected_at,
        "complete": bool(complete), "seed": seed, "cells": cells, "diagnostics": diagnostics,
        "source": {"url": SOURCE_URL, "commit": SOURCE_COMMIT, "method_version": protocol["version"]},
    }


def _date(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("采集日期必须包含时区")
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, timezone.utc)
        except (ValueError, OverflowError, OSError):
            raise ValueError("采集日期无效") from None
    if not isinstance(value, str) or not value:
        raise ValueError("采集日期缺失或无效")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("采集日期无效") from None
    if result.tzinfo is None:
        raise ValueError("采集日期必须包含时区")
    return result.astimezone(timezone.utc)


def _protocol_valid(protocol):
    if not isinstance(protocol, dict) or protocol.get("version") not in SUPPORTED_METHOD_VERSIONS:
        return False
    expected = {k: v for k, v in protocol.items() if k != "id"}
    try:
        digest = _digest(expected)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return False
    if protocol.get("id") != digest:
        return False
    # Verify current exact supported manifest, not an attacker-chosen self-consistent digest.
    preset = next((x for x in PRESETS if CELL_PRIORITY_ORDER[:x["cells"]] == protocol.get("cells")), None)
    if preset is None:
        return False
    try:
        supported = make_protocol({"preset": preset["id"], **{k: protocol.get(k) for k in ("samples_per_cell", "concurrency", "timeout_seconds", "adapter")},
                                   **({"rpm": protocol["rpm"]} if "rpm" in protocol else {})})
    except ValueError:
        return False
    if "rpm" not in protocol:
        # Historical manifests used concurrency-only scheduling. Preserve their
        # exact identity and execution semantics; never silently add a rate.
        if supported["concurrency"] > 8 or protocol["version"] != METHOD_VERSION:
            return False
        del supported["rpm"]
        supported["id"] = _digest({k: v for k, v in supported.items() if k != "id"})
    return protocol == supported


def _validated_cells(cells):
    """Check categorical evidence before either import or offline comparison."""
    if not isinstance(cells, dict) or not 1 <= len(cells) <= 16:
        raise ValueError("指纹必须包含 1–16 个已知探针单元")
    clean_cells = {}
    for cell, value in cells.items():
        if cell not in CELL_PRIORITY_ORDER or not isinstance(value, dict):
            raise ValueError("指纹包含未知或无效的探针单元")
        counts = value.get("counts")
        if not isinstance(counts, dict) or len(counts) > 10000:
            raise ValueError("答案计数结构无效")
        for answer, number in counts.items():
            if not isinstance(answer, str) or not 1 <= len(answer) <= 80:
                raise ValueError("归一化答案无效")
            if isinstance(number, bool) or not isinstance(number, int) or not 0 <= number <= 100000:
                raise ValueError("答案计数必须为非负整数")
        categories = ("valid", "invalid", "refusal", "empty", "error")
        for category in categories:
            number = value.get(category + "_count", 0)
            if isinstance(number, bool) or not isinstance(number, int) or not 0 <= number <= 100000:
                raise ValueError("样本分类计数无效")
        if sum(counts.values()) != value.get("valid_count"):
            raise ValueError("有效样本数与答案计数之和不一致")
        total = value.get("total_count")
        if isinstance(total, bool) or not isinstance(total, int) or total != sum(value.get(c + "_count", 0) for c in categories):
            raise ValueError("样本总数与分类计数之和不一致")
        latency = value.get("median_latency_ms")
        if latency is not None and (isinstance(latency, bool) or not isinstance(latency, (int, float)) or not math.isfinite(latency) or latency < 0):
            raise ValueError("延迟必须为非负数或未知值")
        clean_cells[cell] = {"counts": dict(counts), "total_count": total,
                             **{c + "_count": value.get(c + "_count", 0) for c in categories},
                             "median_latency_ms": latency, "entropy_bits": entropy_bits(counts)}
    return clean_cells


def compare_artifacts(candidate, reference, *, now=None):
    if not isinstance(candidate, dict) or not isinstance(reference, dict):
        raise ValueError("待测与参考指纹必须为结构化对象")
    a, b = _validated_cells(candidate.get("cells")), _validated_cells(reference.get("cells"))
    pa, pb = candidate.get("protocol"), reference.get("protocol")
    expected = set(pa["cells"]) if _protocol_valid(pa) else set(a)
    entries = []
    for cell in sorted(set(a) & set(b)):
        if a[cell].get("valid_count", 0) < 10 or b[cell].get("valid_count", 0) < 10:
            continue
        distance = jensen_shannon_divergence(a[cell]["counts"], b[cell]["counts"])
        if distance is not None:
            entries.append({"cell_id": cell, "label": cell_label(cell), "jsd": distance,
                            "valid_candidate": a[cell]["valid_count"], "valid_reference": b[cell]["valid_count"]})
    entries.sort(key=lambda x: -x["jsd"])
    distance = statistics.mean(x["jsd"] for x in entries) if entries else None
    compatible = _protocol_valid(pa) and _protocol_valid(pb) and pa == pb
    reasons, warnings = [], []
    age = None
    try:
        instant = _date(now) if now is not None else datetime.now(timezone.utc)
        age = (instant - _date(reference.get("collected_at"))).total_seconds() / 86400
        candidate_age = (instant - _date(candidate.get("collected_at"))).total_seconds() / 86400
        if age < -5 / 1440 or candidate_age < -5 / 1440:
            reasons.append("采集日期在未来，无法核实参考时效。")
        if age > REFERENCE_MAX_AGE_DAYS or candidate_age > REFERENCE_MAX_AGE_DAYS:
            reasons.append("参考或待测指纹超过 30 天有效期，请重新采集；这是本地工程策略。")
    except (ValueError, OverflowError, OSError):
        reasons.append("缺少可信采集日期，无法核实参考时效。")
    if not compatible:
        reasons.insert(0, "采集协议不兼容或无法核实；距离仅供探索，不形成一致性判定。")
    if any(isinstance(p, dict) and p.get("version") == REASONING_METHOD_VERSION for p in (pa, pb)):
        reasons.append("保留思考协议的判定阈值尚未校准；仅展示探索性距离，不形成一致性判定。")
    verdict = "incomparable" if reasons else None
    needed = max(4, math.ceil(len(expected) * .8))
    if len(entries) < needed:
        reasons.append(f"仅 {len(entries)} 个单元可比，需要至少 {needed} 个，且每格双方均至少 10 个有效答案。")
        if verdict is None:
            verdict = "insufficient"
    for label, artifact, cells in (("待测", candidate, a), ("参考", reference, b)):
        diagnostics = _import_diagnostics(artifact.get("diagnostics", {}), cells)
        if not artifact.get("complete"):
            warnings.append(f"{label}采集未完整结束。")
        if diagnostics.get("reasoning_observed"):
            warnings.append(f"{label}观测到推理过程，不能按直接回答通道解释。")
        if diagnostics.get("token_budget_exceeded_count", 0):
            warnings.append(f"{label}回报的输出词元存在超过固定预算的请求；需复核参数执行情况，不据此断言隐藏推理。")
        if (diagnostics.get("valid_rate") or 0) < .8:
            warnings.append(f"{label}有效答案比例低于 80%。")
        if (diagnostics.get("error_rate") or 0) > .1:
            warnings.append(f"{label}请求错误比例超过 10%。")
        if (diagnostics.get("truncated_rate") or 0) > .05:
            warnings.append(f"{label}输出截断比例超过 5%。")
        if (diagnostics.get("extra_text_rate") or 0) > .2:
            warnings.append(f"{label}额外解释比例超过 20%，单词回答约束未稳定满足。")
        half = diagnostics.get("split_half") or {}
        if half.get("warning"):
            warnings.append(f"{label}分半自检差异较高，需独立复测。")
        elif any((half.get(k) or {}).get("comparable_cells", 0) < 4 for k in ("alternating", "temporal")):
            warnings.append(f"{label}分半自检的有效单元不足。")
        if diagnostics.get("duplicate_response_id_count", 0):
            warnings.append(f"{label}存在重复响应标识，需核查通道诊断。")
    if verdict is None:
        verdict = "uncertain" if warnings else "match" if distance <= .25 else "uncertain" if distance <= .35 else "mismatch"
    labels = {"match": "行为分布相近", "mismatch": "行为差异明显", "uncertain": "证据不明确，建议复测", "insufficient": "有效样本不足", "incomparable": "当前不可严格比较"}
    if not reasons:
        reasons.append("结论来自启发式分布距离，不是模型身份认证；提示注入、部署变化或混合路由均可能影响结果。")
    return {"mean_jsd": distance, "verdict": verdict, "label": labels[verdict], "reasons": reasons, "warnings": warnings,
            "cells": entries, "comparable_cells": len(entries), "required_cells": needed, "thresholds": {"match": .25, "mismatch": .35},
            "protocol_compatible": compatible, "reference_age_days": age}


def validate_import(data):
    if not isinstance(data, dict):
        raise ValueError("指纹文件必须为结构化对象")
    if "fingerprint" in data:
        if not isinstance(data["fingerprint"], dict):
            raise ValueError("外部指纹包装结构无效")
        data = data["fingerprint"]
    external = type(data.get("formatVersion")) is int and data.get("formatVersion") == 1 and isinstance(data.get("protocol"), str)
    if external:
        if not isinstance(data.get("cells"), dict) or not isinstance(data.get("meta", {}), dict):
            raise ValueError("外部指纹单元或来源结构无效")
        if not isinstance(data.get("postReasoning", False), bool):
            raise ValueError("外部推理观测状态无效")
        converted = {}
        for cell, value in (data.get("cells") or {}).items():
            if not isinstance(value, dict):
                raise ValueError("外部指纹单元结构无效")
            converted[cell] = {"counts": value.get("counts"), "valid_count": value.get("validCount"), "total_count": value.get("totalCount"),
                               **{key + "_count": value.get(key + "Count", 0) for key in ("invalid", "refusal", "empty", "error")},
                               "median_latency_ms": value.get("medianLatencyMs"), "entropy_bits": value.get("entropyBits")}
        data = {"format": "workbench-fingerprint", "format_version": 1, "protocol": {"version": data["protocol"], "external_protocol": True, "cells": list(converted)},
                "endpoint": {"model": data.get("model"), "url": (data.get("meta") or {}).get("channel")},
                "collected_at": data.get("collectedAt"), "complete": True, "cells": converted,
                "diagnostics": {"warnings": ["导入的外部参考缺少工作台完整协议与原始样本，仅供探索。"], "reasoning_observed": bool(data.get("postReasoning"))},
                "source": {"url": SOURCE_URL, "external_protocol": True}}
    if data.get("format") != "workbench-fingerprint" or type(data.get("format_version")) is not int or data.get("format_version") != 1:
        raise ValueError("不支持的指纹文件格式")
    try:
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
        encoded_size = len(encoded.encode())
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        raise ValueError("指纹包含非有限数值或非法结构") from None
    if encoded_size > 2 * 1024 * 1024:
        raise ValueError("指纹文件超过 2 MiB（兆二进制字节）")
    date = _date(data.get("collected_at"))
    if (date - datetime.now(timezone.utc)).total_seconds() > 300:
        raise ValueError("采集日期不能在未来")
    cells = data.get("cells")
    clean_cells = _validated_cells(cells)
    protocol = data.get("protocol")
    if not isinstance(protocol, dict):
        raise ValueError("缺少采集协议")
    if not isinstance(protocol.get("version"), str) or not protocol["version"]:
        raise ValueError("采集协议版本必须为非空字符串")
    if protocol.get("version") in SUPPORTED_METHOD_VERSIONS and not _protocol_valid(protocol):
        raise ValueError("工作台协议摘要不一致或字段被修改")
    if _protocol_valid(protocol):
        if set(cells) != set(protocol["cells"]):
            raise ValueError("探针单元与冻结协议不一致")
        if any(value["total_count"] > protocol["samples_per_cell"] for value in cells.values()):
            raise ValueError("样本数超过冻结协议的计划值")
        if data.get("complete") and any(value["total_count"] != protocol["samples_per_cell"] for value in cells.values()):
            raise ValueError("完整采集的样本数与冻结协议不一致")
        protocol = copy.deepcopy(protocol)
    else:
        protocol = {"version": str(protocol.get("version") or "unknown")[:160], "external_protocol": True, "cells": list(cells)}
    # Only whitelisted metadata persists; never import arbitrary nested credentials.
    endpoint, source = data.get("endpoint", {}), data.get("source", {})
    if not isinstance(endpoint, dict) or not isinstance(source, dict):
        raise ValueError("通道或来源结构无效")
    if any(value is not None and not isinstance(value, str) for value in (
        endpoint.get("url"), endpoint.get("model"), source.get("commit"),
        source.get("method_version"), source.get("source_note"),
    )):
        raise ValueError("通道和来源的文本字段必须为字符串")
    url = str(endpoint.get("url") or "")[:2048]
    try:
        parsed = urlsplit(url)
        url = urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", ""))
    except ValueError:
        raise ValueError("通道地址无效") from None
    diagnostics = _import_diagnostics(data.get("diagnostics", {}), clean_cells)
    if not isinstance(data.get("complete"), bool):
        raise ValueError("采集完成状态必须为布尔值")
    seed = data.get("seed")
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**64):
        raise ValueError("排程种子无效")
    return {"format": "workbench-fingerprint", "format_version": 1, "protocol": protocol,
            "endpoint": {"url": url, "model": str(endpoint.get("model") or "")[:200]},
            "collected_at": data["collected_at"], "complete": data["complete"], "seed": seed,
            **({"timing": task_timing(**data["timing"])} if isinstance(data.get("timing"), dict) else {}),
            "cells": clean_cells, "diagnostics": diagnostics,
            "source": {"url": SOURCE_URL, "commit": str(source.get("commit") or "")[:64],
                       "method_version": str(source.get("method_version") or "")[:160],
                       "trusted_source": source.get("trusted_source") is True,
                       "source_note": str(source.get("source_note") or "")[:2000],
                       "external_protocol": not _protocol_valid(protocol)}}


def _import_diagnostics(value, cells):
    """Validate diagnostic evidence and derive every fact supported by counts.

    Missing measurements remain unknown. Counts and flags, when supplied, have
    strict types so Python's bool/int equivalence cannot manufacture coverage.
    """
    if not isinstance(value, dict):
        raise ValueError("通道诊断结构无效")

    def number(item, high=None, integer=False, nullable=True):
        if item is None and nullable:
            return None
        valid_type = isinstance(item, (int, float)) and not isinstance(item, bool)
        try:
            finite = valid_type and math.isfinite(item)
        except OverflowError:
            finite = False
        if not finite or item < 0 or (high is not None and item > high) or (integer and not isinstance(item, int)):
            raise ValueError("通道诊断包含非法数值")
        return item

    def mapping(name):
        item = value.get(name, {})
        if not isinstance(item, dict):
            raise ValueError("通道诊断的子项必须为对象")
        return item

    total = sum(c["total_count"] for c in cells.values())
    result = {key: number(value.get(key), 1) for key in ("valid_rate", "error_rate", "empty_rate", "truncated_rate", "extra_text_rate")}
    for kind in ("valid", "error", "empty"):
        result[kind + "_rate"] = sum(c[kind + "_count"] for c in cells.values()) / total if total else None
    if not isinstance(value.get("reasoning_observed", False), bool):
        raise ValueError("推理观测状态无效")
    result["reasoning_observed"] = value.get("reasoning_observed", False)
    for key in ("model_echo_mismatch_count", "duplicate_response_id_count", "token_budget_exceeded_count"):
        result[key] = number(value.get(key, 0), total, integer=True, nullable=False)

    returned = mapping("returned_models")
    if len(returned) > 1600 or any(not isinstance(key, str) or not 1 <= len(key) <= 200 for key in returned):
        raise ValueError("模型回显结构无效")
    result["returned_models"] = {key: number(count, total, integer=True, nullable=False) for key, count in returned.items()}
    if sum(result["returned_models"].values()) > total:
        raise ValueError("模型回显计数超过请求总数")

    half = mapping("split_half")
    if not isinstance(half.get("warning", False), bool):
        raise ValueError("分半自检告警状态无效")
    result["split_half"] = {}
    possible_cells = sum(cell["valid_count"] >= 10 for cell in cells.values())
    for kind in ("alternating", "temporal"):
        item = half.get(kind, {})
        if not isinstance(item, dict):
            raise ValueError("分半自检结构无效")
        mean = number(item.get("mean_jsd"), 1)
        coverage = number(item.get("comparable_cells", 0), possible_cells, integer=True, nullable=False)
        if (mean is None) != (coverage == 0):
            raise ValueError("分半自检距离与有效单元数不一致")
        result["split_half"][kind] = {"mean_jsd": mean, "comparable_cells": coverage}
    result["split_half"]["warning"] = any(item["mean_jsd"] is not None and item["mean_jsd"] > .25 for item in result["split_half"].values())

    cache, usage = mapping("cache"), mapping("usage")
    result["cache"] = {
        key: number(cache.get(key, total if key == "unknown_samples" else 0), total, integer=True, nullable=False)
        for key in ("observed_samples", "cached_samples", "uncached_samples", "unknown_samples")
    }
    cached = result["cache"]
    if cached["observed_samples"] != cached["cached_samples"] + cached["uncached_samples"] or cached["observed_samples"] + cached["unknown_samples"] != total:
        raise ValueError("缓存分组计数与请求总数不一致")
    for group in ("cached", "uncached"):
        key = group + "_median_latency_ms"
        latency = number(cache.get(key))
        if cached[group + "_samples"] == 0 and latency is not None:
            raise ValueError("没有对应缓存样本，不能提供该组延迟")
        cached[key] = latency
    result["usage"] = {key: number(usage.get(key), integer=key != "reported_cost") for key in USAGE_KEYS}
    coverage = mapping("usage_observed_samples")
    result["usage_observed_samples"] = {key: number(coverage.get(key), total, integer=True) for key in USAGE_KEYS}
    for key, observed in result["usage_observed_samples"].items():
        amount = result["usage"][key]
        if observed is not None and ((observed == 0) != (amount is None)):
            raise ValueError("用量合计与报告覆盖次数不一致")
    output_observed = result["usage_observed_samples"]["completion_tokens"]
    if output_observed is not None and result["token_budget_exceeded_count"] > output_observed:
        raise ValueError("输出预算异常次数超过输出用量的报告次数")
    # Positive reported reasoning usage outweighs a contradictory summary flag.
    result["reasoning_observed"] = result["reasoning_observed"] or (result["usage"]["reasoning_tokens"] or 0) > 0
    warnings = value.get("warnings", [])
    if not isinstance(warnings, list) or len(warnings) > 100 or any(not isinstance(warning, str) for warning in warnings):
        raise ValueError("诊断说明结构无效")
    result["warnings"] = list(dict.fromkeys([warning[:1000] for warning in warnings] + _usage_coverage_warnings(result["usage"], result["usage_observed_samples"], total)))
    return result
