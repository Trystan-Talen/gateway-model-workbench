"""任务生命周期时钟与旧证据适配；不把恢复时刻或文件时间冒充执行时间。"""
import threading
import time

from time_record import request_timing, task_timing


class RunClock:
    def __init__(self, created_at=None):
        self._lock = threading.RLock()
        self.wall = {"created_at": time.time() if created_at is None else created_at}
        self.mono = {"created_at": time.monotonic()}
        self._last_dispatch = None

    def dispatched(self):
        """每次实际 HTTP（网络请求）调用前实录；含兼容性第二次尝试。"""
        with self._lock:
            self._last_dispatch = (time.time(), time.monotonic())

    def finish_dispatch(self):
        with self._lock:
            if "dispatch_ended_at" not in self.wall:
                if self._last_dispatch is None:
                    return self.mark("dispatch_ended_at")
                self.wall["dispatch_ended_at"], self.mono["dispatch_ended_at"] = self._last_dispatch
            return self.wall["dispatch_ended_at"]

    def mark(self, event):
        with self._lock:
            if event not in self.wall:
                self.wall[event] = time.time()
                self.mono[event] = time.monotonic()
            return self.wall[event]

    def snapshot(self):
        with self._lock:
            start = self.mono.get("started_at")
            end = self.mono.get("ended_at")
            dispatch = self.mono.get("dispatch_ended_at")
            return task_timing(
                **self.wall,
                queue_ms=(start - self.mono["created_at"]) * 1000 if start is not None else None,
                elapsed_ms=((end if end is not None else time.monotonic()) - start) * 1000 if start is not None else None,
                drain_ms=(end - dispatch) * 1000 if end is not None and dispatch is not None else None,
            )

    def elapsed(self):
        return (self.snapshot()["elapsed_ms"] or 0) / 1000


def restored_timing(snapshot, *, interrupted=False):
    recorded = snapshot.get("timing")
    if isinstance(recorded, dict):
        fields = dict(recorded)
    else:
        elapsed = snapshot.get("elapsed")
        fields = dict(created_at=snapshot.get("created"), started_at=snapshot.get("started_at"),
                      ended_at=snapshot.get("finished_at"), source="legacy",
                      elapsed_ms=elapsed * 1000 if isinstance(elapsed, (float, int)) and not isinstance(elapsed, bool) else None)
    if interrupted and not fields.get("interruption_detected_at"):
        fields["interruption_detected_at"] = time.time()
    return task_timing(**fields)


def request_evidence(record):
    if isinstance(record.get("timing"), dict):
        return request_timing(**record["timing"])
    ts = record.get("ts")
    return request_timing(
        started_at=ts / 1000 if isinstance(ts, (int, float)) and not isinstance(ts, bool) else None,
        ttft_ms=record.get("ttft_ms"), total_ms=record.get("total_ms"),
        schedule_lag_ms=record.get("schedule_lag_ms"), source="legacy",
    )
