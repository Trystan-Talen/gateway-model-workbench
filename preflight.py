"""测试前风险预检：在创建任务之前回答“这次测试会发生什么”。

为什么需要它：
- 旧流程只校验字段合法性，用户看不到预计请求量、耗时、磁盘占用和并发压力。
- 压测工具的风险是外溢的（打到线上、被供应商限流、磁盘写满），
  必须在提交之前给出一个可读的预检卡片，而不是事后解释失败。

本模块保持纯函数：输入已校验的配置，输出可展示的估算；不做网络请求、不读磁盘。
"""
import ipaddress
import urllib.parse

# 单条结果行的经验占用，用于估算结果文件大小。原始记录开启后显著变大。
BASE_LINE_BYTES = 420
RAW_LINE_BYTES = 2600

# 公认的本地 / 回环主机名。命中即视为“非生产目标”。
_LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}

# 这些路径片段通常意味着“已经指向某个具体业务环境”，命中时提高一档风险。
_PROD_HINTS = ("prod", "production", "live", "online", "正式", "生产", "线上")


def _host_of(url):
    try:
        parsed = urllib.parse.urlparse(str(url or ""))
    except ValueError:
        return ""
    return (parsed.hostname or "").lower()


def classify_target(url):
    """判断目标地址属于本地、内网还是疑似生产域名。

    返回值用于两件事：风险分级，以及“是否需要在创建前二次确认”。
    注意：这里只做字符串层面的分类，不解析 DNS，也不代表目标是否真的可压测。
    """
    host = _host_of(url)
    if not host:
        return {"host": "", "kind": "unknown", "production_like": True,
                "reason": "无法解析目标主机名，请确认地址完整。"}
    if host in _LOCAL_HOSTNAMES or host.endswith(".localhost"):
        return {"host": host, "kind": "local", "production_like": False,
                "reason": "目标是本机地址，通常是本地联调或自建测试服务。"}
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if ip.is_loopback or ip.is_private or ip.is_link_local:
            return {"host": host, "kind": "private", "production_like": False,
                    "reason": "目标是内网保留地址段，通常是内部测试环境。"}
        return {"host": host, "kind": "public_ip", "production_like": True,
                "reason": "目标是公网 IP，无法判断是否为生产服务，请人工确认。"}
    lowered = host.replace("-", "").replace(".", "")
    if any(hint in lowered for hint in _PROD_HINTS):
        return {"host": host, "kind": "production_like", "production_like": True,
                "reason": "域名中包含生产环境常见字样，疑似线上服务。"}
    return {"host": host, "kind": "public_domain", "production_like": True,
            "reason": "目标是公网域名，无法确认是否为测试环境，请人工确认。"}


def _concurrency_ceiling(rpm, concurrency, avg_seconds):
    """并发上限是否足以支撑计划速率。不足时会形成排队，而不是更快完成。"""
    needed = float(rpm) / 60.0 * max(0.1, float(avg_seconds))
    return {
        "needed": needed,
        "configured": int(concurrency),
        # 实际可完成速率 = 并发 / 单条耗时；低于计划速率时无法兑现计划。
        "achievable_rpm": round(float(concurrency) * 60.0 / max(0.1, float(avg_seconds)), 1),
        "sufficient": needed <= float(concurrency) + 1e-9,
    }


