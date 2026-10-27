"""TPM 任务冻结配置和离线预览；本模块不会访问上游。"""
from __future__ import annotations

import copy
import math
from urllib.parse import urlsplit

from tpm_usage import worst_case_cost

MAX_RPS = 1000
MAX_PLANNED_REQUESTS = 250_000
MAX_CONCURRENCY = 8192
CONCURRENCY_HEADROOM = 1.5
CLOCK_EVIDENCE_TOLERANCE_MS = 5

DEFAULT_PRICES = {"input": 0.304, "output": 1.064, "cache_read": 0.0874, "cache_write": None}
DEFAULTS = {
    "name": "2 亿 TPM（完整词元/分钟）摸底", "url": "", "model": "", "api_key": "",
    "prices": DEFAULT_PRICES.copy(), "budget_usd": 80, "target_tpm": 200_000_000,
    "input_tokens": 49_500, "expected_output_tokens": 500, "max_output_tokens": 1_024,
    "timeout_seconds": 120, "calibration_requests": 20, "cache_mode": "prefer_cache",
    "template_groups": 1, "warmup_requests": 1, "concurrency": 2048, "pool_connections": 2048,
    "execution_mode": "auto", "planning_latency_seconds": 30,
    "target_duration_seconds": 90, "ramp_duration_seconds": 20,
    "context_window_tokens": 65_536, "protocol_overhead_tokens": 128,
    "output_limit_parameter": "max_tokens", "usage_semantics": "openai",
    "usage_semantics_confirmed": False, "cache_write_price_mode": "unconfirmed",
    "seed": "tpm-synthetic-v1", "min_success_rate": .99, "near_target_ratio": .95,
    "max_missed_ratio": .01, "expected_cache_hit_rate": .95,
    "cache_hit_stop_threshold": .5, "max_backlog_growth_ratio": .1,
    "max_latency_degradation_ratio": 2.0,
}


def _number(raw, key, default, low, high, integer=False):
    value = raw.get(key, default)
    if isinstance(value, bool):
        raise ValueError(f"{key}（配置字段）必须是数值")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{key}（配置字段）必须是数值") from None
    if not math.isfinite(number) or not low <= number <= high or (integer and int(number) != number):
        raise ValueError(f"{key}（配置字段）须在 {low} 至 {high} 之间" + ("且为整数" if integer else ""))
    return int(number) if integer else number


