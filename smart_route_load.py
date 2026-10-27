#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""黑盒网关请求执行器。

只记录客户端能够客观观察到的状态码、完成状态、首个正文耗时和总耗时；
不推断选路、回退或上游内部状态。
"""
from __future__ import annotations

import argparse
import asyncio
import codecs
import json
import math
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

from time_record import request_timing, task_timing

try:
    import httpx
except ImportError:
    sys.stderr.write("缺少依赖 httpx，请先运行：python3 -m pip install httpx\n")
    raise

MODEL_LIST_PATHS = ["/models", "/api/v1/models", "/v1/models"]
CHAT_PATH = "/v1/chat/completions"
DONE_SENTINEL = "[DONE]"
RAW_CAPTURE_LIMIT = int(os.environ.get("ROUTE_TEST_RAW_LIMIT", str(1024 * 1024)))
PARSE_BUFFER_LIMIT = 2 * 1024 * 1024
USAGE_RETRY_POLICY = "explicit-unsupported-v2"


SMOKE_PROMPTS = (
    "请用不超过三句话说明幂等性为什么对支付接口重要，并给出一个常见实现方式。",
    "某服务连续三分钟错误率上升但延迟正常，请列出两个优先排查方向。",
    "把‘系统目前有点慢’改写成一条可执行、可度量的故障描述。",
    "解释连接超时与读取超时的区别，每项只写一句话。",
    "给一个 JSON 接口设计三条最基础的输入校验规则，回答不超过六十字。",
    "用一句话说明为什么压测报告必须区分计划速率与完成吞吐。",
    "列出发布前最值得自动化检查的三个项目，只输出编号列表。",
    "一个接口返回 HTTP 200 但正文被截断，是否应计为成功？请简述理由。",
)

LONG_CONTEXT_PARAGRAPHS = (
    "项目背景：海岚零售正在把分散在门店、仓库和电商平台的库存查询能力合并为统一服务。旧系统按渠道分别维护库存，促销高峰时经常出现页面显示可售、实际无法出库的情况。新服务需要在不改变门店收银流程的前提下，逐步接管查询流量。",
    "业务边界：统一库存服务只负责读取、预占、释放和确认四类动作，不负责定价、优惠计算或配送路线规划。门店离线销售仍由本地系统暂存，恢复联网后以事件形式补报；补报事件必须携带门店编号、设备编号和本地递增序号。",
    "一致性要求：普通浏览允许五秒以内的最终一致性，提交订单前的库存确认必须读取权威数据。预占默认保留十五分钟，支付完成后转为正式扣减；取消订单、支付失败和超时关闭都要触发释放，并保证重复通知不会造成二次变更。",
    "流量特征：工作日午间请求量约为基线的两倍，周末晚间约为三倍，大型促销开始后的前十分钟可能达到十二倍。查询请求占总量的百分之九十二，写请求占百分之八，但写请求失败对用户和账务的影响明显更高。",
    "性能目标：查询接口在稳定负载下的总耗时 p95 应低于三百毫秒，预占接口 p95 应低于六百毫秒。任何一分钟窗口内，完整成功响应比例不得低于百分之九十九点九；限流、超时、网络失败和正文不完整必须分开统计。",
    "容量约束：第一阶段部署在两个可用区，每个区域最多开放六十个应用连接。数据库连接数不能随请求并发线性增长，缓存失效时也不能让所有请求同时回源，因此客户端、应用层和存储层都需要明确的背压与降级策略。",
    "观测要求：每个请求都要有全链路标识，服务端日志记录接口名、结果类别和耗时分段，但不得写入访问密钥或完整用户资料。压测工具只能把客户端实际看到的状态作为结论，不能根据响应文本猜测内部选路或上游回退。",
    "发布策略：迁移按百分之一、百分之十、百分之五十和全量四个阶段推进，每个阶段至少观察一个完整业务高峰。若库存差异率、写入失败率或数据库等待时间超过阈值，应停止扩量；回滚只切换读取入口，已经创建的预占仍由新服务处理完毕。",
    "异常场景：需要覆盖缓存不可用、单区域网络抖动、下游响应变慢、消息重复投递和时钟偏差。演练期间不能通过无限重试掩盖失败，重试必须限制次数、加入抖动，并在超出预算后返回可识别的错误。",
    "验收口径：报告同时展示计划调度速率、实际开始数量、实际完成吞吐、成功率、首字耗时和完整响应耗时。测试结束条件、未执行请求数量以及原始记录是否开启都要明确，避免把计划时长误写成真实运行时长。",
)


# 缓存压测使用一段固定且足够长的前缀；请求编号和变化内容只出现在末尾问题，
# 这样连续请求可以复用上游提示缓存。前缀不得包含时间戳、随机数或 request_index。
CACHE_PROMPT_PREFIX = (
    "缓存命中率专项压测。请把下面这份固定业务背景作为上下文，后续只回答末尾的问题。"
    "不要复述背景，不要根据问题改写背景，也不要在回答中输出内部请求编号。\n\n"
    + "\n\n".join(LONG_CONTEXT_PARAGRAPHS)
    + "\n\n补充背景：" + "\n\n".join(LONG_CONTEXT_PARAGRAPHS)
)

CACHE_QUESTIONS = (
    "问题一：请用一句话说明查询请求的性能目标。",
    "问题二：请列出库存预占超过时限后的一个处理动作。",
    "问题三：请指出发布扩量前需要观察的一项指标。",
    "问题四：请用不超过二十字说明为什么要限制重试次数。",
    "问题五：请回答统一库存服务负责的四类动作中任意两类。",
    "问题六：请给出一个缓存不可用时仍需满足的可靠性要求。",
    "问题七：请说明普通浏览允许的最终一致性时间。",
    "问题八：请列出一个可用于追踪请求的标识字段。",
)


def build_payload(model, profile, extra_messages, temperature, max_tokens, stream=True,
                  input_estimate_tokens=None, request_index=0):
    """构造可比较且不过度重复的测试请求。

    ``request_index`` 用于轮换题目并降低网关缓存对结果的干扰；长输入使用由不同段落
    组成的稳定语料，而不是重复同一句填充文本。
    """
    index = int(request_index or 0)
    if profile == "cache":
        # 固定前缀必须逐字节稳定；只替换末尾问题，确保缓存命中率可被真实观测。
        content = CACHE_PROMPT_PREFIX + "\n\n" + CACHE_QUESTIONS[index % len(CACHE_QUESTIONS)]
        default_max_tokens = 64
    elif profile == "long":
        shift = index % len(LONG_CONTEXT_PARAGRAPHS)
        paragraphs = LONG_CONTEXT_PARAGRAPHS[shift:] + LONG_CONTEXT_PARAGRAPHS[:shift]
        questions = (
            "请只列出文中三项性能或可靠性验收指标，每项不超过二十字。",
            "请概括迁移过程中最关键的三个停止扩量条件，只输出项目符号列表。",
            "请指出文中两项不能由黑盒压测客户端直接推断的内部信息，并简述原因。",
        )
        content = (f"长输入样本编号 L-{index:06d}。阅读以下项目资料：\n\n"
                   + "\n\n".join(paragraphs)
                   + f"\n\n任务：{questions[index % len(questions)]}")
        default_max_tokens = 128
    elif profile == "short":
        left = 17 + index % 73
        right = 11 + (index * 7) % 67
        content = (f"极短输出校验 S-{index:06d}：计算 {left}+{right}。"
                   "只输出阿拉伯数字结果，不要解释或添加标点。")
        default_max_tokens = 12
    else:
        prompt = SMOKE_PROMPTS[index % len(SMOKE_PROMPTS)]
        content = f"常规问答样本 Q-{index:06d}：{prompt}"
        default_max_tokens = 128
    messages = [{"role": "user", "content": content}]
    if extra_messages:
        messages = list(extra_messages) + messages
    payload = {"model": model, "messages": messages, "stream": bool(stream)}
    if temperature is not None:
        payload["temperature"] = temperature
    payload["max_tokens"] = default_max_tokens if max_tokens is None else max_tokens
    return payload


def _model_fetch_exception_label(exc):
    """把模型探测异常转成不泄露敏感信息、且用户可行动的提示。"""
    if isinstance(exc, httpx.ConnectTimeout):
        return "连接超时（目标服务未及时响应）"
    if isinstance(exc, httpx.ReadTimeout):
        return "读取超时（模型接口响应过慢）"
    if isinstance(exc, httpx.ConnectError):
        return "连接失败（目标服务未监听或地址不可达）"
    if isinstance(exc, httpx.HTTPError):
        return f"HTTP 客户端错误（{type(exc).__name__}）"
    return type(exc).__name__


def _model_list_candidates(base_url):
    # 用户可能填入根地址、/v1、/api/v1，甚至完整的 chat/completions 地址；
    # 先去掉聊天接口后缀，避免把 /models 拼到完整接口之后。
    base = str(base_url or "").strip().rstrip("/")
    # 只移除聊天接口的末级路径，保留 /v1 或 /api/v1 前缀；否则完整地址
    # https://host/v1/chat/completions 会错误退化成 https://host/models。
    if base.endswith("/chat/completions"):
        base = base[:-len("/chat/completions")].rstrip("/")
    candidates = [base + "/models"] + [base + path for path in MODEL_LIST_PATHS]
    return list(dict.fromkeys(candidates))


async def fetch_models_detailed(client, base_url, headers):
    """获取模型并保留每个候选地址的诊断结果。

    ``ok`` 表示至少有一个候选地址返回了可解析的 HTTP 200 响应；``models`` 为空
    时仍可能是接口成功但没有模型。错误信息只包含地址路径、状态码和异常类型，
    不回传响应正文，也不回传请求头，避免泄露 API Key（应用程序编程接口密钥）。
    """
    attempted = 0
    successful_responses = 0
    diagnostics = []
    for url in _model_list_candidates(base_url):
        attempted += 1
        path = urllib.parse.urlsplit(url).path or "/"
        try:
            resp = await client.get(url, headers=headers, timeout=10.0)
            status = resp.status_code
            if status != 200:
                diagnostics.append(f"{path} → HTTP {status}")
                continue
            successful_responses += 1
            try:
                models = _extract_model_ids(resp.json())
            except Exception:
                diagnostics.append(f"{path} → HTTP 200（响应不是有效 JSON）")
                continue
            clean = sorted(dict.fromkeys(models))
            if clean:
                return {"ok": True, "models": clean, "attempted": attempted, "status": 200, "error": None, "diagnostics": diagnostics}
            diagnostics.append(f"{path} → HTTP 200（没有可用模型）")
        except Exception as exc:
            # 只记录异常类型，不把异常正文带回 UI；正文可能包含 URL、鉴权或代理信息。
            diagnostics.append(f"{path} → {_model_fetch_exception_label(exc)}")
    detail = "；".join(diagnostics[-4:])
    if successful_responses:
        return {"ok": True, "models": [], "attempted": attempted, "status": 200,
                "error": f"模型列表接口请求成功，但没有返回可用模型。{detail}", "diagnostics": diagnostics}
    return {"ok": False, "models": [], "attempted": attempted, "status": None,
            "error": f"无法获取模型列表。请检查 URL 路径、API Key 和网关兼容性。{detail}",
            "diagnostics": diagnostics}


async def fetch_models(client, base_url, headers):
    return (await fetch_models_detailed(client, base_url, headers))["models"]


def _extract_model_ids(data):
    items = data
    if isinstance(data, dict):
        items = data.get("data", data.get("models", []))
    if not isinstance(items, list):
        return []
    result = []
    for item in items:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict) and isinstance(item.get("id"), str):
            result.append(item["id"])
    return result


def classify_failure(status_code, body_text="", header_text="", fallback_patterns=None,
                     fallback_keywords=None):
    """兼容旧调用签名，只返回客户端可验证的失败类型。"""
    if status_code in (None, 0):
        return "NETWORK", False, False
    if status_code == 408:
        return "TIMEOUT", False, False
    if status_code == 429:
        return "HTTP_429", False, False
    if 500 <= status_code < 600:
        return "HTTP_5XX", False, False
    if 400 <= status_code < 500:
        return "HTTP_4XX", False, False
    return "HTTP_OTHER", False, False


def _usage_options_rejected(status_code, body_text):
    """只识别上游明确拒绝流式用量参数的校验错误，不把限流或鉴权当作兼容问题。

    错误正文仅在内存中分类；调用方只得到布尔事实，不回显上游错误正文。
    """
    if status_code not in (400, 422):
        return False
    target_field = re.compile(r"\b(?:stream_options|include_usage)\b", re.IGNORECASE)

    def rejected(error):
        if isinstance(error, list):
            # 不把一个字段的未知错误与另一个字段的普通校验错误拼成降级证据。
            return any(rejected(item) for item in error)
        if isinstance(error, dict):
            nested = error.get("error") or error.get("detail") or error.get("errors")
            if nested is not None:
                return rejected(nested)
            field = error.get("param") or error.get("parameter") or error.get("loc")
            if field is not None and not target_field.search(json.dumps(field)):
                return False
            # 只看错误字段，响应中的请求回显不能成为兼容性证据。
            error = {key: error[key] for key in
                     ("message", "msg", "code", "type", "param", "parameter", "loc")
                     if key in error}
            detail = json.dumps(error, ensure_ascii=False).lower()
        else:
            detail = str(error or "").lower()
        if not target_field.search(detail):
            return False
        return any(marker in detail for marker in (
            "unsupported", "not supported", "does not support", "doesn't support",
            "unknown", "unrecognized", "unexpected", "not allowed", "not permitted",
            "extra_forbidden", "不支持", "未知字段", "不允许",
        ))

    try:
        parsed = json.loads(str(body_text or ""))
    except (TypeError, ValueError):
        parsed = body_text
    return rejected(parsed)


def should_retry_without_usage(result, include_usage, stream=True):
    """有明确兼容证据才允许去掉 usage 参数重试一次。"""
    return bool(include_usage and stream and not result.get("usage_seen")
                and result.get("status_code") in (400, 422)
                and result.get("_usage_options_rejected"))


def new_blank_result():
    """空白结果（schema v3）。

    v3 在 v2 之上只增加上游 usage 计量，用于计算缓存命中率与真实 Token 消耗：
    多厂商的原始字段必须先经过 ``_extract_usage`` 映射到统一字段名，任何未识别
    或缺失的值一律写入 ``None``——「没有数据」绝不能与「计为 0」混同。
    """
    return {
        "schema_version": 3,
        "ts": None,
        "mode": None,
        "profile": None,
        "item_id": None,
        "step_index": None,
        "grade": None,
        "status_code": None,
        "error_type": None,
        "error_detail": None,
        "ttft_ms": None,
        "total_ms": None,
        "timing": request_timing(),
        "completed": False,
        "reason": None,
        # 以下为 schema v3 新增的用量计量字段。
        "usage_seen": False,
        "usage_raw": None,
        "usage_source": None,
        "usage_level": "NONE",
        "prompt_tokens": None,
        "cached_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "cache_hit": None,
    }


def _first_int(container, keys):
    """按给定键序取第一个可解析为整数的值；全都没有则返回 None。"""
    if not isinstance(container, dict):
        return None
    for key in keys:
        if key in container:
            raw = container.get(key)
            if raw is None or isinstance(raw, bool):
                continue
            try:
                return int(raw)
            except (TypeError, ValueError):
                continue
    return None


def _extract_usage(obj):
    """把厂商 usage 载荷映射为统一字段名。

    已知差异：OpenAI 与多数兼容网关用 ``prompt_tokens_details.cached_tokens``；
    部分适配器直接给顶层 ``cache_read_input_tokens``。只做「跨网关取同一个量」，
    不增不减语义。命中判定沿用平台自身口径：``cached_tokens > 0``。

    返回 ``None`` 表示这个载荷里没有任何可用的 usage 统计；返回字典表示
    「确实收到了 usage 块」，其中缺失的单项仍为 ``None``。
    """
    if not isinstance(obj, dict):
        return None
    nested = obj.get("prompt_tokens_details")
    if not isinstance(nested, dict):
        nested = {}
    top_cached = _first_int(obj, (
        "cached_tokens", "cache_read_input_tokens", "cached_input_tokens",
        "prompt_cache_hit_tokens", "cache_read_tokens",
    ))
    nested_cached = _first_int(nested, ("cached_tokens", "cache_read_input_tokens"))
    cached = nested_cached if nested_cached is not None else top_cached
    prompt = _first_int(obj, ("prompt_tokens", "input_tokens"))
    completion = _first_int(obj, ("completion_tokens", "output_tokens"))
    total = _first_int(obj, ("total_tokens",))
    if prompt is None and completion is None and total is None and cached is None:
        return None
    return {
        "prompt_tokens": prompt,
        "cached_tokens": cached,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def _merge_usage(into, seen):
    """把后到的 usage 合并进累计值。

    流式响应里同一个请求可能既有块内 usage，又有 ``[DONE]`` 前的汇总 usage；
    汇总块通常字段更全（例如只有它带 ``prompt_tokens_details``）。逐字段用
    「非空覆盖非空」补齐，而不是整块覆盖，避免后到的残缺块抹掉先到的真实值。
    """
    if not isinstance(seen, dict):
        return into
    for key, value in seen.items():
        if value is not None:
            into[key] = value
    return into


def _content_and_finish(obj: Any) -> Tuple[str, bool]:
    """抽取兼容 OpenAI 响应中的正文和完成信号。"""
    if not isinstance(obj, dict):
        return "", False
    pieces = []
    finished = False
    choices = obj.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason") is not None:
                finished = True
            for container_key in ("delta", "message"):
                container = choice.get(container_key)
                if isinstance(container, dict):
                    content = container.get("content")
                    if isinstance(content, str):
                        pieces.append(content)
            if isinstance(choice.get("text"), str):
                pieces.append(choice["text"])
    if isinstance(obj.get("output_text"), str):
        pieces.append(obj["output_text"])
    return "".join(pieces), finished


def _parse_sse_data(payload: str) -> Tuple[str, bool, bool, bool]:
    """解析一个 data: 载荷；usage 单列在 ``_parse_sse_event`` 里取。

    返回 (正文, 完成信号, 是否为有效事件, 是否为上游错误对象)。
    上游错误对象必须被单独识别：HTTP 200 的流中途回 {"error": {...}} 时，
    若只当成「无效行」丢弃，会被后面的 [DONE] 掩盖成一次成功请求。
    """
    content, finished, valid, upstream_error, _usage = _parse_sse_event(payload)
    return content, finished, valid, upstream_error


def _parse_sse_event(payload: str):
    """解析一个 data: 载荷，额外返回其中的 usage 统计。

    返回 ``(正文, 完成信号, 是否有效事件, 是否上游错误, usage)``。保留
    ``_parse_sse_data`` 的四元组签名给已有调用方，避免破坏既有契约。
    """
    payload = payload.strip()
    if payload == DONE_SENTINEL:
        return "", True, True, False, None
    if not payload:
        return "", False, False, False, None
    try:
        obj = json.loads(payload)
    except Exception:
        # 真实厂商会把正文里的换行原样写进 JSON 字符串（严格 JSON 不允许控制字符）。
        # 这类流在 SSE 里合法、在 JSON 里不合法，放宽一次再试，避免整段正文丢失。
        try:
            obj = json.loads(payload, strict=False)
        except Exception:
            return "", False, False, False, None
    if isinstance(obj, dict) and obj.get("error"):
        return "", False, False, True, None
    content, finished = _content_and_finish(obj)
    usage = None
    if isinstance(obj, dict) and obj.get("usage") is not None:
        usage = _extract_usage(obj["usage"])
    return content, finished, True, False, usage


def _parse_sse_line(line: str) -> Tuple[str, bool, bool]:
    """单行 SSE 原语（保留给逐行调用方；不处理跨行 data 拼接）。"""
    line = line.strip()
    if not line or line.startswith(":") or "keep-alive" in line.lower():
        return "", False, False
    if line.startswith("data:"):
        line = line[5:].strip()
    content, finished, valid, _error = _parse_sse_data(line)
    return content, finished, valid


def new_sse_event_parser():
    """按 SSE 规范组装事件：跨行的多条 data: 用 "\\n" 拼接后再解析。

    返回一个可调用对象 parse(line) -> (正文, 完成信号, 有效事件, 上游错误)。
    空白行代表一个事件结束；注释行与其他字段行被忽略（与规范一致）。
    ``parse.usage`` 在解析过程中累积最近一次非空 usage 块。
    """
    state = {"data": [], "usage": None}

    def parse(line: str) -> Tuple[str, bool, bool, bool]:
        line = line.rstrip("\r")
        stripped = line.strip()
        if not stripped:
            if not state["data"]:
                return "", False, False, False
            payload = "\n".join(state["data"])
            state["data"] = []
            content, finished, valid, upstream_error, usage = _parse_sse_event(payload)
            if usage is not None:
                state["usage"] = _merge_usage(state["usage"] or {}, usage)
            return content, finished, valid, upstream_error
        if stripped.startswith(":") or "keep-alive" in stripped.lower():
            return "", False, False, False
        if line.startswith("data:"):
            state["data"].append(line[5:].lstrip(" "))
        return "", False, False, False

    parse.state = state
    return parse


async def do_request(client, item, payload, headers, timeout_cfg, fb_patterns=None,
                     fb_keywords=None, chat_url=None, capture_raw=False,
                     include_usage=False):
    """执行一次请求。TTFT 仅在首次出现非空正文时记录。

    ``include_usage=True`` 时会在流式请求里带上 ``stream_options.include_usage``，
    以便拿到 usage 与缓存命中证据；老网关不接受该字段时由调用方降级重试。
    注入在这里完成，而不是交给每个调用方——忘记注入就会静默拿不到缓存命中率。
    """
    payload = _payload_with_usage(payload, include_usage)
    start = time.monotonic()
    started_at = time.time()
    result = new_blank_result()
    result.update({
        "ts": int(started_at * 1000),
        "item_id": item.get("id"),
        "mode": item.get("mode"),
        "profile": item.get("profile"),
        "step_index": item.get("step_index"),
        "grade": item.get("grade"),
    })
    status_code = 0
    ttft = None
    first_token_at = None
    cancelled = None
    completed = False
    error_type = None
    reason = None
    raw_parts = []
    raw_size = 0
    raw_truncated = False
    parse_parts = []
    parse_size = 0
    saw_valid_event = False
    saw_finish = False
    # 收到 finish 之后仍要看一眼尾块：OpenAI 在 include_usage 时把 usage 放在
    # finish_reason 之后、[DONE] 之前的独立块里，看到 finish 就断开会丢掉它。
    # 尾块数量有上限，避免个别上游在 finish 后不停地推增量导致连接被拖住。
    post_finish_events = 0
    usage_after_finish = False
    mid_stream_error = False
    response_text_parts = []
    usage_seen = {}

    try:
        timeout = httpx.Timeout(
            connect=float(timeout_cfg["connect"]),
            read=float(timeout_cfg["read"]),
            write=float(timeout_cfg["write"]),
            pool=float(timeout_cfg["pool"]),
        )
        async with client.stream("POST", chat_url, json=payload, headers=headers,
                                 timeout=timeout, follow_redirects=False) as resp:
            status_code = resp.status_code
            if not (200 <= status_code < 300):
                # TCP 分片可能把多字节字符切成两半，必须用增量解码器跨 chunk 拼接；
                # 逐块 decode("utf-8","replace") 会把半个汉字渲染成替换符。
                decoder = codecs.getincrementaldecoder("utf-8")("replace")
                async for chunk in resp.aiter_bytes():
                    text = decoder.decode(chunk)
                    if parse_size < PARSE_BUFFER_LIMIT:
                        part = text[:PARSE_BUFFER_LIMIT - parse_size]
                        parse_parts.append(part); parse_size += len(part)
                    if capture_raw:
                        remain = max(0, RAW_CAPTURE_LIMIT - raw_size)
                        if remain:
                            part = chunk[:remain]
                            raw_parts.append(part); raw_size += len(part)
                        raw_truncated = raw_truncated or len(chunk) > remain
                error_type = classify_failure(status_code)[0]
                reason = f"HTTP {status_code}"
                # 私有标记只供当前 worker 决定是否降级；逐条结果存储会忽略它。
                result["_usage_options_rejected"] = _usage_options_rejected(
                    status_code, "".join(parse_parts))
            else:
                line_buffer = ""
                event_parser = new_sse_event_parser()
                decoder = codecs.getincrementaldecoder("utf-8")("replace")
                async for chunk in resp.aiter_bytes():
                    text = decoder.decode(chunk)
                    if capture_raw:
                        remain = max(0, RAW_CAPTURE_LIMIT - raw_size)
                        if remain:
                            part = chunk[:remain]
                            raw_parts.append(part); raw_size += len(part)
                        raw_truncated = raw_truncated or len(chunk) > remain
                    if parse_size < PARSE_BUFFER_LIMIT:
                        part = text[:PARSE_BUFFER_LIMIT - parse_size]
                        parse_parts.append(part); parse_size += len(part)
                    line_buffer += text
                    while "\n" in line_buffer:
                        line, line_buffer = line_buffer.split("\n", 1)
                        content, finish, valid, upstream_error = event_parser(line)
                        saw_valid_event = saw_valid_event or valid
                        # usage 只可能出现在 finish 之后；一旦出现即可停止读取。
                        if event_parser.state["usage"] is not None:
                            _merge_usage(usage_seen, event_parser.state["usage"])
                            usage_after_finish = saw_finish or finish
                        if content:
                            response_text_parts.append(content)
                            if ttft is None:
                                first_token_at = time.time()
                                ttft = int((time.monotonic() - start) * 1000)
                        if upstream_error:
                            mid_stream_error = True
                            saw_finish = True
                        saw_finish = saw_finish or finish
                    if saw_finish:
                        completed = not mid_stream_error
                        post_finish_events += 1
                        if not include_usage or usage_after_finish or post_finish_events >= 4:
                            break
                    if len(line_buffer) > 256 * 1024:
                        line_buffer = line_buffer[-64 * 1024:]

                # 冲掉增量解码器里剩下的半个字符（正常应已在上面 break 前消费完）。
                tail = decoder.decode(b"", final=True)
                if tail:
                    line_buffer += tail

                body = "".join(parse_parts)
                if line_buffer.strip() and not saw_finish:
                    content, finish, valid, upstream_error = event_parser(line_buffer)
                    saw_valid_event = saw_valid_event or valid
                    if upstream_error:
                        mid_stream_error = True
                        saw_finish = True
                    saw_finish = saw_finish or finish
                    if content:
                        response_text_parts.append(content)
                        if ttft is None:
                            first_token_at = time.time()
                            ttft = int((time.monotonic() - start) * 1000)
                completed = (completed or saw_finish) and not mid_stream_error

                # 非流式兼容响应：2xx JSON 且包含正文即视为完成。
                if not completed and not mid_stream_error:
                    try:
                        parsed = json.loads(body)
                        content, finish = _content_and_finish(parsed)
                        if content:
                            response_text_parts.append(content)
                            # 完整 JSON 到达后才能解析正文，不能冒充流式首字时间。
                        completed = bool(content) or finish
                        if isinstance(parsed, dict) and parsed.get("usage") is not None:
                            found = _extract_usage(parsed["usage"])
                            if found:
                                _merge_usage(usage_seen, found)
                                usage_after_finish = True
                    except Exception:
                        pass
                if mid_stream_error:
                    error_type = "PROTOCOL"
                    reason = "流式响应中途返回上游错误对象（HTTP 200 内嵌 error）"
                elif not completed:
                    error_type = "PROTOCOL"
                    reason = "响应为 2xx，但未发现完整响应标记或有效正文"
    except asyncio.CancelledError as exc:
        cancelled = exc
        completed = False
        error_type = "CANCELLED"
        reason = "客户端取消请求；上游是否停止及最终计费未知"
    except httpx.TimeoutException as exc:
        error_type = "TIMEOUT"
        reason = f"{type(exc).__name__}: {exc}"[:300]
    except httpx.HTTPError as exc:
        error_type = "NETWORK"
        reason = f"{type(exc).__name__}: {exc}"[:300]
    except Exception as exc:
        error_type = "NETWORK"
        reason = f"{type(exc).__name__}: {exc}"[:300]

    ended_at = time.time()
    total_ms = int((time.monotonic() - start) * 1000)
    cached = usage_seen.get("cached_tokens")
    def since_item(name):
        value = item.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return max(0, (start - value) * 1000)
        return None

    result.update({
        "status_code": status_code,
        "completed": completed,
        "error_type": error_type,
        "error_detail": reason,
        "reason": reason,
        "response_text": "".join(response_text_parts),
        "ttft_ms": ttft,
        "total_ms": total_ms,
        "timing": request_timing(
            scheduled_at=item.get("scheduled_at"), enqueued_at=item.get("enqueued_at"),
            started_at=started_at, first_token_at=first_token_at, ended_at=ended_at,
            queue_ms=since_item("_enqueued_mono"),
            schedule_lag_ms=since_item("_scheduled_mono"), ttft_ms=ttft, total_ms=total_ms,
        ),
    })
    # 只有真的收到 usage 块才写入计量字段；否则保持 None，与「命中 0」区分开。
    if usage_seen:
        result.update({
            "usage_seen": True,
            "usage_source": "stream" if payload.get("stream") else "body",
            "prompt_tokens": usage_seen.get("prompt_tokens"),
            "cached_tokens": cached,
            "completion_tokens": usage_seen.get("completion_tokens"),
            "total_tokens": usage_seen.get("total_tokens"),
            "cache_hit": None if cached is None else bool(cached > 0),
        })
        if cached is not None and cached > 0:
            result["usage_level"] = "HIT_ONLY"
        elif cached is not None:
            result["usage_level"] = "NONE"
    if capture_raw:
        raw_output = b"".join(raw_parts).decode("utf-8", "replace")
        if raw_truncated:
            raw_output += "\n[已截断：达到原始响应记录上限]"
        result["raw_input"] = payload
        result["raw_output"] = raw_output
    if cancelled is not None:
        # 保留取消语义，同时让持久化调用方拿到最后的时间及用量证据。
        cancelled.request_result = result
        raise cancelled
    return result


def _validate_cli(args):
    if args.concurrency <= 0:
        raise SystemExit("--concurrency 必须大于 0")
    if args.queue_size <= 0:
        raise SystemExit("--queue-size 必须大于 0")
    if not math.isfinite(args.rps) or args.rps <= 0:
        raise SystemExit("--rps 必须是大于 0 的有限数字")
    if args.duration <= 0 or args.iterations <= 0:
        raise SystemExit("--duration 和 --iterations 必须大于 0")
    for name in ("connect_timeout", "read_timeout", "write_timeout"):
        value = getattr(args, name, None)
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise SystemExit(f"--{name.replace('_', '-')} 必须是大于 0 的有限数字")
    if args.profile not in {"smoke", "short", "long", "cache"}:
        raise SystemExit("--profile 必须是 smoke、short、long 或 cache")
    if args.mode == "ramp":
        if not args.ramps:
            raise SystemExit("ramp 模式需要 --ramps")
        try:
            ramps = [float(x.strip()) for x in args.ramps.split(",") if x.strip()]
        except ValueError:
            raise SystemExit("--ramps 必须是逗号分隔的正数")
        if not ramps or any(not math.isfinite(x) or x <= 0 for x in ramps):
            raise SystemExit("--ramps 必须是大于 0 的有限数字")


def _chat_url(base_url, explicit=None):
    if explicit:
        return explicit
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        base = base[:-3]
    return base + CHAT_PATH


def _payload_with_usage(payload, include_usage):
    """按需注入 ``stream_options.include_usage``（流式才有意义）。

    部分网关不认这个字段并直接 400，所以调用方在收到 4xx 时必须能降级重试；
    这里只负责构造，不替调用方决定兼容性。
    """
    if not (include_usage and payload.get("stream")):
        return payload
    enriched = dict(payload)
    options = dict(enriched.get("stream_options") or {})
    options["include_usage"] = True
    enriched["stream_options"] = options
    return enriched


def attempt_timing(result, attempt_index):
    """兼容性重发保留每次真实尝试的时间；不保存敏感正文。"""
    return {"attempt_index": attempt_index, "status_code": result.get("status_code"),
            "error_type": result.get("error_type"),
            "timing": result.get("timing") or request_timing(source="partial")}


async def run(args):
    _validate_cli(args)
    lifecycle = {"created_at": time.time()}
    created_mono = time.monotonic()
    lifecycle_mono = time.monotonic()
    lifecycle.update(started_at=time.time(), queue_ms=(lifecycle_mono - created_mono) * 1000)
    task_status = "running"
    dispatched = 0
    done = 0
    total = None
    dispatch_ended_mono = None
    output_created = False
    headers = dict(args.headers or {})
    if args.api_key:
        headers["Authorization"] = f"Bearer {args.api_key}"
    headers.setdefault("Accept", "text/event-stream")
    client = httpx.AsyncClient(http2=args.http2, verify=not args.insecure)
    out = None
    chat_url = _chat_url(args.url, args.chat_url)

    def persist_task_times():
        if not output_created:
            return
        metadata_path = Path(str(args.out) + ".task.json")
        pending_path = metadata_path.with_name(metadata_path.name + ".tmp")
        pending_path.write_text(json.dumps({
            "schema_version": 1, "status": task_status,
            "timing": task_timing(**lifecycle), "planned_requests": total,
            "started_requests": dispatched, "finished_requests": done,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(pending_path, metadata_path)

    try:
        if args.dry_run:
            models = await fetch_models(client, args.url, headers)
            print("发现模型：", ", ".join(models) if models else "未自动发现")
            print(json.dumps(build_payload(args.model or "<model>", args.profile,
                                           args.extra_messages, args.temperature,
                                           args.max_tokens, args.stream),
                             ensure_ascii=False, indent=2))
            return 0
        model = args.model
        if not model:
            models = await fetch_models(client, args.url, headers)
            if not models:
                raise SystemExit("未发现模型，请用 --model 指定")
            for i, value in enumerate(models):
                print(f"[{i}] {value}")
            idx = args.pick_index if args.pick_index is not None else int(input("选择模型序号：") or "0")
            model = models[idx]

        q = asyncio.Queue(maxsize=args.queue_size)
        write_lock = asyncio.Lock()
        # usage 模式是全局开关：一旦某个 worker 发现网关不支持就整体降级，
        # 避免同一轮里一部分请求带 include_usage、一部分不带，造成口径不一致。
        usage_mode = [bool(args.include_usage)]
        out = open(args.out, "w", encoding="utf-8") if args.out else None
        output_created = out is not None
        start = asyncio.get_running_loop().time()
        start_wall = time.time()

        steps = []
        if args.mode == "ramp":
            steps = [(i + 1, rps, args.duration, max(1, int(rps * args.duration)))
                     for i, rps in enumerate(float(x.strip()) for x in args.ramps.split(",") if x.strip())]
        elif args.mode == "soak":
            steps = [(1, args.rps, args.duration, max(1, int(args.rps * args.duration)))]
        else:
            steps = [(1, args.rps, args.iterations / args.rps, args.iterations)]
        total = sum(s[3] for s in steps)
        # 请求前先落盘创建/开始时间，进程意外消失时仍有可核查的任务起点。
        persist_task_times()

        async def producer():
            item_id = 0; offset = 0.0
            for step_index, rps, duration, count in steps:
                step_start = start + offset
                for j in range(count):
                    due = step_start + j / rps
                    wait = due - asyncio.get_running_loop().time()
                    if wait > 0:
                        await asyncio.sleep(wait)
                    item = {"id": item_id, "step_index": step_index, "rps": rps,
                            "scheduled_at": start_wall + offset + j / rps,
                            "_scheduled_mono": due, "mode": args.mode,
                            "profile": args.profile, "grade": args.grade}
                    await q.put(item)
                    # put 返回后才真正入队；队列满时的等待不伪装为队内等待。
                    item.update(enqueued_at=time.time(), _enqueued_mono=time.monotonic())
                    item_id += 1
                offset += duration
            for _ in range(args.concurrency):
                await q.put(None)

        async def worker():
            nonlocal done, dispatched, dispatch_ended_mono
            while True:
                item = await q.get()
                attempts = []
                try:
                    if item is None:
                        return
                    payload = build_payload(model, item["profile"], args.extra_messages,
                                            args.temperature, args.max_tokens, args.stream, request_index=item["id"])
                    dispatched += 1
                    lifecycle["dispatch_ended_at"] = time.time()
                    dispatch_ended_mono = time.monotonic()
                    result = await do_request(client, item, payload, headers,
                                              {"connect": args.connect_timeout, "read": args.read_timeout,
                                               "write": args.write_timeout, "pool": args.connect_timeout},
                                              chat_url=chat_url, include_usage=usage_mode[0])
                    attempts.append(attempt_timing(result, len(attempts) + 1))
                    # 仅在上游明确拒绝 usage 参数时降级；429 等失败保留且不追加请求。
                    if should_retry_without_usage(result, usage_mode[0], args.stream):
                        usage_mode[0] = False
                        lifecycle["dispatch_ended_at"] = time.time()
                        dispatch_ended_mono = time.monotonic()
                        retry_item = {key: value for key, value in item.items() if key not in
                                      ("scheduled_at", "enqueued_at", "_scheduled_mono", "_enqueued_mono")}
                        result = await do_request(client, retry_item, payload, headers,
                                                  {"connect": args.connect_timeout, "read": args.read_timeout,
                                                   "write": args.write_timeout, "pool": args.connect_timeout},
                                                  chat_url=chat_url, include_usage=False)
                        attempts.append(attempt_timing(result, len(attempts) + 1))
                    result.pop("_usage_options_rejected", None)
                    result["usage_retry_policy"] = USAGE_RETRY_POLICY
                    result["attempts"] = attempts
                    if out:
                        async with write_lock:
                            out.write(json.dumps(result, ensure_ascii=False) + "\n")
                            out.flush()
                    done += 1
                    if done % 100 == 0:
                        print(f"已完成 {done}/{total}")
                except asyncio.CancelledError as exc:
                    result = getattr(exc, "request_result", None)
                    if result is not None:
                        attempts.append(attempt_timing(result, len(attempts) + 1))
                        result["attempts"] = attempts
                        result["usage_retry_policy"] = USAGE_RETRY_POLICY
                        result.pop("_usage_options_rejected", None)
                        if out:
                            async with write_lock:
                                out.write(json.dumps(result, ensure_ascii=False) + "\n")
                                out.flush()
                        done += 1
                    raise
                finally:
                    q.task_done()

        producer_task = asyncio.create_task(producer())
        workers = [asyncio.create_task(worker()) for _ in range(args.concurrency)]
        pending = [producer_task, *workers]
        try:
            # 同时观察生产者和工作协程；否则队列满时工作协程失败会永久等入队。
            await asyncio.gather(*pending)
        except BaseException as exc:
            lifecycle["dispatch_ended_at"] = time.time()
            dispatch_ended_mono = time.monotonic()
            if isinstance(exc, asyncio.CancelledError):
                lifecycle["stop_requested_at"] = time.time()
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            raise
        if usage_mode[0]:
            print("usage 计量：已启用 stream_options.include_usage")
        else:
            print("usage 计量：本网关不支持 include_usage，已降级（缓存命中率将标记为不可得）")
        print(f"完成：{done}/{total}" + (f"，结果：{args.out}" if args.out else ""))
        task_status = "finished"
        return 0
    except asyncio.CancelledError:
        task_status = "cancelled"
        lifecycle.setdefault("stop_requested_at", time.time())
        raise
    except BaseException:
        task_status = "failed"
        raise
    finally:
        # 没有发完的任务以取消/异常确认时刻标记停止调度，不补造真实发送时间。
        if lifecycle.get("dispatch_ended_at") is None:
            lifecycle["dispatch_ended_at"] = time.time()
            dispatch_ended_mono = time.monotonic()
        lifecycle["ended_at"] = time.time()
        end_mono = time.monotonic()
        lifecycle["elapsed_ms"] = (end_mono - lifecycle_mono) * 1000
        lifecycle["drain_ms"] = (end_mono - dispatch_ended_mono) * 1000
        try:
            if out:
                out.close()
        finally:
            try:
                await client.aclose()
            finally:
                persist_task_times()


def main():
    p = argparse.ArgumentParser(description="黑盒网关请求执行器")
    p.add_argument("--url", required=True)
    p.add_argument("--chat-url")
    p.add_argument("--api-key")
    p.add_argument("--model")
    p.add_argument("--pick-index", type=int)
    p.add_argument("--mode", choices=["smoke", "ramp", "soak"], default="smoke")
    p.add_argument("--rps", type=float, default=100 / 60,
                   help="计划每秒请求数，默认 100/60（每分钟 100 请求）")
    p.add_argument("--ramps")
    p.add_argument("--iterations", type=int, default=50)
    p.add_argument("--duration", type=int, default=60)
    p.add_argument("--concurrency", type=int, default=20)
    p.add_argument("--queue-size", type=int, default=500)
    p.add_argument("--profile", choices=["smoke", "short", "long", "cache"], default="smoke")
    p.add_argument("--stream", action=argparse.BooleanOptionalAction, default=True)
    # 默认请求 usage：缓存命中率和 Token（词元）用量必须有真实上游证据。
    # 仍保留 --no-include-usage，用于明确知道目标不支持该扩展的兼容场景。
    p.add_argument("--include-usage", action=argparse.BooleanOptionalAction, default=True,
                   help="请求上游返回 usage 统计（缓存命中率数据来源）；网关不支持时自动降级")
    p.add_argument("--grade")
    p.add_argument("--out")
    p.add_argument("--connect-timeout", type=float, default=10.0)
    p.add_argument("--read-timeout", type=float, default=60.0)
    p.add_argument("--write-timeout", type=float, default=30.0)
    p.add_argument("--temperature", type=float)
    p.add_argument("--max-tokens", type=int)
    p.add_argument("--extra-messages")
    p.add_argument("--header", action="append", dest="header_items", default=[])
    p.add_argument("--http2", action="store_true")
    p.add_argument("--insecure", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    args.headers = {}
    for raw in args.header_items:
        if "=" not in raw:
            raise SystemExit(f"--header 格式错误：{raw}")
        key, value = raw.split("=", 1); args.headers[key] = value
    if args.extra_messages:
        try:
            args.extra_messages = json.loads(args.extra_messages)
            if not isinstance(args.extra_messages, list):
                raise ValueError
        except ValueError:
            raise SystemExit("--extra-messages 必须是 JSON 数组")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
