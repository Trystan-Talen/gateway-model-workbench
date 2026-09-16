"""运维基础件与测试方案模板的回归测试。

这些模块是「可观测、可控并发、可复用方案」三项改造的落点，
单独测试可以不启动服务、不发真实请求。
"""
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import ops
import test_plan
import route_test_ui as ui


class ConcurrencyLimiterTests(unittest.TestCase):
    def test_default_limit_is_one(self):
        limiter = ops.ConcurrencyLimiter()
        self.assertEqual(limiter.limit(), 1)
        self.assertTrue(limiter.acquire("a"))
        self.assertFalse(limiter.acquire("b"), "闸门只有一个名额时第二个任务必须排队")
        limiter.release("a")
        self.assertTrue(limiter.acquire("b"))

    def test_set_limit_opens_slots_in_order(self):
        limiter = ops.ConcurrencyLimiter(1)
        limiter.acquire("a")
        self.assertFalse(limiter.acquire("b"))
        self.assertEqual(limiter.set_limit(2), 2)
        self.assertTrue(limiter.acquire("b"), "放宽上限后等待中的任务应立即获得名额")
        self.assertFalse(limiter.acquire("c"))

    def test_acquire_is_idempotent_for_same_task(self):
        limiter = ops.ConcurrencyLimiter(1)
        self.assertTrue(limiter.acquire("a"))
        self.assertTrue(limiter.acquire("a"))
        self.assertEqual(limiter.active(), ["a"])

    def test_release_reports_whether_slot_existed(self):
        limiter = ops.ConcurrencyLimiter(2)
        limiter.acquire("a")
        self.assertTrue(limiter.release("a"))
        self.assertFalse(limiter.release("a"))

    def test_limit_is_clamped_to_at_least_one(self):
        limiter = ops.ConcurrencyLimiter(0)
        self.assertEqual(limiter.limit(), 1)
        self.assertEqual(limiter.set_limit(-5), 1)

    def test_snapshot_shape(self):
        limiter = ops.ConcurrencyLimiter(3)
        limiter.acquire("a")
        snap = limiter.snapshot()
        self.assertEqual(snap["limit"], 3)
        self.assertEqual(snap["active"], 1)
        self.assertEqual(snap["active_ids"], ["a"])

    def test_concurrent_acquire_never_exceeds_limit(self):
        limiter = ops.ConcurrencyLimiter(2)
        granted = []
        lock = threading.Lock()

        def worker(name):
            if limiter.acquire(name):
                with lock:
                    granted.append(name)
                time.sleep(0.02)
                limiter.release(name)

        threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertLessEqual(len(granted), 2, "多线程争抢也不能超过上限")


class EventLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "events.jsonl")
        self.log = ops.EventLog(self.path)

    def tearDown(self):
        for name in ("events.jsonl", "events.jsonl.1"):
            try:
                os.remove(os.path.join(self.tmp, name))
            except OSError:
                pass
        os.rmdir(self.tmp)

    def test_writes_one_json_line_per_event(self):
        self.log.log("task.started", task_id="t1", mode="smoke")
        self.log.log("task.finished", task_id="t1", status="finished")
        with open(self.path, encoding="utf-8") as f:
            lines = [json.loads(x) for x in f if x.strip()]
        self.assertEqual([x["event"] for x in lines], ["task.started", "task.finished"])
        self.assertEqual(lines[0]["task_id"], "t1")
        self.assertIn("ts", lines[0])

    def test_secret_fields_are_redacted(self):
        self.log.log("http.request", api_key="sk-real", authorization="Bearer x",
                     nested={"password": "p", "keep": 1})
        with open(self.path, encoding="utf-8") as f:
            record = json.loads(f.readline())
        self.assertEqual(record["api_key"], "[已隐藏]")
        self.assertEqual(record["authorization"], "[已隐藏]")
        self.assertEqual(record["nested"]["password"], "[已隐藏]")
        self.assertEqual(record["nested"]["keep"], 1)

    def test_long_strings_are_truncated(self):
        self.log.log("x", blob="a" * 900)
        with open(self.path, encoding="utf-8") as f:
            record = json.loads(f.readline())
        self.assertLessEqual(len(record["blob"]), 501)
        self.assertTrue(record["blob"].endswith("…"))

    def test_rotation_keeps_previous_file(self):
        log = ops.EventLog(self.path, max_bytes=200)
        for i in range(40):
            log.log("filler", index=i, payload="x" * 60)
        self.assertTrue(os.path.exists(self.path + ".1"), "超过上限后应保留上一份日志")
        self.assertTrue(log.tail(5), "轮转后新文件仍可读")

    def test_write_failure_degrades_without_raising(self):
        log = ops.EventLog(os.path.join(self.tmp, "no-such-dir", "events.jsonl"))
        log.log("x", a=1)  # 不应抛出
        self.assertTrue(log.stats()["degraded"])
        self.assertEqual(log.tail(), [])

    def test_tail_returns_most_recent_first_readable(self):
        for i in range(5):
            self.log.log("e", index=i)
        tail = self.log.tail(2)
        self.assertEqual([x["index"] for x in tail], [3, 4])

    def test_stats_count_by_event(self):
        self.log.log("a")
        self.log.log("a")
        self.log.log("b")
        self.assertEqual(self.log.stats()["by_event"], {"a": 2, "b": 1})


class AuditLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.audit = ops.AuditLog(os.path.join(self.tmp, "audit.db"))

    def tearDown(self):
        for name in os.listdir(self.tmp):
            os.remove(os.path.join(self.tmp, name))
        os.rmdir(self.tmp)

    def test_record_and_recent_round_trip(self):
        self.assertIsNotNone(self.audit.record("task.delete", actor="alice", target="t1"))
        rows = self.audit.recent()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "task.delete")
        self.assertEqual(rows[0]["actor"], "alice")
        self.assertEqual(rows[0]["target"], "t1")
        self.assertTrue(rows[0]["ok"])

    def test_failure_is_preserved(self):
        self.audit.record("task.delete", actor="alice", ok=False, detail="任务不存在")
        row = self.audit.recent()[0]
        self.assertFalse(row["ok"])
        self.assertEqual(row["detail"], "任务不存在")

    def test_recent_returns_newest_first(self):
        for name in ("a", "b", "c"):
            self.audit.record(f"act.{name}")
        self.assertEqual([x["action"] for x in self.audit.recent()], ["act.c", "act.b", "act.a"])

    def test_limit_is_clamped(self):
        for i in range(5):
            self.audit.record("act", target=str(i))
        self.assertEqual(len(self.audit.recent(2)), 2)
        self.assertEqual(len(self.audit.recent(0)), 1)
        self.assertEqual(len(self.audit.recent(99999)), 5)

    def test_count(self):
        self.audit.record("a")
        self.audit.record("b")
        self.assertEqual(self.audit.count(), 2)

    def test_unwritable_path_degrades(self):
        audit = ops.AuditLog("/nonexistent-dir-audit/audit.db")
        before = time.time()
        self.assertIsNone(audit.record("x"))
        self.assertEqual(audit.recent(), [])
        self.assertEqual(audit.count(), 0)
        self.assertLess(time.time() - before, 5, "不可写时也要快速降级，不能长阻塞")

    def test_record_is_thread_safe(self):
        def worker(i):
            self.audit.record("bulk", target=str(i))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.audit.count(), 20)


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = test_plan.TemplateStore(os.path.join(self.tmp, "templates.json"))

    def tearDown(self):
        for name in os.listdir(self.tmp):
            os.remove(os.path.join(self.tmp, name))
        os.rmdir(self.tmp)

    def test_builtins_are_listed_and_marked(self):
        items = self.store.list()
        self.assertGreaterEqual(len(items), 6)
        self.assertTrue(all(x["builtin"] for x in items))
        self.assertTrue(all(x.get("id") for x in items))

    def test_every_builtin_config_passes_validation(self):
        for item in self.store.list():
            # 模板只包含「怎么压」的参数；目标是使用时才填的，所以这里补一个地址和模型再校验。
            cfg = {"url": "http://127.0.0.1:9/v1", "model": "gpt-4o-mini", **item["config"]}
            validated = ui.validate_config(cfg, require_secret=False)
            steps, total = ui.build_plan(validated)
            self.assertGreater(total, 0, f"{item['id']} 的请求量为 0")
            self.assertTrue(steps, f"{item['id']} 没有生成计划步骤")

    def test_builtin_configs_do_not_carry_secrets(self):
        for item in self.store.list():
            self.assertNotIn("api_key", item["config"])
            self.assertNotIn("url", item["config"], "目标地址属于使用者，不属于模板")

    def test_sanitize_strips_secrets_and_none(self):
        cleaned = test_plan.sanitize({"api_key": "sk-x", "url": "u", "concurrency": 4,
                                      "model": None, "token": "t"})
        self.assertEqual(cleaned, {"url": "u", "concurrency": 4})

    def test_save_assigns_custom_id_and_persists(self):
        saved = self.store.save({"name": "我的方案", "config": {"url": "http://127.0.0.1:1/v1", "mode": "smoke"}})
        self.assertTrue(saved["id"].startswith("custom-"))
        entry = self.store.get(saved["id"])
        self.assertFalse(entry["builtin"])
        self.assertEqual(entry["name"], "我的方案")
        # 重新打开也要还在（落盘不是内存假象）
        reopened = test_plan.TemplateStore(os.path.join(self.tmp, "templates.json"))
        self.assertIsNotNone(reopened.get(saved["id"]))

    def test_save_strips_secrets_from_config(self):
        saved = self.store.save({"name": "带密钥", "config": {"api_key": "sk-real", "url": "u"}})
        self.assertNotIn("api_key", saved["config"])
        self.assertNotIn("sk-real", json.dumps(self.store.list(), ensure_ascii=False))

    def test_save_requires_name_and_config(self):
        with self.assertRaises(ValueError):
            self.store.save({"config": {"url": "u"}})
        with self.assertRaises(ValueError):
            self.store.save({"name": "x"})

    def test_builtin_cannot_be_deleted(self):
        builtin_id = self.store.list()[0]["id"]
        with self.assertRaises(ValueError):
            self.store.delete(builtin_id)

    def test_custom_can_be_deleted(self):
        saved = self.store.save({"name": "临时", "config": {"url": "u"}})
        self.store.delete(saved["id"])
        self.assertIsNone(self.store.get(saved["id"]))

    def test_get_unknown_returns_none(self):
        self.assertIsNone(self.store.get("does-not-exist"))

    def test_secret_never_persisted_to_disk(self):
        self.store.save({"name": "s", "config": {"api_key": "sk-leak", "url": "u"}})
        with open(os.path.join(self.tmp, "templates.json"), encoding="utf-8") as f:
            self.assertNotIn("sk-leak", f.read())