def estimate_run(cfg, steps=None, *, line_bytes=None, raw_line_bytes=None):
    """估算一次网关压测的请求量、耗时、磁盘与风险等级。

    `cfg` 必须是 validate_config 的产物；`steps` 为 build_plan 的结果（可选）。
    """
    steps = steps or []
    planned_requests = int(sum(step.get("count", 0) for step in steps))
    if planned_requests <= 0:
        planned_requests = int(cfg.get("iterations") or 0)

    rpm = float(cfg.get("rpm") or 0)
    concurrency = int(cfg.get("concurrency") or 1)
    capture_raw = bool(cfg.get("capture_raw"))

    # 最短执行时间由计划速率决定；阶梯和长跑按各档时长求和。
    if steps:
        min_seconds = float(sum(step.get("duration", 0) for step in steps))
    else:
        min_seconds = planned_requests / (rpm / 60.0) if rpm > 0 else 0.0

    # 结果文件大小：原始记录开启后单行显著变大。
    per_line = int((raw_line_bytes if capture_raw else None) or (RAW_LINE_BYTES if capture_raw else (line_bytes or BASE_LINE_BYTES)))
    estimated_bytes = planned_requests * per_line

    # 单条平均耗时按读取超时的一半粗估，只为暴露并发不足，不作为性能预测。
    read_timeout = float(cfg.get("read_timeout") or 60)
    assumed_latency = max(1.0, read_timeout / 2.0)
    capacity = _concurrency_ceiling(rpm, concurrency, assumed_latency)

    target = classify_target(cfg.get("url"))

    findings = []
    if not capacity["sufficient"]:
        findings.append({
            "level": "warn",
            "code": "concurrency_below_plan",
            "title": "并发上限可能不足以兑现计划速率",
            "detail": f"按单条约 {assumed_latency:.0f} 秒估算，并发 {concurrency} 约可完成 "
                      f"{capacity['achievable_rpm']:.0f} RPM，低于计划的 {rpm:.0f} RPM；"
                      f"缺口会表现为排队和调度延迟，而不是更快完成。",
        })
    if estimated_bytes >= 2 * 1024 ** 3:
        findings.append({"level": "risk", "code": "disk_large", "title": "预计结果文件超过 2 GB",
                         "detail": "原始记录会显著放大磁盘占用，建议缩短时长或关闭原始记录后再执行。"})
    elif estimated_bytes >= 512 * 1024 ** 2:
        findings.append({"level": "warn", "code": "disk_notice", "title": "预计结果文件超过 512 MB",
                         "detail": "请确认磁盘剩余空间，并在完成后清理不再需要的结果文件。"})
    if rpm >= 6000:
        findings.append({"level": "warn", "code": "rpm_high", "title": "计划调度速率较高",
                         "detail": "高计划速率很容易触发供应商限流；建议先用阶梯模式确认拐点。"})
    if target["production_like"]:
        findings.append({"level": "risk", "code": "production_target", "title": "目标疑似生产环境",
                         "detail": target["reason"] + " 压测会产生真实负载并可能计费。"})
    if capture_raw:
        findings.append({"level": "info", "code": "capture_raw", "title": "已开启原始输入输出记录",
                         "detail": "结果文件会保存完整请求与响应正文，分享前必须检查是否含敏感数据。"})
    if cfg.get("insecure"):
        findings.append({"level": "warn", "code": "insecure_tls", "title": "已跳过 TLS 证书校验",
                         "detail": "该模式只应用于自签名证书的测试环境，不应在正式环境使用。"})
    if bool(cfg.get("stream")):
        findings.append({"level": "info", "code": "stream_on", "title": "流式请求已开启",
                         "detail": "TTFT 只统计到首段真实内容的时间；空内容分片不计入首字。"})

    level = risk_level(findings, production_like=target["production_like"])
    return {
        "planned_requests": planned_requests,
        "min_seconds": round(min_seconds, 1),
        "planned_rpm": round(rpm, 2),
        "concurrency": concurrency,
        "capacity": capacity,
        "estimated_bytes": estimated_bytes,
        "capture_raw": capture_raw,
        "target": target,
        "findings": findings,
        "level": level,
        # 生产类目标必须由用户显式确认后才能提交，前端据此拦截。
        "requires_confirmation": bool(target["production_like"]),
        "note": "估算基于配置与经验单行长度，不是对网关真实性能的预测；实际执行受上游限流和网络影响。",
    }


def estimate_quality_run(cfg, case_count=None):
    """估算一次质量评测的请求量、耗时与风险。

    质量评测与压测的关键差异：请求量固定为“题数 × 资源数”，不按时间发压；
    因此风险主要来自上游配额和时间成本，而不是并发压力。
    """
    resources = [r for r in (cfg.get("resources") or []) if not r.get("historical_reference")]
    cases = int(case_count if case_count is not None else (cfg.get("case_count") or 0))
    planned_requests = cases * max(1, len(resources))
    # 题目是逐条串行的：串行执行让时间成本随题数线性增长。
    assumed_latency = max(1.0, float((resources[0] if resources else {}).get("read_timeout") or 60) / 2.0)
    estimated_bytes = planned_requests * (RAW_LINE_BYTES if cfg.get("capture_raw") else 900)

    findings = []
    if cfg.get("capture_raw"):
        findings.append({"level": "info", "code": "capture_raw", "title": "已开启逐题原始记录",
                         "detail": "每道题的完整请求与响应都会落盘，结果文件显著变大。"})
    if cases < 100:
        findings.append({"level": "warn", "code": "small_suite", "title": "题量低于内部参考线",
                         "detail": f"当前 {cases} 题，低于进入业务复核所需的 100 题；结论只能描述本次样本。"})
    if len(resources) == 2 and (resources[0].get("url"), resources[0].get("model")) == (resources[1].get("url"), resources[1].get("model")):
        findings.append({"level": "warn", "code": "same_endpoint", "title": "两侧地址与模型名相同",
                         "detail": "这更可能是重复观测，而不是两个独立资源；对照结论没有意义。"})
    target = classify_target((resources[0] if resources else {}).get("url"))
    if target["production_like"]:
        findings.append({"level": "risk", "code": "production_target", "title": "目标疑似生产环境",
                         "detail": target["reason"] + " 评测会占用真实配额并可能计费。"})

    level = risk_level(findings, production_like=target["production_like"])
    return {
        "planned_requests": planned_requests,
        "min_seconds": round(planned_requests * assumed_latency, 1),
        "case_count": cases,
        "resource_count": len(resources),
        "estimated_bytes": estimated_bytes,
        "capture_raw": bool(cfg.get("capture_raw")),
        "target": target,
        "findings": findings,
        "level": level,
        "requires_confirmation": bool(target["production_like"]),
        "note": "质量评测逐题串行执行；时间与费用随题数线性增长，且不自动重试失败请求。",
    }