def validate_config(raw, require_secret=True):
    if not isinstance(raw, dict):
        raise ValueError("配置必须是对象")
    cfg = copy.deepcopy(DEFAULTS)
    # Older callers explicitly supplied capacities; preserve their manual intent.
    cfg["execution_mode"] = raw.get("execution_mode", "manual" if any(
        key in raw for key in ("concurrency", "pool_connections")) else "auto")
    if cfg["execution_mode"] not in ("auto", "manual"):
        raise ValueError("execution_mode（执行规划模式）不受支持")
    for key in ("name", "url", "model", "connection_id", "api_key", "seed"):
        if key in raw:
            if not isinstance(raw[key], str):
                raise ValueError(f"{key}（配置字段）必须为文本")
            cfg[key] = raw[key].strip()
    if "url" not in raw:
        cfg["url"] = str(raw.get("base_url") or "").strip()
    cfg["url"] = cfg["url"].rstrip("/")
    try:
        parsed = urlsplit(cfg["url"])
        port = parsed.port
    except ValueError:
        raise ValueError("接口地址无效") from None
    if (require_secret or cfg["url"]) and (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or any(ord(c) < 33 for c in cfg["url"])):
        raise ValueError("接口地址须为 HTTP(S)（网页传输协议）地址，不能包含凭据、查询串或片段")
    if len(cfg["url"]) > 2048 or port == 0:
        raise ValueError("接口地址无效")
    if (require_secret and not cfg.get("model")) or len(cfg["model"]) > 256:
        raise ValueError("请指定一个模型（最多 256 个字符）")
    if require_secret and not cfg.get("api_key"):
        raise ValueError("请配置 API Key（接口密钥）")
    if len(cfg.get("api_key", "")) > 8192 or any(c in cfg.get("api_key", "") for c in "\r\n"):
        raise ValueError("接口密钥格式无效")
    if not cfg["name"] or len(cfg["name"]) > 120 or not cfg["seed"] or len(cfg["seed"]) > 256:
        raise ValueError("任务名称或语料种子长度无效")

    integer_ranges = {"input_tokens": (64, 1_000_000), "expected_output_tokens": (1, 16_384),
                      "max_output_tokens": (1, 32_768), "calibration_requests": (2, 100),
                      "template_groups": (1, 128), "warmup_requests": (0, 20),
                      "concurrency": (1, MAX_CONCURRENCY), "pool_connections": (1, MAX_CONCURRENCY),
                      "context_window_tokens": (256, 2_000_000), "protocol_overhead_tokens": (0, 4096)}
    float_ranges = {"target_tpm": (1, 1_000_000_000), "timeout_seconds": (.05, 600),
                    "planning_latency_seconds": (.001, 600),
                    "target_duration_seconds": (.05, 3600), "ramp_duration_seconds": (.05, 600),
                    "min_success_rate": (.5, 1), "near_target_ratio": (.5, .999999),
                    "max_missed_ratio": (0, .5), "expected_cache_hit_rate": (0, 1),
                    "cache_hit_stop_threshold": (0, 1), "max_backlog_growth_ratio": (0, 1),
                    "max_latency_degradation_ratio": (1, 10)}
    for key, bounds in integer_ranges.items():
        if cfg["execution_mode"] == "auto" and key in ("concurrency", "pool_connections"):
            continue
        cfg[key] = _number(raw, key, DEFAULTS[key], *bounds, integer=True)
    for key, bounds in float_ranges.items():
        cfg[key] = _number(raw, key, DEFAULTS[key], *bounds)
    for key, choices in {
        "cache_mode": ("prefer_cache", "avoid_cache"),
        "usage_semantics": ("openai", "independent"),
        "cache_write_price_mode": ("unconfirmed", "input", "explicit"),
        "output_limit_parameter": ("max_tokens", "max_completion_tokens"),
    }.items():
        cfg[key] = raw.get(key, DEFAULTS[key])
        if cfg[key] not in choices:
            raise ValueError(f"{key}（配置字段）不受支持")
    cfg["usage_semantics_confirmed"] = raw.get("usage_semantics_confirmed") is True
    if require_secret and cfg["usage_semantics"] == "independent" and not cfg["usage_semantics_confirmed"]:
        raise ValueError("独立缓存协议须确认普通输入不包含缓存读取与写入")
    prices = raw.get("prices", {})
    if not isinstance(prices, dict):
        raise ValueError("价格配置必须是对象")
    cfg["prices"] = {}
    for key, default in DEFAULT_PRICES.items():
        value = prices.get(key, default)
        cfg["prices"][key] = None if value is None or value == "" else _number({key: value}, key, default, 0, 10000)
    if any(cfg["prices"][key] is None for key in ("input", "output", "cache_read")):
        raise ValueError("请填写普通输入、输出、缓存读取报价")
    if cfg["cache_write_price_mode"] == "input":
        cfg["prices"]["cache_write"] = cfg["prices"]["input"]
    elif cfg["cache_write_price_mode"] == "unconfirmed":
        cfg["prices"]["cache_write"] = None
    elif cfg["prices"]["cache_write"] is None:
        raise ValueError("请明确填写缓存写入报价")
    if require_secret and cfg["prices"]["cache_write"] is None:
        raise ValueError("校准会收费；请先确认缓存写入报价，或明确按普通输入价计算")
    cfg["budget_usd"] = _number(raw, "budget_usd", 80 if cfg["cache_mode"] == "prefer_cache" else 160, .001, 100000)
    if cfg["expected_output_tokens"] > cfg["max_output_tokens"]:
        raise ValueError("期望输出不能大于输出硬上限")
    if cfg["input_tokens"] + cfg["max_output_tokens"] + cfg["protocol_overhead_tokens"] > cfg["context_window_tokens"]:
        raise ValueError("模型上下文不足以容纳目标完整输入、输出硬上限与协议开销")
    if cfg["execution_mode"] == "manual" and cfg["pool_connections"] < cfg["concurrency"]:
        raise ValueError("连接池容量不能小于并发上限，请同时调整")
    if cfg["template_groups"] * cfg["input_tokens"] > 16_000_000:
        raise ValueError("预生成语料总量超过本机模块上限，请减少模板组数或输入长度")
    # 冻结明确保证的行为，不接受客户端传入重试或任意上游参数。
    cfg.update(stream=True, retries=0, upstream_retries="unknown", schema_version=1)
    execution = execution_plan(cfg)
    cfg.update(concurrency=execution["concurrency"], pool_connections=execution["pool_connections"])
    return cfg