class SchedulerIntegrationTests(unittest.TestCase):
    """第二个任务在放宽上限后真的会开跑，而不是无限期排队。"""

    def setUp(self):
        # LIMITER 是进程级单例；这里必须把上一轮测试占住的名额清掉，否则后续用例
        # 会因为「名额已经被别的测试占着」而排队，看起来像业务逻辑出错。
        self.addCleanup(self._reset_limiter)
        self._reset_limiter()
        # 只替换「真实 I/O」这一层：调度、结果文件、协程执行都不该在单元测试里发生。
        self.patches = [
            mock.patch.object(ui, "persist_history"),
            mock.patch.object(ui, "LOOP", object()),
            mock.patch.object(ui, "run_task", side_effect=_closed_coro),
        ]
        self.run_task = self.patches[2].start()
        for patch in self.patches[:2]:
            patch.start()
        self.scheduled = []
        self._schedule_patch = mock.patch(
            "asyncio.run_coroutine_threadsafe",
            side_effect=self._fake_schedule,
        )
        self._schedule_patch.start()
        self.addCleanup(self._stop_patches)

    def _reset_limiter(self):
        for task_id in list(ui.LIMITER.active()):
            ui.LIMITER.release(task_id)
        ui.LIMITER.set_limit(1)

    def _stop_patches(self):
        self._schedule_patch.stop()
        for patch in self.patches:
            patch.stop()

    def _fake_schedule(self, coro, loop):
        self.scheduled.append(coro)
        coro.close()
        future = mock.MagicMock()
        future.result = lambda: None
        return future

    def _config(self):
        return {"url": "http://127.0.0.1:1/v1", "api_key": "k", "model": "gpt-4o-mini",
                "mode": "smoke", "iterations": 1, "concurrency": 1, "rpm": 60, "profile": "smoke"}

    def test_second_task_starts_once_a_slot_opens(self):
        manager = ui.TaskManager()
        with mock.patch.object(ui.Task, "open_output", lambda self: None):
            first = manager.add(self._config())
            self.assertEqual(manager.running_ids, [first.id])
            second = manager.add(self._config())
            self.assertNotIn(second.id, manager.running_ids, "名额用完时应排队而不是并行启动")
            self.assertEqual(manager.snapshot()["waiting"], [second.id])

            ui.LIMITER.set_limit(2)
            manager._pump()
        self.assertIn(second.id, manager.running_ids)
        self.assertEqual(self.run_task.call_count, 2, "名额放开后等待中的任务必须被真正启动")

    def test_finish_releases_slot_and_starts_next(self):
        manager = ui.TaskManager()
        with mock.patch.object(ui.Task, "open_output", lambda self: None):
            first = manager.add(self._config())
            second = manager.add(self._config())
            manager.finish(manager.get(first.id))
        self.assertEqual(manager.running_ids, [second.id])

    def test_current_id_keeps_pointing_at_latest_started(self):
        manager = ui.TaskManager()
        with mock.patch.object(ui.Task, "open_output", lambda self: None):
            first = manager.add(self._config())
            self.assertEqual(manager.current_id, first.id)
            ui.LIMITER.set_limit(2)
            second = manager.add(self._config())
        self.assertEqual(manager.current_id, second.id)
        self.assertEqual(manager.current_id, manager.running_ids[-1])

    def test_snapshot_exposes_limiter_and_queue(self):
        manager = ui.TaskManager()
        with mock.patch.object(ui.Task, "open_output", lambda self: None):
            manager.add(self._config())
        snap = manager.snapshot()
        self.assertEqual(snap["limit"], 1)
        self.assertEqual(snap["active"], 1)


def _closed_coro():
    async def _noop():
        return None
    return _noop()


if __name__ == "__main__":
    unittest.main()
