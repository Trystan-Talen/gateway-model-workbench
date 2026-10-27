"""Only synthetic transports and temporary storage; never uses real credentials."""
import asyncio
import copy
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import fingerprint_service as service
from fingerprint_protocol import make_plan, make_protocol, _digest


class Limiter:
    def __init__(self, blocked=False):
        self.blocked = blocked
        self.active = set()
        self.acquisitions = []
        self.releases = []

    def acquire(self, run_id):
        if self.blocked or self.active:
            return False
        self.active.add(run_id)
        self.acquisitions.append(run_id)
        return True

    def release(self, run_id):
        self.active.discard(run_id)
        self.releases.append(run_id)


def answer_for(body):
    prompt = body["messages"][1]["content"].lower()
    if "color" in prompt:
        return "blue"
    if "颜色" in prompt:
        return "蓝色"
    if "animal" in prompt:
        return "cat"
    if "动物" in prompt:
        return "猫"
    if "letter" in prompt or "字母" in prompt:
        return "a"
    if "coin" in prompt:
        return "heads"
    if "硬币" in prompt:
        return "正面"
    if "city" in prompt:
        return "Paris"
    if "城市" in prompt:
        return "北京"
    return "7"


def success(request, **overrides):
    body = json.loads(request.content)
    payload = {"model": body["model"], "id": "synthetic-" + str(time.monotonic_ns()),
               "choices": [{"message": {"role": "assistant", "content": answer_for(body)}, "finish_reason": "stop"}]}
    payload.update(overrides)
    return httpx.Response(200, json=payload)


class FingerprintServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.managers = []
        self.requests = []

    def tearDown(self):
        for manager in self.managers:
            manager.close()
        self.directory.cleanup()

    def manager(self, handler=None, limiter=None, directory=None):
        manager = service.FingerprintManager(directory or self.directory.name, limiter=limiter)
        async def record(request):
            self.requests.append(request)
            result = handler(request) if handler else success(request)
            return await result if asyncio.iscoroutine(result) else result
        manager._new_client = lambda protocol: httpx.AsyncClient(transport=httpx.MockTransport(record), follow_redirects=False, trust_env=False)
        manager.restore()
        self.managers.append(manager)
        return manager

    def config(self, **overrides):
        result = {"name": "合成通道自检", "mode": "selfcheck", "preset": "quick", "samples_per_cell": 10,
                  "concurrency": 2, "rpm": 600000, "timeout_seconds": 5, "adapter": "standard", "risk_acknowledged": True,
                  "endpoint": {"url": "https://synthetic.invalid/v1", "model": "synthetic-model", "api_key": "synthetic-test-secret"}}
        result.update(overrides)
        return result

    def wait(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(.01)
        self.fail("synthetic task did not reach the expected state")

    def complete(self, manager, run):
        return self.wait(lambda: (manager.get(run["id"]) if manager.get(run["id"])["status"] in service.TERMINAL else None))

    def collect(self, manager, **overrides):
        return self.complete(manager, manager.create(self.config(**overrides)))

    def test_active_elapsed_uses_runtime_clock_then_freezes_at_end(self):
        async def slow(request):
            await asyncio.sleep(1)
            return success(request)
        limiter = Limiter(blocked=True)
        manager = self.manager(slow, limiter=limiter)
        run = manager.create(self.config())
        self.assertIsNone(run["timing"]["elapsed_ms"])
        with manager._condition:
            limiter.blocked = False
            manager._condition.notify_all()
        self.wait(lambda: self.requests and manager.get(run["id"])["status"] == "running")
        initial = manager.get(run["id"])["timing"]["elapsed_ms"]
        self.assertGreater(initial, 0)
        time.sleep(.01)
        self.assertGreater(manager.get(run["id"])["timing"]["elapsed_ms"], initial)
        self.assertIsNone(manager.get(run["id"])["timing"]["ended_at"])
        manager.stop(run["id"])
        final = self.complete(manager, run)["timing"]
        time.sleep(.01)
        self.assertEqual(manager.get(run["id"])["timing"], final)
        self.assertGreaterEqual(final["elapsed_ms"], initial)
        self.assertIsNotNone(final["ended_at"])

    def test_task_and_sample_timing_are_preserved_without_changing_protocol(self):
        manager = self.manager()
        run = self.collect(manager)
        timing = run["timing"]
        self.assertRegex(timing["created_at"], r"^\d{4}-\d\d-\d\dT.*\.\d{3}\+08:00$")
        self.assertLessEqual(timing["created_at"], timing["started_at"])
        self.assertLessEqual(timing["dispatch_ended_at"], timing["ended_at"])
        self.assertGreater(timing["elapsed_ms"], 0)
        self.assertGreaterEqual(timing["queue_ms"], 0)
        self.assertGreaterEqual(timing["drain_ms"], 0)
        rows = manager.samples(run["id"])["samples"]
        self.assertEqual(len(rows), 40)
        for row in rows:
            measured = row["timing"]
            self.assertEqual(measured["source"], "recorded")
            self.assertLessEqual(measured["enqueued_at"], measured["started_at"])
            self.assertLessEqual(measured["started_at"], measured["ended_at"])
            self.assertEqual(measured["total_ms"], row["latency_ms"])
            self.assertIsNone(measured["first_token_at"])
            self.assertIsNone(measured["ttft_ms"])
            self.assertIsNone(measured["scheduled_at"])
        exported = manager.export(run["id"])
        self.assertEqual(exported["timing"], timing)
        self.assertNotIn("timing", exported["protocol"])
        imported = service.validate_import(exported)
        self.assertEqual(imported["timing"], timing)
        self.assertEqual(imported["protocol"], exported["protocol"])
        manager.close()
        restored = self.manager()
        self.assertEqual(restored.get(run["id"])["timing"], timing)
        self.assertEqual(restored.samples(run["id"])["samples"], rows)

    def test_legacy_sample_mapping_keeps_unobserved_timestamps_unknown(self):
        manager = self.manager(limiter=Limiter(blocked=True))
        run = manager.create(self.config())
        manager.stop(run["id"])
        legacy = {"index": 0, "cell_id": "synthetic", "started_at": "2026-10-15T12:00:00+08:00",
                  "finished_at": "2026-10-15T12:00:01+08:00", "latency_ms": 985.5}
        manager._samples_path(run["id"]).write_text(json.dumps(legacy) + "\n")
        timing = manager.samples(run["id"])["samples"][0]["timing"]
        self.assertEqual(timing["source"], "legacy")
        self.assertEqual(timing["started_at"], "2026-10-15T12:00:00.000+08:00")
        self.assertEqual(timing["total_ms"], 985.5)
        self.assertIsNone(timing["first_token_at"])
        self.assertIsNone(timing["enqueued_at"])
        self.assertIsNone(timing["queue_ms"])

    def test_legacy_restart_records_detection_without_fabricating_completion(self):
        manager = self.manager(limiter=Limiter(blocked=True))
        run = manager.create(self.config())
        identifier = run["id"]
        metadata = copy.deepcopy(manager._runs[identifier])
        manager.stop(identifier)
        manager.close()
        metadata.pop("timing")
        metadata["status"] = "running"
        metadata["started_at"] = metadata["created"]
        metadata["finished_at"] = None
        manager._save_run(metadata)
        restored = self.manager()
        recovered = restored.get(identifier)
        self.assertEqual(recovered["status"], "interrupted")
        self.assertIsNone(recovered["finished_at"])
        self.assertIsNone(recovered["timing"]["ended_at"])
        self.assertIsNone(recovered["timing"]["elapsed_ms"])
        self.assertIsNotNone(recovered["timing"]["interruption_detected_at"])
        self.assertIsNone(recovered["timing"]["stop_requested_at"])
        self.assertEqual(recovered["artifact"]["timing"], recovered["timing"])
        self.assertFalse(self.requests)

    def test_new_defaults_and_connection_pool_agree(self):
        manager = self.manager(limiter=Limiter(blocked=True))
        config = self.config()
        config.pop("rpm")
        config.pop("concurrency")
        run = manager.create(config)
        self.assertEqual((run["protocol"]["concurrency"], run["protocol"]["rpm"]), (20, 100))
        async def inspect_client():
            async with service.FingerprintManager._new_client(manager, run["protocol"]) as client:
                self.assertEqual(client._transport._pool._max_connections, 20)
                self.assertEqual(client._transport._pool._max_keepalive_connections, 20)
        asyncio.run(inspect_client())

    def test_rate_pacer_spaces_workers_and_does_not_catch_up(self):
        async def scenario():
            now = 0
            starts = []
            async def advance(delay):
                nonlocal now
                now += delay
            pacer = service._RequestPacer(100, clock=lambda: now, sleep=advance)
            async def start():
                self.assertTrue(await pacer.wait())
                starts.append(now)
            await asyncio.gather(*(start() for _ in range(5)))
            self.assertEqual([round(x, 3) for x in starts], [0, .6, 1.2, 1.8, 2.4])
            now += 5
            await start()
            await start()
            self.assertAlmostEqual(starts[-1] - starts[-2], .6)
            before = now
            self.assertFalse(await pacer.wait(lambda: False))
            self.assertEqual(now, before)
        asyncio.run(scenario())

    def test_stop_while_rate_waiting_sends_no_more_requests(self):
        starts = []
        def observed(request):
            starts.append(time.monotonic())
            return success(request)
        manager = self.manager(observed)
        run = manager.create(self.config(concurrency=20, rpm=100))
        self.wait(lambda: len(starts) >= 2)
        stop_at = time.monotonic()
        manager.stop(run["id"])
        final = self.complete(manager, run)
        self.assertGreaterEqual(starts[1] - starts[0], .58)
        self.assertEqual(len(starts), 2)
        self.assertLess(time.monotonic() - stop_at, .5)
        self.assertEqual(final["status"], "stopped")
        self.assertEqual(final["progress"]["done"], 2)

    def test_legacy_reference_and_frozen_repeat_keep_original_manifest(self):
        manager = self.manager()
        legacy = make_protocol(self.config())
        legacy.pop("rpm")
        legacy["id"] = _digest({k: v for k, v in legacy.items() if k != "id"})
        baseline = self.complete(manager, manager.create(self.config(mode="reference", trusted_source=True, source_note="合成旧参考"), frozen_protocol=legacy))
        config = self.config(mode="verify", reference_id=baseline["reference_id"])
        config.pop("rpm")
        candidate = self.complete(manager, manager.create(config))
        self.assertEqual(candidate["protocol"], legacy)
        self.assertTrue(candidate["comparison"]["protocol_compatible"])
        repeated = self.complete(manager, manager.create(self.config(), frozen_protocol=legacy))
        self.assertEqual(repeated["protocol"], legacy)
        with self.assertRaisesRegex(ValueError, "冻结协议一致"):
            manager.create({**config, "rpm": 100})

    def test_exact_single_turn_protocol_and_no_secrets_on_disk(self):
        manager = self.manager()
        run = self.collect(manager)
        self.assertEqual(run["status"], "finished", run.get("error"))
        self.assertEqual(run["progress"], {"done": 40, "total": 40, "valid": 40, "errors": 0})
        self.assertEqual(len(self.requests), 40)
        expected_plan = make_plan(run["protocol"], manager._runs[run["id"]]["seed"])
        expected_messages = sorted((p["system_prompt"], p["user_prompt"]) for p in expected_plan)
        actual_messages = []
        for request in self.requests:
            body = json.loads(request.content)
            self.assertEqual(set(body), {"model", "stream", "temperature", "top_p", "max_tokens", "messages"})
            self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
            self.assertEqual(body["temperature"], 1)
            self.assertEqual(body["top_p"], 1)
            self.assertEqual(body["max_tokens"], 16)
            self.assertIs(body["stream"], False)
            self.assertEqual(request.headers["authorization"], "Bearer synthetic-test-secret")
            self.assertEqual(str(request.url), "https://synthetic.invalid/v1/chat/completions")
            actual_messages.append(tuple(m["content"] for m in body["messages"]))
        self.assertEqual(sorted(actual_messages), expected_messages)
        self.assertEqual(run["diagnostics"]["usage"], dict.fromkeys(service.USAGE_KEYS))
        self.assertNotIn("samples", manager.list_runs()[0])
        self.assertNotIn("artifact", manager.list_runs()[0])
        self.assertIsNotNone(run["artifact"])
        self.assertTrue(run["artifact"]["complete"])
        self.assertFalse(manager._runtimes)
        for path in manager.data_dir.rglob("*"):
            if path.is_file():
                self.assertNotIn("synthetic-test-secret", path.read_text())
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_deepseek_official_requires_explicit_nonthinking_protocol(self):
        manager = self.manager()
        endpoint = {"url": "https://api.deepseek.com", "model": "deepseek-flash", "api_key": "synthetic-test-secret"}
        with self.assertRaisesRegex(ValueError, "关闭思考"):
            manager.create(self.config(endpoint=endpoint))
        self.assertEqual(self.requests, [])
        self.assertEqual(manager.list_runs(), [])

    def test_deepseek_disables_thinking_without_expanding_or_retrying(self):
        def provider(request):
            body = json.loads(request.content)
            if body.get("thinking") != {"type": "disabled"}:
                return success(request, choices=[{"message": {"content": "", "reasoning_content": "synthetic"}, "finish_reason": "length"}],
                               usage={"completion_tokens": 16, "completion_tokens_details": {"reasoning_tokens": 16}})
            return success(request)
        manager = self.manager(provider)
        run = self.collect(manager, adapter="deepseek", endpoint={"url": "https://api.deepseek.com", "model": "deepseek-flash", "api_key": "synthetic-test-secret"})
        self.assertEqual(run["status"], "finished")
        self.assertEqual(run["progress"]["valid"], 40)
        self.assertEqual(len(self.requests), 40)
        for request in self.requests:
            body = json.loads(request.content)
            self.assertEqual(body["thinking"], {"type": "disabled"})
            self.assertEqual(body["max_tokens"], 16)
            self.assertEqual(body["temperature"], 1)
            self.assertEqual(len(body["messages"]), 2)
        standard = service.make_protocol(self.config())
        self.assertNotEqual(run["protocol"]["id"], standard["id"])
        self.assertEqual(standard["extra_body"], {})

    def test_reasoning_only_truncation_stops_early_and_never_builds_reference(self):
        def exhausted(request):
            return success(request, choices=[{"message": {"content": "", "reasoning_content": "synthetic"}, "finish_reason": "length"}],
                           usage={"completion_tokens": 16, "completion_tokens_details": {"reasoning_tokens": 16}})
        manager = self.manager(exhausted)
        run = self.collect(manager, concurrency=1, mode="reference", trusted_source=True, source_note="合成来源")
        self.assertEqual(run["status"], "error")
        self.assertIn("连续 3 次", run["error"])
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(run["progress"], {"done": 3, "total": 40, "valid": 0, "errors": 0})
        self.assertFalse(run["artifact"]["complete"])
        self.assertEqual(manager.list_references(), [])
        for row in manager.samples(run["id"])["samples"]:
            self.assertIn("推理阶段耗尽", row["error"])
            self.assertEqual(row["category"], "invalid")
            self.assertEqual(row["status_code"], 200)

    def test_parallel_reasoning_failure_cancels_inflight_and_bounds_requests(self):
        async def exhausted(request):
            await asyncio.sleep(.01)
            return success(request, choices=[{"message": {"content": "", "reasoning_content": "synthetic"}, "finish_reason": "length"}])
        manager = self.manager(exhausted)
        run = self.collect(manager, concurrency=4)
        self.assertEqual(run["status"], "error")
        self.assertLessEqual(len(self.requests), 3 + 4 - 1)
        self.assertEqual(run["progress"]["done"], len(self.requests))
        self.assertFalse(run["artifact"]["complete"])
        self.assertFalse(manager._runtimes)

    def test_isolated_reasoning_truncation_does_not_stop_healthy_collection(self):
        def intermittent(request):
            if len(self.requests) % 3 != 0:
                return success(request, choices=[{"message": {"content": "", "reasoning_content": "synthetic"}, "finish_reason": "length"}])
            return success(request)
        manager = self.manager(intermittent)
        run = self.collect(manager, concurrency=1)
        self.assertEqual(run["status"], "finished")
        self.assertEqual(len(self.requests), 40)
        self.assertGreater(run["progress"]["valid"], 0)

    def test_fixed_adapter_does_not_probe_or_expand(self):
        manager = self.manager()
        run = self.collect(manager, adapter="openai")
        self.assertEqual(run["status"], "finished")
        self.assertEqual(len(self.requests), 40)
        for request in self.requests:
            body = json.loads(request.content)
            self.assertEqual(body["reasoning_effort"], "none")
            self.assertEqual(body["max_completion_tokens"], 16)
            self.assertNotIn("max_tokens", body)

    def test_native_reasoning_uses_explicit_budget_and_only_final_answer(self):
        def provider(request):
            body = json.loads(request.content)
            if "temperature" in body or "top_p" in body or body.get("reasoning_effort") == "none":
                return httpx.Response(400, json={"error": "synthetic unsupported sampling parameter"})
            budget = body.get("max_completion_tokens", body.get("max_tokens"))
            if budget < 128:
                return success(request, choices=[{"message": {"content": None, "reasoning_content": "synthetic thought"}, "finish_reason": "length"}],
                               usage={"completion_tokens": budget, "completion_tokens_details": {"reasoning_tokens": budget}})
            return success(request, choices=[{"message": {"content": answer_for(body), "reasoning_content": "never an answer or saved raw thought"}, "finish_reason": "stop"}],
                           usage={"prompt_tokens": 20, "completion_tokens": 129, "completion_tokens_details": {"reasoning_tokens": 128}})
        for adapter in ("openai_reasoning", "deepseek_reasoning"):
            with self.subTest(adapter=adapter):
                manager = self.manager(provider)
                self.requests.clear()
                endpoint = {"url": "https://api.deepseek.com" if adapter == "deepseek_reasoning" else "https://synthetic.invalid/v1",
                            "model": "synthetic-always-thinking", "api_key": "synthetic-test-secret"}
                run = self.collect(manager, adapter=adapter, endpoint=endpoint)
                self.assertEqual(run["status"], "finished")
                self.assertEqual(run["progress"]["valid"], 40)
                self.assertEqual(len(self.requests), 40)
                for request in self.requests:
                    body = json.loads(request.content)
                    self.assertEqual(body[run["protocol"]["token_field"]], 4096)
                    self.assertEqual(body["reasoning_effort"], "low")
                    self.assertNotIn("temperature", body)
                    self.assertNotIn("top_p", body)
                    if adapter == "deepseek_reasoning":
                        self.assertEqual(body["thinking"], {"type": "enabled"})
                rows = manager.samples(run["id"])["samples"]
                self.assertTrue(all(row["category"] == "valid" and row["reasoning_observed"] for row in rows))
                self.assertNotIn("never an answer", manager._samples_path(run["id"]).read_text())
                self.assertEqual(run["diagnostics"]["usage"]["reasoning_tokens"], 5120)
                self.assertEqual(run["diagnostics"]["token_budget_exceeded_count"], 0)

    def test_reasoning_budget_exhaustion_does_not_expand_or_retry(self):
        def provider(request):
            return success(request, choices=[{"message": {"content": "", "reasoning_content": "synthetic thought"}, "finish_reason": "length"}],
                           usage={"completion_tokens": 4096, "completion_tokens_details": {"reasoning_tokens": 4096}})
        manager = self.manager(provider)
        run = self.collect(manager, adapter="openai_reasoning", concurrency=1, mode="reference", trusted_source=True, source_note="合成思考来源")
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(run["status"], "error")
        self.assertIn("4096", run["error"])
        self.assertEqual(manager.list_references(), [])
        self.assertEqual(run["diagnostics"]["usage"]["completion_tokens"], 3 * 4096)
        self.assertTrue(all(json.loads(r.content)["max_completion_tokens"] == 4096 for r in self.requests))

    def test_glm_official_incompatible_protocol_is_rejected_before_sending(self):
        manager = self.manager()
        for host in ("api.z.ai", "open.bigmodel.cn"):
            for model in ("glm-5.3", "glm-5.3-flash", "glm-5.3-flashx"):
                for adapter in ("standard", "zhipu", "openai_reasoning"):
                    with self.subTest(host=host, model=model, adapter=adapter):
                        endpoint = {"url": "https://" + host + "/api/paas/v4", "model": model, "api_key": "synthetic-test-secret"}
                        with self.assertRaisesRegex(ValueError, "不能关闭思考.*4096"):
                            manager.create(self.config(endpoint=endpoint, adapter=adapter))
                        service.validate_endpoint_protocol(endpoint, make_protocol({"adapter": "zhipu_reasoning"}))
        self.assertEqual(self.requests, [])
        # Exact host and model matches only; an alias is not evidence of provider semantics.
        for host, model in (("api.z.ai.evil.invalid", "glm-5.3-flash"), ("synthetic.invalid", "glm-5.3-flash"), ("api.z.ai", "glm-5.3-custom"), ("api.z.ai", "glm-5.2")):
            service.validate_endpoint_protocol({"url": "https://" + host, "model": model}, make_protocol({"adapter": "zhipu"}))

    def test_glm_flash_mock_reproduces_empty_body_then_uses_explicit_thinking_protocol(self):
        def provider(request):
            body = json.loads(request.content)
            if body.get("thinking", {}).get("type") == "disabled":
                return httpx.Response(400, json={"error": "synthetic GLM-5.3-Flash only supports enabled thinking"})
            if body["max_tokens"] == 16:
                return success(request, choices=[{"message": {"content": "", "reasoning_content": "synthetic thought"}, "finish_reason": "length"}],
                               usage={"prompt_tokens": 20, "completion_tokens": 16})
            self.assertEqual(body["max_tokens"], 4096)
            self.assertEqual(body["thinking"], {"type": "enabled"})
            self.assertEqual(body["reasoning_effort"], "low")
            self.assertNotIn("max_completion_tokens", body)
            self.assertNotIn("temperature", body)
            self.assertNotIn("top_p", body)
            return success(request, choices=[{"message": {"content": answer_for(body), "reasoning_content": "synthetic thought never saved"}, "finish_reason": "stop"}],
                           usage={"prompt_tokens": 20, "completion_tokens": 129})

        manager = self.manager(provider)
        endpoint = {"url": "https://synthetic.invalid/v1", "model": "glm-5.3-flash", "api_key": "synthetic-test-secret"}
        original = self.collect(manager, endpoint=endpoint, concurrency=1)
        self.assertEqual(original["status"], "error")
        self.assertEqual(original["progress"]["done"], 3)
        self.assertEqual(original["progress"]["valid"], 0)
        self.assertIn("不能关闭思考", original["error"])
        self.assertIn("4096", original["error"])
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(original["diagnostics"]["usage"]["completion_tokens"], 48)
        disabled = self.collect(manager, endpoint=endpoint, concurrency=1, adapter="zhipu")
        self.assertEqual(disabled["status"], "error")
        self.assertEqual(len(self.requests), 4)  # No parameter fallback or retry after rejection.
        self.requests.clear()
        endpoint["url"] = "https://open.bigmodel.cn/api/paas/v4"
        accepted = self.collect(manager, endpoint=endpoint, adapter="zhipu_reasoning")
        self.assertEqual(accepted["status"], "finished")
        self.assertEqual(accepted["progress"]["valid"], 40)
        self.assertEqual(len(self.requests), 40)
        self.assertIsNone(accepted["diagnostics"]["usage"]["reasoning_tokens"])
        self.assertEqual(accepted["diagnostics"]["usage"]["completion_tokens"], 5160)
        self.assertNotIn("synthetic thought", manager._samples_path(accepted["id"]).read_text())
        with self.assertRaisesRegex(ValueError, "确认"):
            manager.create(self.config(endpoint=endpoint, adapter="zhipu_reasoning", risk_acknowledged=False))

    def test_reasoning_references_freeze_repeat_import_and_comparison(self):
        manager = self.manager()
        baseline = self.collect(manager, adapter="openai_reasoning", mode="reference", trusted_source=True, source_note="合成思考参考")
        reference = manager.list_references()[0]
        candidate = self.collect(manager, mode="verify", adapter="openai_reasoning", reference_id=reference["id"])
        self.assertEqual(candidate["protocol"], baseline["protocol"])
        self.assertTrue(candidate["comparison"]["protocol_compatible"])
        self.assertEqual(candidate["comparison"]["verdict"], "incomparable")
        repeated = self.complete(manager, manager.create(self.config(), frozen_protocol=baseline["protocol"]))
        self.assertEqual(repeated["protocol"], baseline["protocol"])
        with self.assertRaisesRegex(ValueError, "冻结协议一致"):
            manager.create(self.config(mode="verify", reference_id=reference["id"], adapter="standard"))

    def test_typed_reasoning_content_is_never_counted_as_answer(self):
        def only_reasoning(request):
            return success(request, choices=[{"message": {"content": [{"type": "reasoning", "text": answer_for(json.loads(request.content))}]}, "finish_reason": "stop"}])
        manager = self.manager(only_reasoning)
        run = self.collect(manager, adapter="openai_reasoning")
        self.assertEqual(run["progress"]["valid"], 0)
        self.assertTrue(run["diagnostics"]["reasoning_observed"])
        self.assertTrue(all(row["raw"] == "" for row in manager.samples(run["id"])["samples"]))
        message = {"content": [{"type": "reasoning", "text": "cat"}, {"type": "text", "text": "dog"}]}
        self.assertEqual(service._content(message), "dog")

    def test_reasoning_rejected_parameter_does_not_fallback(self):
        manager = self.manager(lambda request: httpx.Response(400, json={"error": "synthetic unsupported effort"}))
        run = self.collect(manager, adapter="openai_reasoning", concurrency=1)
        self.assertEqual(run["status"], "error")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(json.loads(self.requests[0].content)["reasoning_effort"], "low")

    def test_inline_thought_tags_cannot_be_normalized_into_one_word_answers(self):
        # Without the content boundary guard, <think>cat</think> normalizes
        # into a single Latin word and incorrectly passes an open-word probe.
        for text in ("<think>cat</think>", "<THINK>cat</THINK>dog", "<analysis>cat</analysis>"):
            with self.subTest(text=text):
                row = {"cell_id": "random-animal:en", "error": None}
                service.FingerprintManager._decode_response(row, {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]})
                self.assertEqual(row["category"], "invalid")
                self.assertIsNone(row["normalized"])
                self.assertTrue(row["reasoning_observed"])
                self.assertIn("未作为最终答案", row["error"])

    def test_upstream_cookies_never_become_request_history(self):
        def set_cookie(request):
            response = success(request)
            response.headers["Set-Cookie"] = "session=previous-turn; Path=/"
            return response
        manager = self.manager(set_cookie)
        run = self.collect(manager, concurrency=1)
        self.assertEqual(run["status"], "finished")
        self.assertTrue(all("cookie" not in request.headers for request in self.requests))

    def test_timeouts_record_samples_without_retry(self):
        def timed_out(request):
            raise asyncio.TimeoutError()
        manager = self.manager(timed_out)
        run = self.collect(manager)
        self.assertEqual(run["progress"]["errors"], 40)
        self.assertEqual(len(self.requests), 40)
        self.assertTrue(all("超时" in sample["error"] for sample in manager.samples(run["id"])["samples"]))

    def test_rate_limits_never_retry(self):
        manager = self.manager(lambda request: httpx.Response(429, headers={"Retry-After": "1"}, json={"error": "synthetic"}))
        run = self.collect(manager)
        self.assertEqual(len(self.requests), 40)
        self.assertEqual(run["progress"]["errors"], 40)
        self.assertTrue(all(s["status_code"] == 429 for s in manager.samples(run["id"])["samples"]))

    def test_rejected_parameters_fail_fast_without_fallback(self):
        manager = self.manager(lambda request: httpx.Response(400, json={"error": "remove reasoning field"}))
        run = self.collect(manager, adapter="openai")
        self.assertEqual(run["status"], "error")
        self.assertLessEqual(len(self.requests), 2)
        self.assertFalse(run["artifact"]["complete"])
        self.assertTrue(all(json.loads(r.content)["reasoning_effort"] == "none" for r in self.requests))
        self.assertFalse(manager._runtimes)

    def test_auth_failure_does_not_store_upstream_error_body(self):
        manager = self.manager(lambda request: httpx.Response(401, json={"error": "synthetic-test-secret"}))
        run = self.collect(manager)
        self.assertEqual(run["status"], "error")
        self.assertLessEqual(len(self.requests), 2)
        self.assertNotIn("synthetic-test-secret", json.dumps(run))
        self.assertNotIn("synthetic-test-secret", manager._samples_path(run["id"]).read_text())

    def test_transport_errors_do_not_leak_sensitive_exception(self):
        def broken(request):
            raise httpx.ConnectError("network detail synthetic-test-secret", request=request)
        manager = self.manager(broken)
        run = self.collect(manager)
        self.assertEqual(len(self.requests), 40)
        self.assertEqual(run["progress"]["errors"], 40)
        self.assertNotIn("synthetic-test-secret", manager._samples_path(run["id"]).read_text())

    def test_concurrency_and_one_run_at_a_time(self):
        active = 0
        maximum = 0
        completed = []
        async def delayed(request):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            try:
                await asyncio.sleep(.005)
                completed.append(json.loads(request.content)["model"])
                return success(request)
            finally:
                active -= 1
        limiter = Limiter()
        manager = self.manager(delayed, limiter=limiter)
        first = manager.create(self.config(concurrency=3))
        second_cfg = self.config(concurrency=3)
        second_cfg["endpoint"]["model"] = "second-model"
        second = manager.create(second_cfg)
        self.complete(manager, first)
        self.complete(manager, second)
        self.assertEqual(maximum, 3)
        self.assertEqual(completed, ["synthetic-model"] * 40 + ["second-model"] * 40)
        self.assertEqual(limiter.acquisitions, [first["id"], second["id"]])
        self.assertEqual(limiter.releases, [first["id"], second["id"]])
        self.assertFalse(limiter.active)

    def test_stop_waiting_never_sends_requests(self):
        manager = self.manager(limiter=Limiter(blocked=True))
        run = manager.create(self.config())
        stopped = manager.stop(run["id"])
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(stopped["progress"]["done"], 0)
        self.assertIsNotNone(stopped["timing"]["stop_requested_at"])
        self.assertIsNotNone(stopped["timing"]["ended_at"])
        self.assertIsNone(stopped["timing"]["started_at"])
        self.assertIsNone(stopped["timing"]["elapsed_ms"])
        self.assertFalse(manager._runtimes)
        self.assertEqual(self.requests, [])
        self.assertFalse(manager.export(run["id"])["complete"])

    def test_stop_cancels_inflight_and_preserves_responses(self):
        async def delayed(request):
            await asyncio.sleep(.025 if len(self.requests) < 4 else 20)
            return success(request)
        limiter = Limiter()
        manager = self.manager(delayed, limiter=limiter)
        run = manager.create(self.config())
        self.wait(lambda: manager.get(run["id"])["progress"]["done"] >= 2)
        stopped = manager.stop(run["id"])
        self.assertIn(stopped["status"], {"stopping", "stopped"})
        final = self.complete(manager, run)
        self.assertEqual(final["status"], "stopped")
        self.assertGreaterEqual(final["progress"]["valid"], 2)
        self.assertLess(final["progress"]["done"], 40)
        self.assertEqual(final["progress"]["done"], manager.samples(run["id"])["total"])
        self.assertFalse(final["artifact"]["complete"])
        self.assertIsNotNone(final["timing"]["stop_requested_at"])
        self.assertIsNotNone(final["timing"]["ended_at"])
        for row in manager.samples(run["id"])["samples"]:
            self.assertIsNotNone(row["timing"]["ended_at"])
            self.assertGreaterEqual(row["timing"]["total_ms"], 0)
        self.assertFalse(manager._runtimes)
        self.assertFalse(limiter.active)
        self.assertEqual(manager.stop(run["id"])["status"], "stopped")

    def test_reference_collection_export_import_offline_compare_and_delete(self):
        manager = self.manager()
        baseline = self.collect(manager, mode="reference", trusted_source=True, source_note="合成测试可信来源")
        self.assertEqual(baseline["status"], "finished", baseline["error"])
        reference_id = baseline["reference_id"]
        reference = manager.get_reference(reference_id)
        self.assertIs(reference["trusted_source"], True)
        self.assertEqual(reference["artifact"]["source"]["source_note"], "合成测试可信来源")
        summary = manager.list_references()[0]
        for field in ("protocol", "collected_at", "complete", "diagnostics", "endpoint", "trusted_source"):
            self.assertIn(field, summary)
        candidate = self.collect(manager, mode="verify", reference_id=reference_id)
        self.assertEqual(candidate["comparison"]["verdict"], "match", candidate["comparison"])
        request_count = len(self.requests)
        comparison = manager.compare(candidate["id"], reference_id)
        self.assertEqual(comparison["verdict"], "match")
        self.assertEqual(len(self.requests), request_count)
        imported = manager.import_reference(manager.export(baseline["id"]), "导入参考", "合成记录", True)
        self.assertEqual(imported["artifact"]["protocol"], baseline["protocol"])
        manager.delete(baseline["id"])
        self.assertIsNotNone(manager.get_reference(reference_id))
        self.assertIsNone(manager.get(baseline["id"]))
        manager.delete_reference(imported["id"])
        self.assertIsNone(manager.get_reference(imported["id"]))

    def test_compare_rejects_invalid_reference_id_types(self):
        manager = self.manager()
        run = self.collect(manager)
        for reference_id in ([], {}, None, True, 123, "", "not-an-id"):
            with self.subTest(reference_id=reference_id):
                with self.assertRaisesRegex(ValueError, "参考编号无效"):
                    manager.compare(run["id"], reference_id)
        self.assertIsNone(manager.get(run["id"])["comparison"])
        self.assertEqual(len(self.requests), 40)

    def test_import_size_limit_is_two_mebibytes(self):
        manager = self.manager()
        run = self.collect(manager)
        evidence = manager.export(run["id"])
        self.assertEqual(service.MAX_IMPORT_BYTES, 2 * 1024 * 1024)
        evidence["ignored_padding"] = "x" * service.MAX_IMPORT_BYTES
        with self.assertRaisesRegex(ValueError, "2 MiB"):
            manager.import_reference(evidence, "超限参考", "", False)
        self.assertEqual(manager.list_references(), [])
        evidence["ignored_padding"] = "x"
        self.assertIsNotNone(manager.import_reference(evidence, "有效参考", "", False))

    def test_untrusted_reference_never_yields_strict_match(self):
        manager = self.manager()
        run = self.collect(manager)
        imported = manager.import_reference(manager.export(run["id"]), "未验证来源", "", False)
        comparison = manager.compare(run["id"], imported["id"])
        self.assertEqual(comparison["verdict"], "uncertain")
        self.assertTrue(any("来源" in warning for warning in comparison["warnings"]))

    def test_cannot_delete_reference_in_use_or_active_run(self):
        limiter = Limiter()
        manager = self.manager(limiter=limiter)
        baseline = self.collect(manager, mode="reference", trusted_source=True, source_note="合成来源")
        limiter.blocked = True
        run = manager.create(self.config(mode="verify", reference_id=baseline["reference_id"]))
        with self.assertRaises(ValueError):
            manager.delete_reference(baseline["reference_id"])
        with self.assertRaises(ValueError):
            manager.delete(run["id"])
        with self.assertRaises(ValueError):
            manager.export(run["id"])
        with self.assertRaises(ValueError):
            manager.compare(run["id"], baseline["reference_id"])
        manager.stop(run["id"])
        manager.delete_reference(baseline["reference_id"])

    def test_restart_recovers_progress_but_never_resumes(self):
        manager = self.manager()
        run = self.collect(manager)
        manager.close()
        metadata = manager._runs[run["id"]]
        metadata["status"] = "running"
        metadata["progress"]["done"] = 5
        metadata["artifact"] = None
        manager._save_run(metadata)
        restored = self.manager()
        recovered = restored.get(run["id"])
        self.assertEqual(recovered["status"], "interrupted")
        self.assertEqual(recovered["progress"]["done"], 40)
        self.assertFalse(recovered["artifact"]["complete"])
        self.assertEqual(len(self.requests), 40)
        self.assertFalse(restored._runtimes)
        again = self.manager()
        self.assertEqual(again.get(run["id"])["status"], "interrupted")

    def test_corruption_isolated_and_partial_sample_recovery_warns(self):
        manager = self.manager()
        run = self.collect(manager)
        manager.close()
        (manager.runs_dir / ('a' * 32 + '.json')).write_text('{broken')
        (manager.references_dir / ('b' * 32 + '.json')).write_text('[]')
        metadata = manager._runs[run["id"]]
        metadata["status"] = "running"
        manager._save_run(metadata)
        with manager._samples_path(run["id"]).open('a') as stream:
            stream.write('{partial')
        restored = self.manager()
        recovered = restored.get(run["id"])
        self.assertEqual(recovered["status"], "interrupted")
        self.assertEqual(recovered["progress"]["done"], 40)
        self.assertIn("损坏", recovered["storage_warning"])
        self.assertEqual(len(restored.storage_warnings), 2)
        self.assertEqual(len(list(restored.data_dir.rglob('*.corrupt-*'))), 2)

    def test_corrupt_run_dates_are_quarantined_without_breaking_healthy_history(self):
        manager = self.manager()
        healthy = self.collect(manager)
        manager.close()
        original = copy.deepcopy(manager._runs[healthy["id"]])
        corruptions = [("created", []), ("created", "nonsense"), ("started_at", "2026-09-15T08:00:00"),
                       ("finished_at", {"unexpected": "object"}), ("artifact", "bad-collected-at")]
        for index, (field, value) in enumerate(corruptions, 1):
            corrupt = copy.deepcopy(original)
            corrupt["id"] = f"{index:032x}"
            if field == "artifact":
                corrupt["artifact"]["collected_at"] = value
            else:
                corrupt[field] = value
            manager._save_run(corrupt)
        restored = self.manager()
        self.assertEqual([run["id"] for run in restored.list_runs()], [healthy["id"]])
        self.assertEqual(len(restored.storage_warnings), len(corruptions))
        self.assertEqual(len(list(restored.runs_dir.glob("*.corrupt-*"))), len(corruptions))
        self.assertIsNotNone(restored.export(healthy["id"]))

    def test_corrupt_reference_created_date_is_quarantined(self):
        manager = self.manager()
        run = self.collect(manager, mode="reference", trusted_source=True, source_note="合成来源")
        manager.close()
        reference = manager.get_reference(run["reference_id"])
        reference["created"] = "bad-date"
        service._atomic_json(manager._reference_path(reference["id"]), reference)
        restored = self.manager()
        self.assertEqual(restored.list_references(), [])
        self.assertIsNotNone(restored.get(run["id"]))
        self.assertEqual(len(restored.storage_warnings), 1)

    def test_reference_survives_restart(self):
        manager = self.manager()
        run = self.collect(manager, mode="reference", trusted_source=True, source_note="合成来源")
        manager.close()
        restored = self.manager()
        reference = restored.get_reference(run["reference_id"])
        self.assertIsNotNone(reference)
        self.assertTrue(reference["trusted_source"])
        self.assertEqual(reference["artifact"]["protocol"], run["protocol"])

    def test_usage_refusal_content_parts_and_truncation(self):
        sequence = 0
        def special(request):
            nonlocal sequence
            sequence += 1
            text = answer_for(json.loads(request.content))
            message = {"content": [{"type": "text", "text": text}], "reasoning_content": "synthetic reasoning"}
            if sequence % 3 == 0:
                message["refusal"] = "synthetic refusal"
            return success(request, choices=[{"message": message, "finish_reason": "length" if sequence % 3 == 1 else "stop"}],
                           usage={"prompt_tokens": 10, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 0 if sequence % 2 else 8},
                                  "completion_tokens_details": {"reasoning_tokens": 1}, "cost": .0001})
        manager = self.manager(special)
        run = self.collect(manager)
        samples = manager.samples(run["id"])["samples"]
        self.assertTrue(any(s["category"] == "refusal" for s in samples))
        self.assertTrue(any(s["category"] == "valid" for s in samples))
        self.assertTrue(all(s["category"] == "invalid" for s in samples if s["truncated"]))
        self.assertTrue(run["diagnostics"]["reasoning_observed"])
        self.assertEqual(run["diagnostics"]["usage"]["prompt_tokens"], 400)
        self.assertEqual(run["diagnostics"]["cache"]["cached_samples"], 20)
        self.assertEqual(run["diagnostics"]["cache"]["uncached_samples"], 20)
        self.assertEqual(run["diagnostics"]["cache"]["unknown_samples"], 0)

    def test_truncated_refusal_is_always_invalid(self):
        manager = self.manager(lambda request: success(request, choices=[{"message": {"content": "", "refusal": "synthetic refusal"}, "finish_reason": "length"}]))
        run = self.collect(manager)
        samples = manager.samples(run["id"])["samples"]
        self.assertTrue(all(s["truncated"] and s["category"] == "invalid" for s in samples))

    def test_unknown_and_invalid_usage_stays_null(self):
        values = service._usage({"usage": {"prompt_tokens": True, "completion_tokens": -2, "reasoning_tokens": float('nan'), "cost": "1", "cached_tokens": 10 ** 1000}})
        self.assertEqual(values, dict.fromkeys(service.USAGE_KEYS))
        self.assertEqual(service._usage({"usage": {"prompt_tokens": 0}})["prompt_tokens"], 0)

    def test_sample_limits_and_raw_length(self):
        manager = self.manager(lambda request: success(request, choices=[{"message": {"content": "a" * 4097}, "finish_reason": "stop"}]))
        run = self.collect(manager)
        page = manager.samples(run["id"], offset=10, limit=5)
        self.assertEqual(page["total"], 40)
        self.assertEqual([s["index"] for s in page["samples"]], list(range(10, 15)))
        self.assertTrue(all(len(s["raw"]) == 4096 and s["truncated"] and s["category"] == "invalid" for s in page["samples"]))
        self.assertEqual(manager.samples(run["id"], offset=500)["samples"], [])
        for offset, limit in ((-1, 10), (0, 0), (0, 501), (True, 10), (0, False)):
            with self.assertRaises(ValueError):
                manager.samples(run["id"], offset, limit)

    def test_response_size_limit_and_invalid_structure(self):
        count = 0
        def oversized(request):
            nonlocal count
            count += 1
            return httpx.Response(200, content=b'x' * (service.MAX_RESPONSE_BYTES + 1)) if count % 2 else httpx.Response(200, json={"choices": []})
        manager = self.manager(oversized)
        run = self.collect(manager)
        self.assertEqual(run["progress"]["errors"], 40)
        self.assertTrue(any("上限" in s["error"] for s in manager.samples(run["id"])["samples"]))

    def test_upstream_echo_of_key_is_redacted(self):
        manager = self.manager(lambda request: success(request, id="synthetic-test-secret", model="synthetic-test-secret",
                                                      choices=[{"message": {"content": "synthetic-test-secret"}, "finish_reason": "stop"}]))
        run = self.collect(manager)
        for path in manager.data_dir.rglob('*'):
            if path.is_file():
                self.assertNotIn("synthetic-test-secret", path.read_text())
        self.assertNotIn("synthetic-test-secret", json.dumps(manager.export(run["id"])))

    def test_unexpected_tool_call_is_invalid(self):
        manager = self.manager(lambda request: success(request, choices=[{"message": {"content": "7", "tool_calls": [{"id": "synthetic"}]}, "finish_reason": "tool_calls"}]))
        run = self.collect(manager)
        self.assertEqual(run["progress"]["valid"], 0)
        self.assertTrue(all(s["category"] == "invalid" for s in manager.samples(run["id"])["samples"]))

    def test_url_and_input_rejections(self):
        manager = self.manager(limiter=Limiter(blocked=True))
        invalid = [self.config(risk_acknowledged=False), self.config(mode="unknown"), self.config(mode=[]), self.config(mode="verify", reference_id={}), self.config(mode="reference"),
                   self.config(mode="reference", trusted_source=True), self.config(extra_body={"tools": []}),
                   self.config(concurrency=21), self.config(samples_per_cell=9), self.config(timeout_seconds=4),
                   self.config(adapter="automatic"), self.config(mode="verify", reference_id="does-not-exist")]
        for url in ("file:///tmp/secrets", "https://user:key@synthetic.invalid", "https://synthetic.invalid?key=secret", "https://synthetic.invalid#secret", "https://synthetic.invalid:99999", "https://synthetic.invalid/\\bad"):
            cfg = self.config()
            cfg["endpoint"]["url"] = url
            invalid.append(cfg)
        cfg = self.config()
        cfg["endpoint"]["headers"] = {"Injected": "yes"}
        invalid.append(cfg)
        for config in invalid:
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    manager.create(config)
        self.assertEqual(self.requests, [])
        self.assertEqual(manager.list_runs(), [])

    def test_empty_api_key_supported_for_local_unauthed_endpoint(self):
        manager = self.manager()
        cfg = self.config()
        cfg["endpoint"].update({"url": "http://127.0.0.1:9999", "api_key": ""})
        run = self.complete(manager, manager.create(cfg))
        self.assertEqual(run["status"], "finished")
        self.assertTrue(all("authorization" not in r.headers for r in self.requests))
        self.assertEqual(run["endpoint"]["url"], "http://127.0.0.1:9999/v1/chat/completions")

    def test_disk_failure_stops_collection_and_clears_secret(self):
        manager = self.manager()
        with patch.object(manager, '_append_sample', side_effect=OSError('synthetic-test-secret')):
            run = self.collect(manager)
        self.assertEqual(run["status"], "error")
        self.assertLessEqual(len(self.requests), 2)
        self.assertFalse(manager._runtimes)
        self.assertNotIn('synthetic-test-secret', json.dumps(run))

    def test_snapshots_are_detached_and_bad_import_rejected(self):
        manager = self.manager()
        run = self.collect(manager)
        snapshot = manager.get(run["id"])
        snapshot["endpoint"]["model"] = "changed"
        snapshot["protocol"]["max_tokens"] = 999
        self.assertEqual(manager.get(run["id"])["endpoint"]["model"], "synthetic-model")
        self.assertEqual(manager.get(run["id"])["protocol"]["max_tokens"], 16)
        with self.assertRaises(ValueError):
            manager.import_reference({"not": "artifact"}, "bad", "", False)
        with self.assertRaises(ValueError):
            manager.import_reference(run["artifact"], "bad", "", True)
        self.assertEqual(manager.list_references(), [])


class ModelDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.key = "synthetic-discovery-secret"
        self.config = {"url": "https://synthetic.invalid/v1", "api_key": self.key}

    def discover(self, handler, config=None, *, cookies=None):
        async def capture(request):
            self.requests.append(request)
            result = handler(request)
            return await result if asyncio.iscoroutine(result) else result
        with patch.object(service, '_model_discovery_client', lambda: httpx.AsyncClient(
                transport=httpx.MockTransport(capture), follow_redirects=False, trust_env=False, cookies=cookies)):
            return service.discover_models(self.config if config is None else config)

    def test_one_get_preserves_exact_ids_deduplicates_and_does_not_inherit_cookies(self):
        payload = {"data": [{"id": "vendor/Model-XL:latest"}, {"id": "模型甲"}, {"id": "vendor/Model-XL:latest"},
                            {"id": "Vendor/Model-XL:latest"}, {"id": "Model With Space"}],
                   "api_key": self.key, "unrelated": "must not pass through"}
        result = self.discover(lambda request: httpx.Response(200, json=payload), cookies={"session": "previous-conversation"})
        self.assertEqual(result, {"models": ["vendor/Model-XL:latest", "模型甲", "Vendor/Model-XL:latest", "Model With Space"],
                                  "endpoint": "https://synthetic.invalid/v1/models"})
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(str(request.url), "https://synthetic.invalid/v1/models")
        self.assertEqual(request.content, b"")
        self.assertEqual(request.headers['Authorization'], "Bearer " + self.key)
        self.assertNotIn('cookie', request.headers)
        self.assertNotIn(self.key, json.dumps(result))

    def test_discovery_default_client_disables_proxies_and_redirects(self):
        original = httpx.AsyncClient
        options = []
        async def upstream(request):
            self.requests.append(request)
            return httpx.Response(200, json={"data": []})
        def factory(**kwargs):
            options.append(kwargs)
            return original(transport=httpx.MockTransport(upstream), **kwargs)
        with patch.object(service.httpx, "AsyncClient", side_effect=factory):
            result = service.discover_models(self.config)
        self.assertEqual(result["models"], [])
        self.assertFalse(options[0]["trust_env"])
        self.assertFalse(options[0]["follow_redirects"])
        self.assertEqual(options[0]["timeout"].read, 15)
        self.assertEqual(len(self.requests), 1)

    def test_known_base_paths_map_to_one_models_endpoint_without_probing(self):
        urls = {"https://synthetic.invalid": "https://synthetic.invalid/v1/models",
                "https://synthetic.invalid/v1/": "https://synthetic.invalid/v1/models",
                "https://synthetic.invalid/v1/chat/completions": "https://synthetic.invalid/v1/models",
                "https://synthetic.invalid/api/v2/chat/completions/": "https://synthetic.invalid/api/v2/models"}
        for url, expected in urls.items():
            before = len(self.requests)
            result = self.discover(lambda request: httpx.Response(200, json={"data": []}), {"url": url, "api_key": ""})
            self.assertEqual(result["endpoint"], expected)
            self.assertEqual(len(self.requests), before + 1)
            self.assertNotIn('authorization', self.requests[-1].headers)

    def test_invalid_entries_and_key_echoes_are_not_returned_as_models(self):
        data = [None, 42, "raw-model-string", {}, {"id": None}, {"id": 7}, {"id": ""}, {"id": "   "},
                {"id": " leading"}, {"id": "trailing "}, {"id": "bad\nname"}, {"id": "bad\x7fname"},
                {"id": "x" * 257}, {"id": self.key}, {"id": "prefix-" + self.key}, {"id": "Valid-ID"}]
        result = self.discover(lambda request: httpx.Response(200, json={"data": data}))
        self.assertEqual(result["models"], ["Valid-ID"])
        self.assertNotIn(self.key, json.dumps(result))

    def test_http_failures_are_readable_do_not_retry_or_follow_redirects(self):
        for status in (401, 403, 404, 405, 501, 302, 307, 429, 500):
            with self.subTest(status=status):
                before = len(self.requests)
                with self.assertRaises(ValueError) as error:
                    self.discover(lambda request: httpx.Response(status, headers={"Location": "https://other.invalid/models"},
                                                                json={"error": self.key}))
                self.assertIn("手动填写", str(error.exception))
                self.assertNotIn(self.key, str(error.exception))
                self.assertEqual(len(self.requests), before + 1)

    def test_invalid_config_never_sends_a_request(self):
        invalid = [[], {}, {**self.config, "model": "not-allowed"}, {**self.config, "headers": {}},
                   {**self.config, "timeout_seconds": 60}, {**self.config, "api_key": []},
                   {**self.config, "url": "https://user:password@synthetic.invalid/v1"},
                   {**self.config, "url": "https://synthetic.invalid?key=" + self.key},
                   {**self.config, "url": "https://synthetic.invalid/" + self.key}]
        for config in invalid:
            with self.subTest(config=config):
                with self.assertRaises(ValueError) as error:
                    self.discover(lambda request: httpx.Response(200, json={"data": []}), config)
                self.assertNotIn(self.key, str(error.exception))
        self.assertEqual(self.requests, [])

    def test_malformed_response_and_schema_rejection_never_echo_response(self):
        responses = [httpx.Response(200, content=self.key.encode()), httpx.Response(200, json=[]),
                     httpx.Response(200, json={"data": self.key}), httpx.Response(200, json={"models": []})]
        for response in responses:
            before = len(self.requests)
            with self.assertRaises(ValueError) as error:
                self.discover(lambda request: response)
            self.assertIn("手动填写", str(error.exception))
            self.assertNotIn(self.key, str(error.exception))
            self.assertEqual(len(self.requests), before + 1)

    def test_response_size_limit_stops_reading(self):
        with self.assertRaisesRegex(ValueError, "1 MiB"):
            self.discover(lambda request: httpx.Response(200, content=b'x' * (service.MAX_RESPONSE_BYTES + 1)))
        self.assertEqual(len(self.requests), 1)

    def test_network_and_unexpected_errors_never_expose_key(self):
        for exception in (httpx.ReadTimeout(self.key), httpx.ConnectError(self.key), ValueError(self.key), RuntimeError(self.key)):
            before = len(self.requests)
            def broken(request):
                raise exception
            with self.assertRaises(ValueError) as error:
                self.discover(broken)
            self.assertIn("手动填写", str(error.exception))
            self.assertNotIn(self.key, str(error.exception))
            self.assertEqual(len(self.requests), before + 1)

    def test_total_timeout_cancels_slow_response_and_closes_stream(self):
        class SlowStream(httpx.AsyncByteStream):
            closed = False
            async def __aiter__(self):
                await asyncio.sleep(10)
                yield b'{"data":[]}'
            async def aclose(self):
                self.closed = True
        stream = SlowStream()
        with patch.object(service, 'MODEL_DISCOVERY_TIMEOUT_SECONDS', .02):
            started = time.monotonic()
            with self.assertRaisesRegex(ValueError, "超时"):
                self.discover(lambda request: httpx.Response(200, stream=stream))
            self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(stream.closed)
        self.assertEqual(len(self.requests), 1)

    def test_discovery_does_not_create_files_or_write_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(service, '_atomic_json', side_effect=AssertionError("discovery must not persist")):
                result = self.discover(lambda request: httpx.Response(200, json={"data": [{"id": "model"}]}))
            self.assertEqual(result["models"], ["model"])
            self.assertEqual(list(Path(directory).rglob('*')), [])


if __name__ == '__main__':
    unittest.main()