def build_plan(config, actual_input=None, actual_output=None, p95=None):
    input_mean = float(actual_input if actual_input is not None else config["input_tokens"])
    output_mean = float(actual_output if actual_output is not None else config["expected_output_tokens"])
    total_mean = input_mean + output_mean
    if not math.isfinite(total_mean) or total_mean <= 0 or input_mean <= 0 or output_mean < 0:
        raise ValueError("校准后的每请求完整用量必须为有效正数")
    stages = []
    for index, fraction in enumerate((.25, .5, .75, 1), 1):
        target = config["target_tpm"] * fraction
        duration = config["target_duration_seconds"] if index == 4 else config["ramp_duration_seconds"]
        rps = target / total_mean / 60
        if rps > MAX_RPS:
            raise ValueError(f"单机计划速率超过 {MAX_RPS} 请求/秒上限，请降低目标或增加输入长度")
        stages.append({"index": index, "target_tpm": target, "duration_seconds": duration,
                       "rps": rps, "planned_requests": math.ceil(rps * duration - 1e-10),
                       "planned_tokens": target * duration / 60})
    if sum(stage["planned_requests"] for stage in stages) > MAX_PLANNED_REQUESTS:
        raise ValueError(f"单轮正式计划超过 {MAX_PLANNED_REQUESTS} 条请求上限，请降低目标或缩短时长")
    return stages


def execution_plan(config, calibration=None, stages=None):
    """Derive finite capacities without changing a frozen configuration or budget."""
    calibration = calibration or {}
    if stages is None:
        stages = build_plan(config, calibration.get("mean_input_tokens"), calibration.get("mean_output_tokens"))
    p95 = calibration.get("p95_seconds")
    measured = isinstance(p95, (int, float)) and not isinstance(p95, bool) and math.isfinite(p95) and p95 >= 0
    latency = float(p95) if measured else float(config.get("planning_latency_seconds", DEFAULTS["planning_latency_seconds"]))
    required = max(1, math.ceil(stages[-1]["rps"] * latency * CONCURRENCY_HEADROOM - 1e-10))
    mode = config.get("execution_mode", "manual")
    automatic = mode == "auto"
    concurrency = min(required, MAX_CONCURRENCY) if automatic else config["concurrency"]
    pool = concurrency if automatic else config["pool_connections"]
    return {"mode": mode, "source": "calibration_p95" if measured else "assumed_latency",
            "latency_seconds": latency, "safety_factor": CONCURRENCY_HEADROOM,
            "required_concurrency": required, "concurrency": concurrency, "pool_connections": pool,
            "resource_limit": MAX_CONCURRENCY, "capped": automatic and required > MAX_CONCURRENCY}


