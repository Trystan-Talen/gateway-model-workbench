"""TPM 用量归一化、互斥计价与并发安全预算；未知值不等同于零。"""
from __future__ import annotations

import copy
import math
import threading
from decimal import Decimal, InvalidOperation

TOKEN_FIELDS = ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")
PRICE_FIELDS = ("input", "cache_read", "cache_write", "output")


def _count(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or value > 2 ** 53 or not math.isfinite(value) or int(value) != value:
        return None
    return int(value)


def normalize_usage(raw, semantics="openai", source="stream"):
    """只解释用户选定的协议；不把未知网关的同名字段自动混合。

    openai: prompt_tokens 已含 cached_tokens；该协议不单独报告缓存写入。
    independent: input_tokens 是普通输入，读取/写入为独立、不重叠分类。
    输入/输出齐全可计算完整 TPM，缓存明细齐全才能释放账单预留。
    """
    data = copy.deepcopy(raw) if isinstance(raw, dict) else None
    out = dict.fromkeys((*TOKEN_FIELDS, "full_input_tokens", "total_tokens"))
    out.update(raw=data, source=source if data is not None else None,
               semantics=semantics, complete=False, billing_complete=False,
               completeness="missing", issues=[], field_status={})
    if data is None:
        return out

    def read(container, key, dest):
        value = _count(container.get(key))
        out["field_status"][dest] = "missing" if key not in container else ("explicit" if value is not None else "invalid")
        if key in container and value is None:
            out["issues"].append(f"{key}（用量字段）无效")
        return value

    if semantics == "openai":
        full = read(data, "prompt_tokens", "full_input_tokens")
        output = read(data, "completion_tokens", "output_tokens")
        details = data.get("prompt_tokens_details")
        details = details if isinstance(details, dict) else {}
        cache_read = read(details, "cached_tokens", "cache_read_tokens")
        # 标准协议无独立写入分类，保留“协议不适用”来源，不伪装成上游明确的 0。
        cache_write = 0
        out["field_status"]["cache_write_tokens"] = "not_applicable"
        if any(key in data for key in ("cache_creation_input_tokens", "cache_read_input_tokens", "cache_write_tokens", "cached_tokens", "input_tokens")) or any(key in details for key in ("cache_write_tokens", "cache_creation_tokens")):
            out["issues"].append("发现所选协议未定义的缓存/输入字段，请确认用量语义")
        ordinary = full - cache_read if full is not None and cache_read is not None else None
        if ordinary is not None and ordinary < 0:
            ordinary = None
            out["issues"].append("缓存读取大于完整输入")
    elif semantics == "independent":
        ordinary = read(data, "input_tokens", "input_tokens")
        cache_read = read(data, "cache_read_input_tokens", "cache_read_tokens")
        cache_write = read(data, "cache_creation_input_tokens", "cache_write_tokens")
        output = read(data, "output_tokens", "output_tokens")
        full = sum((ordinary, cache_read, cache_write)) if all(v is not None for v in (ordinary, cache_read, cache_write)) else None
        out["field_status"]["full_input_tokens"] = "derived" if full is not None else "missing"
    else:
        out["issues"].append("用量协议未确认，不能可靠解释输入与缓存字段")
        out["completeness"] = "ambiguous"
        return out
    out.update(input_tokens=ordinary, cache_read_tokens=cache_read,
               cache_write_tokens=cache_write, full_input_tokens=full, output_tokens=output)
    if full is not None and output is not None:
        out["total_tokens"] = full + output
    if "total_tokens" in data:
        upstream_total = _count(data["total_tokens"])
        if upstream_total is None or (out["total_tokens"] is not None and upstream_total != out["total_tokens"]):
            out["issues"].append("总用量与输入输出分类不一致")
    out["complete"] = out["total_tokens"] is not None and not out["issues"]
    out["billing_complete"] = out["complete"] and all(out[key] is not None for key in TOKEN_FIELDS)
    out["completeness"] = "ambiguous" if out["issues"] else ("complete" if out["billing_complete"] else "partial")
    return out


def merge_usage(previous, latest):
    """累计快照只替换，不相加；空载荷不覆盖最后一个快照。

    不逐字段拼接不同快照，避免将旧缓存量拼入新输入量后产生虚假的完整用量。
    """
    if isinstance(latest, dict) and latest:
        return copy.deepcopy(latest)
    return copy.deepcopy(previous) if isinstance(previous, dict) else None


def _decimal(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("费用与报价必须为非负有限数") from None
    if not number.is_finite() or number < 0:
        raise ValueError("费用与报价必须为非负有限数")
    return number


def _cost_decimal(usage, prices, partial=False):
    result = Decimal(0)
    for token_key, price_key in zip(TOKEN_FIELDS, PRICE_FIELDS):
        amount, price = usage.get(token_key), prices.get(price_key)
        if amount is None or price is None:
            if partial or amount == 0:
                continue
            return None
        if _count(amount) is None:
            return None
        result += Decimal(amount) * _decimal(price) / Decimal(1_000_000)
    return result


def calculate_cost(usage, prices):
    """返回用户报价下费用；任何非零分类未知则返回 None。"""
    if usage.get("issues") or usage.get("billing_complete") is False:
        return None
    value = _cost_decimal(usage, prices)
    return float(value) if value is not None else None


def worst_case_cost(input_tokens, max_output_tokens, prices):
    """输入按最贵互斥分类计价；无报价的潜在写入不能当免费。"""
    if any(prices.get(key) is None for key in PRICE_FIELDS):
        return None
    highest = max(_decimal(prices[key]) for key in ("input", "cache_read", "cache_write"))
    return float((Decimal(str(input_tokens)) * highest + Decimal(str(max_output_tokens)) * _decimal(prices["output"])) / Decimal(1_000_000))


class BudgetLedger:
    """每次实际发送前原子预留；请求编号幂等，未知账单保持保守预留。"""

    def __init__(self, budget_usd, prices):
        self.budget = _decimal(budget_usd)
        self.prices = dict(prices)
        self._lock = threading.RLock()
        self._entries = {}
        self._known = Decimal(0)
        self._reserved = Decimal(0)
        self._unknown_count = 0
        self._inflight_count = 0

    def reserve(self, request_id, amount):
        value = _decimal(amount)
        with self._lock:
            if request_id in self._entries:
                return False
            if self._known + self._reserved + value > self.budget:
                return False
            self._entries[request_id] = {"reserved": value, "known": Decimal(0), "state": "reserved"}
            self._reserved += value
            self._inflight_count += 1
            return True

    def release_unsent(self, request_id):
        """仅允许调用方确认从未发送的请求释放；已结算编号不会再次释放。"""
        with self._lock:
            entry = self._entries.get(request_id)
            if entry is None or entry["state"] != "reserved":
                return False
            self._reserved -= entry["reserved"]
            entry.update(reserved=Decimal(0), state="unsent")
            self._inflight_count -= 1
            return True

    def settle(self, request_id, usage=None):
        with self._lock:
            entry = self._entries.get(request_id)
            if entry is None or entry["state"] != "reserved":
                return self.snapshot()
            self._inflight_count -= 1
            reliable = bool(usage and usage.get("billing_complete"))
            cost = _cost_decimal(usage, self.prices) if reliable else None
            if cost is not None:
                self._reserved -= entry["reserved"]
                self._known += cost
                entry.update(reserved=Decimal(0), known=cost, state="settled")
            else:
                # 有明确分类仍保留其已知费用，剩余最坏预留留存。语义矛盾的数据不计精确金额。
                partial = _cost_decimal(usage, self.prices, partial=True) if usage and not usage.get("issues") else Decimal(0)
                partial = partial or Decimal(0)
                remaining = max(Decimal(0), entry["reserved"] - partial)
                self._reserved += remaining - entry["reserved"]
                self._known += partial
                entry.update(reserved=remaining, known=partial, state="unknown")
                self._unknown_count += 1
            return self.snapshot()

    def snapshot(self):
        with self._lock:
            return {"budget_usd": float(self.budget), "known_cost_usd": float(self._known),
                    "reserved_cost_usd": float(self._reserved),
                    "remaining_usd": float(max(Decimal(0), self.budget - self._known - self._reserved)),
                    "unknown_requests": self._unknown_count,
                    "inflight_reservations": self._inflight_count,
                    "budget_exceeded": self._known + self._reserved > self.budget,
                    "billing_note": "按已获用量与用户报价计算，不是商家最终账单；未知费用保留预留"}