def risk_level(findings, *, production_like=False):
    """把预检发现归并成一个等级：risk > warn > info > ok。"""
    levels = {str(item.get("level") or "info") for item in findings or ()}
    if "risk" in levels:
        return "risk"
    if "warn" in levels:
        return "warn"
    if production_like:
        return "warn"
    if "info" in levels:
        return "info"
    return "ok"


def estimate_routing_run(cfg):
    """估算一次路由对照：同一份清单会分别打到两侧，请求量因此是两倍。

    两侧都要各自判定生产环境风险——只报一侧会让人以为「另一半是安全的」，
    而实际上两份负载是同时产生的。结果文件同理，两侧各落一个文件。
    """
    tier = int(cfg.get("tier") or 0)
    rpm = float(cfg.get("rpm") or 0)
    baseline = cfg.get("baseline") or {}
    candidate = cfg.get("candidate") or {}
    concurrency = int(baseline.get("concurrency") or 1)
    per_side = tier
    planned_requests = per_side * 2

    # 两侧交错发出，墙钟时间由「一侧的条数 ÷ 计划速率」决定，而不是条数之和。
    min_seconds = (per_side / (rpm / 60.0)) if rpm > 0 else 0.0
    estimated_bytes = planned_requests * (RAW_LINE_BYTES if cfg.get("capture_raw") else BASE_LINE_BYTES)

    assumed_latency = max(1.0, float(baseline.get("read_timeout") or 60) / 2.0)
    capacity = _concurrency_ceiling(rpm, concurrency, assumed_latency)

    base_target = classify_target(baseline.get("url"))
    cand_target = classify_target(candidate.get("url"))
    production_like = bool(base_target["production_like"] or cand_target["production_like"])

    findings = []
    if not capacity["sufficient"]:
        findings.append({
            "level": "warn",
            "code": "concurrency_below_plan",
            "title": "并发上限可能不足以兑现计划速率",
            "detail": f"按单条约 {assumed_latency:.0f} 秒估算，每侧并发 {concurrency} 约可完成 "
                      f"{capacity['achievable_rpm']:.0f} RPM，低于计划的 {rpm:.0f} RPM；"
                      f"两侧交错发出，缺口会同时压在两边的调度队列上。",
        })
    if estimated_bytes >= 2 * 1024 ** 3:
        findings.append({"level": "risk", "code": "disk_large", "title": "预计结果文件超过 2 GB",
                         "detail": "两侧各写一个结果文件，合计超过 2 GB；建议降低档位后再执行。"})
    elif estimated_bytes >= 512 * 1024 ** 2:
        findings.append({"level": "warn", "code": "disk_notice", "title": "预计结果文件超过 512 MB",
                         "detail": "请确认磁盘剩余空间；两侧结果文件在记录删除后才会一并清理。"})
    if rpm >= 6000:
        findings.append({"level": "warn", "code": "rpm_high", "title": "计划调度速率较高",
                         "detail": "两侧共享同一节奏，高计划速率更容易触发供应商限流，进而污染对照结论。"})
    for label, target in (("固定路由", base_target), ("智能路由", cand_target)):
        if target["production_like"]:
            findings.append({"level": "risk", "code": "production_target", "title": f"{label} 目标疑似生产环境",
                             "detail": target["reason"] + " 对照测试会产生真实负载并可能计费。"})
    base_endpoint = (str(baseline.get("url") or "").strip(), str(baseline.get("model") or "").strip())
    cand_endpoint = (str(candidate.get("url") or "").strip(), str(candidate.get("model") or "").strip())
    if base_endpoint == cand_endpoint:
        findings.append({"level": "warn", "code": "same_endpoint", "title": "两侧地址与模型名相同",
                         "detail": "这更可能是同一路由的重复观测；除非两侧 Key 背后的路由确实不同，否则对照结论没有意义。"})

    level = risk_level(findings, production_like=production_like)
    return {
        "planned_requests": planned_requests,
        "min_seconds": round(min_seconds, 1),
        "planned_rpm": round(rpm, 2),
        "concurrency": concurrency,
        "tier": tier,
        "capacity": capacity,
        "estimated_bytes": estimated_bytes,
        "capture_raw": bool(cfg.get("capture_raw")),
        "target": base_target if base_target["production_like"] else cand_target,
        "targets": {"baseline": base_target, "candidate": cand_target},
        "findings": findings,
        "level": level,
        "requires_confirmation": production_like,
        "note": "两侧共用同一份清单与节奏；这里的估算不预测智能路由能否更快，只描述这次运行会发生什么。",
    }


def preflight_payload(cfg, *, kind="run", steps=None, case_count=None):
    """按测试类型分派预检估算。新增类型时只改这里，调用方不必知道分支。"""
    if kind == "quality":
        return estimate_quality_run(cfg, case_count=case_count)
    if kind == "routing":
        return estimate_routing_run(cfg)
    return estimate_run(cfg, steps=steps)