def preview(config, calibration=None):
    calibration = calibration or {}
    mean_input = calibration.get("mean_input_tokens", config["input_tokens"])
    mean_output = calibration.get("mean_output_tokens", config["expected_output_tokens"])
    mean_input = config["input_tokens"] if mean_input is None else mean_input
    mean_output = config["expected_output_tokens"] if mean_output is None else mean_output
    stages = build_plan(config, mean_input, mean_output)
    execution = execution_plan(config, calibration, stages)
    prices = dict(config["prices"])
    requests = sum(s["planned_requests"] for s in stages)
    warmup_count = config["template_groups"] * config["warmup_requests"] if config["cache_mode"] == "prefer_cache" else 0
    calibration_count = config["calibration_requests"]
    expected_cached = config["expected_cache_hit_rate"] if config["cache_mode"] == "prefer_cache" else 0
    measured_cached = calibration.get("cache_hit_rate")
    cached = measured_cached if isinstance(measured_cached, (int, float)) and not isinstance(measured_cached, bool) and math.isfinite(measured_cached) and 0 <= measured_cached <= 1 else expected_cached
    unit = {"input_tokens": mean_input * (1 - cached), "cache_read_tokens": mean_input * cached,
            "cache_write_tokens": 0, "output_tokens": mean_output}
    categories = ("mean_normal_input_tokens", "mean_cache_read_tokens", "mean_cache_write_tokens")
    if all(isinstance(calibration.get(key), (int, float)) and math.isfinite(calibration[key]) and calibration[key] >= 0 for key in categories):
        for key, category in zip(("input_tokens", "cache_read_tokens", "cache_write_tokens"), categories):
            unit[key] = calibration[category]
    # 估算均值允许小数，实际 usage 校验仍只接受整数。
    cost_per_request = sum(unit[key] * (prices[price] or 0) / 1e6 for key, price in
                           (("input_tokens", "input"), ("cache_read_tokens", "cache_read"),
                            ("cache_write_tokens", "cache_write"), ("output_tokens", "output")))
    # 校准/预热保守按全输入未命中估算；不假设首次缓存已命中。
    preparation_unit = (mean_input * prices["input"] + mean_output * prices["output"]) / 1e6
    input_reserve = config["context_window_tokens"] - config["max_output_tokens"]
    reserve = worst_case_cost(input_reserve, config["max_output_tokens"], prices)
    p95 = calibration.get("p95_seconds")
    p95 = float(p95) if isinstance(p95, (int, float)) and math.isfinite(p95) and p95 >= 0 else None
    required_concurrency = execution["required_concurrency"]
    suggested_duration = max(config["target_duration_seconds"], 60 + p95) if p95 is not None and p95 > 30 else config["target_duration_seconds"]
    warnings = ["校准、预热、正式请求和收尾均可能收费，预算保护依赖用户报价和可获得用量。",
                "完整 Token TPM（完整词元/分钟）按客户端成功结束时间统计，不保证等于商家计费或限流口径。",
                "缓存命中是估算条件；必须以实际用量验证，缓存结果不能外推为非缓存容量。",
                "正式发压不重试；网关内部重试未知。停止后已发请求继续结算，等待本身不额外计费。"]
    warnings.append(f"正式报告要求请求的墙钟与单调时间投影偏差不超过 {CLOCK_EVIDENCE_TOLERANCE_MS} 毫秒；证据缺失或超差时标为证据不足，不改写时间或放宽阈值。")
    if not calibration:
        warnings.append("尚未校准：输入输出与费用为目标估算，初次探测语料长度由实际用量校准，不能据此宣称实测吞吐。")
    if execution["source"] == "assumed_latency":
        warnings.append(f"执行容量暂按单请求耗时 {execution['latency_seconds']:g} 秒估算；这是规划假设，校准后按实测第 95 百分位和实际用量重新规划，并须确认正式计划。")
    if execution["capped"]:
        warnings.append(f"自动规划建议并发 {required_concurrency}，已受单机 {MAX_CONCURRENCY} 上限限制；当前发压端可能无法施加目标压力，不会自动增加预算或发送速率。")
    if config["cache_mode"] == "prefer_cache" and calibration and cached < config["cache_hit_stop_threshold"]:
        warnings.append("实测缓存读取比例低于保护阈值，正式费用已按实测比例重估；继续发压将触发低命中保护。")
    if prices["cache_write"] is None:
        warnings.append("缓存写入价格待确认，禁止发起收费校准。")
    if required_concurrency > execution["concurrency"] and not execution["capped"]:
        basis = "校准第 95 百分位" if execution["source"] == "calibration_p95" else "当前耗时假设"
        warnings.append(f"按{basis}建议并发至少 {required_concurrency}；手动容量可能导致错过时隙。")
    if suggested_duration > config["target_duration_seconds"]:
        warnings.append(f"校准第 95 百分位超过 30 秒，建议目标档至少 {suggested_duration:.1f} 秒；需重新估价并明确确认，当前时长不会自动延长。")
    estimated_formal = requests * cost_per_request
    calibration_cost = calibration_count * preparation_unit
    warmup_cost = warmup_count * preparation_unit
    body_bytes = calibration.get("request_body_bytes")
    if body_bytes is None:
        from tpm_corpus import Corpus
        body_bytes = Corpus(config).metadata()["body_bytes"]
    body_note = ("最终校准语料的实际请求体" if calibration else
                 "初次小样本探测的实际请求体；最终长输入字节数和上行带宽在校准后展示")
    return {"stages": stages, "total_planned_tokens": sum(s["planned_tokens"] for s in stages),
            "planned_requests": requests, "duration_seconds": sum(s["duration_seconds"] for s in stages),
            "target_rps": stages[-1]["rps"], "mean_input_tokens": mean_input,
            "mean_output_tokens": mean_output, "request_reserve_usd": reserve,
            "estimated_cost_usd": estimated_formal + calibration_cost + warmup_cost,
            "formal_estimated_cost_usd": estimated_formal, "calibration_cost_usd": calibration_cost,
            "warmup_cost_usd": warmup_cost,
            "worst_case_cost_usd": reserve * (requests + calibration_count + warmup_count) if reserve is not None else None,
            "required_concurrency": required_concurrency, "concurrency": execution["concurrency"],
            "pool_connections": execution["pool_connections"], "execution_plan": execution,
            "request_body_bytes": body_bytes, "request_body_note": body_note,
            "request_body_source": "calibration" if calibration else "initial_probe",
            "estimated_upload_mbps": body_bytes * stages[-1]["rps"] * 8 / 1e6 if calibration and body_bytes is not None else None,
            "suggested_target_duration_seconds": suggested_duration,
            "duration_change_requires_confirmation": suggested_duration > config["target_duration_seconds"],
            "prices": prices, "warnings": warnings, "requires_confirmation": True,
            "expected_cache_hit_rate": expected_cached, "estimated_cache_hit_rate": cached,
            "estimate_usage_basis": "calibration" if calibration else "configured_expectation",
            "criteria": {key: config[key] for key in ("min_success_rate", "near_target_ratio", "max_missed_ratio", "max_backlog_growth_ratio", "max_latency_degradation_ratio")},
            "time_evidence_policy": {"version": 1, "max_clock_offset_ms": CLOCK_EVIDENCE_TOLERANCE_MS, "missing_evidence": "insufficient_evidence"},
            "criteria_note": "严格达到须原始完整用量不低于目标；接近阈值仅标接近。完整 60 秒观察窗、成功率与发压完整率达标，且无持续积压或明显延迟恶化，才可判稳定。",
            "cost_basis": "正式校准后优先使用实测缓存比例与互斥分类均值，校准前采用预期比例；按用户美元/百万词元报价估算；最坏预留使用所有输入类别最高报价；收尾仅结算已发请求"}
