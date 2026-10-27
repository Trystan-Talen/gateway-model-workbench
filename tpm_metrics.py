"""精确增量统计；查询不重扫完整请求历史，也不把短窗口外推为一分钟。"""
from __future__ import annotations

from bisect import bisect_right, insort_right
from collections import Counter
import math
import threading


_TOKEN_FIELDS = (
    ("total_tokens", "full_token_tpm"),
    ("full_input_tokens", "input_tpm"),
    ("output_tokens", "output_tpm"),
    ("cache_read_tokens", "cache_read_tpm"),
    ("cache_write_tokens", "cache_write_tpm"),
)
_DISTRIBUTIONS = ("ttft_seconds", "latency_seconds", "schedule_delay_seconds", "pool_wait_seconds")


def _percentile(values, quantile):
    """与报告的线性插值相同；values 已有序，不在查询时重新排序。"""
    if not values:
        return None
    position = (len(values) - 1) * quantile
    low, high = math.floor(position), math.ceil(position)
    return values[low] + (values[high] - values[low]) * (position - low)


class _CompletedWindow:
    """已成功且完整的整数用量，以结束时间及前缀和保存。

    正常顺序完成只追加；墙钟回拨或乱序完成时，仅重建插入位置后的前缀。
    二分查询同时支持向前、向后观察，不会因窗口曾移过而丢掉历史证据。
    """

    def __init__(self):
        self.times = []
        self.values = []
        # 五类用量，加上读取/写入未知值计数。未知值进入窗口时仍输出 None。
        self.prefix = [(0,) * 7]

    def add(self, ended_at, usage):
        values = tuple(usage.get(key) or 0 for key, _ in _TOKEN_FIELDS)
        values += (int(usage.get("cache_read_tokens") is None),
                   int(usage.get("cache_write_tokens") is None))
        position = bisect_right(self.times, ended_at)
        self.times.insert(position, ended_at)
        self.values.insert(position, values)
        if position == len(self.times) - 1:
            self.prefix.append(tuple(a + b for a, b in zip(self.prefix[-1], values)))
            return
        # Rare out-of-order completions cannot use the append-only shortcut.
        del self.prefix[position + 1:]
        for item in self.values[position:]:
            self.prefix.append(tuple(a + b for a, b in zip(self.prefix[-1], item)))

    def totals(self, lower, upper):
        left, right = bisect_right(self.times, lower), bisect_right(self.times, upper)
        return tuple(b - a for a, b in zip(self.prefix[left], self.prefix[right]))


class LiveMetrics:
    """消费只追加的完成记录，提供与 tpm_report.metrics 相同的实时口径。

    调用方完成记录后追加，再调用 update(records)。已有记录必须保持不变；
    替换列表对象或缩短列表会自动重建。线程锁保护增量更新与查询的一致性。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._reset(None)

    def _reset(self, records):
        self._records = records
        self._consumed = 0
        self._formal_count = self._succeeded = self._reliable = 0
        self._cache_read = self._cache_input = self._output_limited = 0
        self._errors = Counter()
        self._started = []
        self._durations = {key: [] for key in _DISTRIBUTIONS}
        self._completed = _CompletedWindow()

    def update(self, records):
        """只读取新增条目；重复调用不会重复计量。"""
        with self._lock:
            length = len(records)
            if records is not self._records or length < self._consumed:
                self._reset(records)
            for index in range(self._consumed, length):
                row = records[index]
                if row.get("phase") != "formal":
                    continue
                self._formal_count += 1
                success = bool(row.get("success"))
                self._succeeded += success
                if not success:
                    self._errors[row.get("error")] += 1
                self._output_limited += bool(row.get("output_limited"))
                insort_right(self._started, row["started_at"])
                for key, values in self._durations.items():
                    value = row.get(key)
                    if value is not None:
                        insort_right(values, float(value))
                usage = row["usage"]
                if usage.get("complete"):
                    self._reliable += 1
                    if usage.get("cache_read_tokens") is not None:
                        self._cache_read += usage["cache_read_tokens"]
                        self._cache_input += usage.get("full_input_tokens", 0)
                    if success:
                        self._completed.add(row["ended_at"], usage)
            self._consumed = length

    def snapshot(self, *, now, formal_started_at=None, planned=0, missed=0, in_flight=0):
        with self._lock:
            window = self._completed.totals(now - 60, now)
            totals = {name: window[index] for index, (_, name) in enumerate(_TOKEN_FIELDS)}
            if window[5]:
                totals["cache_read_tpm"] = None
            if window[6]:
                totals["cache_write_tpm"] = None
            starts = bisect_right(self._started, now) - bisect_right(self._started, now - 10)
            seconds = max(0, min(60, now - formal_started_at)) if formal_started_at else 0
            elapsed = max(.001, min(10, now - formal_started_at)) if formal_started_at else 10
            count = self._formal_count
            return {
                **totals, "planned": planned, "started": count + in_flight,
                "succeeded": self._succeeded, "failed": count - self._succeeded,
                "missed": missed, "in_flight": in_flight, "actual_start_rps": starts / elapsed,
                "window_seconds": seconds, "window_full": seconds >= 60,
                "success_rate": self._succeeded / count if count else None,
                "usage_complete_rate": self._reliable / count if count else None,
                "cache_hit_rate": self._cache_read / self._cache_input if self._cache_input else None,
                "ttft_p50": _percentile(self._durations["ttft_seconds"], .5),
                "ttft_p95": _percentile(self._durations["ttft_seconds"], .95),
                "latency_p50": _percentile(self._durations["latency_seconds"], .5),
                "latency_p95": _percentile(self._durations["latency_seconds"], .95),
                "schedule_delay_p95": _percentile(self._durations["schedule_delay_seconds"], .95),
                "pool_wait_p95": _percentile(self._durations["pool_wait_seconds"], .95),
                "pool_wait_source": "client_admission_semaphore", "transport_pool_wait": None,
                "http_429": self._errors["http_429"], "timeouts": self._errors["timeout"],
                "output_limited": self._output_limited, "errors": dict(self._errors),
            }
