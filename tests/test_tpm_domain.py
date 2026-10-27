"""TPM 领域回归：全部使用本地合成用量，不连接付费接口。"""
import copy
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from tpm_config import DEFAULTS, build_plan, preview, validate_config
from tpm_corpus import Corpus
from tpm_usage import BudgetLedger, calculate_cost, merge_usage, normalize_usage, worst_case_cost


def config(**overrides):
    raw = {"url": "http://127.0.0.1:9999/v1", "model": "synthetic-model", "api_key": "local-only",
           "cache_write_price_mode": "input"}
    raw.update(overrides)
    return validate_config(raw)


def openai_usage(prompt=49500, output=500, cached=0):
    return normalize_usage({"prompt_tokens": prompt, "completion_tokens": output,
                            "prompt_tokens_details": {"cached_tokens": cached}, "total_tokens": prompt + output})


class TpmConfigurationTests(unittest.TestCase):
    def test_offline_defaults_need_no_secret_and_do_not_mutate_defaults(self):
        before = copy.deepcopy(DEFAULTS)
        with mock.patch("socket.socket", side_effect=AssertionError("offline")):
            cfg = validate_config({}, require_secret=False)
            plan = preview(cfg)
            direct = preview(DEFAULTS)
        self.assertEqual(before, DEFAULTS)
        self.assertEqual(plan["total_planned_tokens"], 400_000_000)
        self.assertEqual(direct["duration_seconds"], 150)
        self.assertIsNone(plan["request_reserve_usd"])

    def test_real_calibration_requires_explicit_write_price(self):
        raw = {"url": "http://localhost/v1", "model": "m", "api_key": "k"}
        with self.assertRaisesRegex(ValueError, "缓存写入报价"):
            validate_config(raw)
        cfg = validate_config({**raw, "cache_write_price_mode": "explicit", "prices": {"cache_write": 0}})
        self.assertEqual(cfg["prices"]["cache_write"], 0)

    def test_url_credentials_query_fragment_rejected(self):
        for url in ("ftp://localhost", "http://u:p@localhost", "http://localhost?api_key=k", "http://localhost/#secret", "http://localhost:0", "http://bad host"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                config(url=url)

    def test_numeric_validation_does_not_accept_nan_infinity_boolean_or_fractional_concurrency(self):
        for overrides in ({"target_tpm": float("nan")}, {"budget_usd": float("inf")}, {"concurrency": True},
                          {"concurrency": 2.5}, {"timeout_seconds": -1}, {"prices": {"input": -1}}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                config(**overrides)

    def test_context_pool_and_output_limits(self):
        for overrides in ({"context_window_tokens": 4096}, {"pool_connections": 2},
                          {"expected_output_tokens": 2048}, {"template_groups": 128, "input_tokens": 200000, "context_window_tokens": 300000}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                config(**overrides)

    def test_explicit_independent_protocol_confirmation(self):
        with self.assertRaisesRegex(ValueError, "独立缓存协议"):
            config(usage_semantics="independent")
        self.assertEqual(config(usage_semantics="independent", usage_semantics_confirmed=True)["usage_semantics"], "independent")

    def test_default_formal_plan_is_four_hundred_million_and_calibration_recomputes_rps(self):
        cfg = config()
        plan = preview(cfg)
        self.assertEqual(plan["total_planned_tokens"], 400_000_000)
        self.assertAlmostEqual(plan["target_rps"], 200_000_000 / 50_000 / 60)
        measured = preview(cfg, {"mean_input_tokens": 40000, "mean_output_tokens": 200, "p95_seconds": 35})
        self.assertAlmostEqual(measured["target_rps"], 200_000_000 / 40200 / 60)
        self.assertEqual(measured["suggested_target_duration_seconds"], 95)
        self.assertEqual(measured["stages"][-1]["duration_seconds"], 90)
        self.assertTrue(measured["duration_change_requires_confirmation"])
        self.assertEqual(measured["required_concurrency"], 4354)

    def test_avoid_cache_defaults_to_higher_budget_without_assumed_hit(self):
        cfg = config(cache_mode="avoid_cache")
        self.assertEqual(cfg["budget_usd"], 160)
        self.assertEqual(preview(cfg)["expected_cache_hit_rate"], 0)
        self.assertEqual(preview(cfg)["warmup_cost_usd"], 0)

    def test_formal_cost_uses_observed_cache_and_independent_write_usage(self):
        cfg = config(cache_write_price_mode="explicit", prices={"cache_write": 1.5})
        optimistic = preview(cfg)
        measured = preview(cfg, {"mean_input_tokens": 49500, "mean_output_tokens": 500, "cache_hit_rate": 0})
        self.assertGreater(measured["estimated_cost_usd"], optimistic["estimated_cost_usd"])
        self.assertEqual(measured["estimated_cache_hit_rate"], 0)
        writes = preview(cfg, {"mean_input_tokens": 49500, "mean_output_tokens": 500,
                               "mean_normal_input_tokens": 0, "mean_cache_read_tokens": 0,
                               "mean_cache_write_tokens": 49500, "cache_hit_rate": 0})
        self.assertGreater(writes["estimated_cost_usd"], measured["estimated_cost_usd"])

    def test_waiting_does_not_add_seconds_times_max_load_fee(self):
        short = preview(config(timeout_seconds=60))
        long = preview(config(timeout_seconds=600))
        self.assertEqual(short["estimated_cost_usd"], long["estimated_cost_usd"])
        self.assertEqual(short["worst_case_cost_usd"], long["worst_case_cost_usd"])

    def test_actual_body_bandwidth_and_pool_capacity(self):
        plan = preview(config(), {"mean_input_tokens": 49500, "mean_output_tokens": 500, "p95_seconds": 10, "request_body_bytes": 300000})
        self.assertAlmostEqual(plan["estimated_upload_mbps"], 160)
        self.assertEqual(plan["required_concurrency"], 1000)
        self.assertEqual(plan["pool_connections"], 1000)

    def test_auto_execution_defaults_scale_with_target_and_visible_latency_assumption(self):
        cfg = config()
        self.assertEqual(cfg["execution_mode"], "auto")
        self.assertEqual((cfg["concurrency"], cfg["pool_connections"]), (3000, 3000))
        plan = preview(cfg)
        self.assertEqual(plan["execution_plan"]["source"], "assumed_latency")
        self.assertEqual(plan["execution_plan"]["latency_seconds"], 30)
        self.assertTrue(any("规划假设" in warning for warning in plan["warnings"]))
        lower = config(target_tpm=50_000_000)
        self.assertEqual((lower["concurrency"], lower["pool_connections"]), (750, 750))
        shorter = config(planning_latency_seconds=10)
        self.assertEqual((shorter["concurrency"], shorter["pool_connections"]), (1000, 1000))

    def test_auto_execution_recalculates_measured_usage_and_latency_without_mutating_config(self):
        cfg = config()
        before = copy.deepcopy(cfg)
        plan = preview(cfg, {"mean_input_tokens": 39600, "mean_output_tokens": 400, "p95_seconds": 4})
        self.assertAlmostEqual(plan["target_rps"], 200_000_000 / 40_000 / 60)
        self.assertEqual(plan["execution_plan"]["source"], "calibration_p95")
        self.assertEqual((plan["required_concurrency"], plan["concurrency"], plan["pool_connections"]), (500, 500, 500))
        self.assertEqual(cfg, before)

    def test_explicit_auto_ignores_stale_manual_values_and_limits_resources(self):
        cfg = config(execution_mode="auto", concurrency=1, pool_connections=2)
        self.assertEqual((cfg["concurrency"], cfg["pool_connections"]), (3000, 3000))
        plan = preview(cfg, {"p95_seconds": 120})
        self.assertEqual(plan["required_concurrency"], 12000)
        self.assertEqual((plan["concurrency"], plan["pool_connections"]), (8192, 8192))
        self.assertTrue(plan["execution_plan"]["capped"])
        self.assertTrue(any("单机 8192 上限" in warning for warning in plan["warnings"]))
        self.assertEqual(cfg["budget_usd"], 80)

    def test_manual_override_and_legacy_explicit_capacities_are_preserved(self):
        for mode in ({}, {"execution_mode": "manual"}):
            with self.subTest(mode=mode):
                cfg = config(concurrency=3, pool_connections=5, **mode)
                self.assertEqual(cfg["execution_mode"], "manual")
                self.assertEqual(validate_config(cfg), cfg)
                plan = preview(cfg, {"p95_seconds": 10})
                self.assertEqual((plan["concurrency"], plan["pool_connections"]), (3, 5))
                self.assertEqual(plan["required_concurrency"], 1000)
                self.assertTrue(any("手动容量" in warning for warning in plan["warnings"]))
        legacy = config(concurrency=3, pool_connections=5)
        legacy.pop("execution_mode")
        legacy.pop("planning_latency_seconds")
        before = copy.deepcopy(legacy)
        self.assertEqual(preview(legacy)["execution_plan"]["mode"], "manual")
        self.assertEqual(legacy, before)

    def test_execution_planning_has_positive_capacity_and_validated_settings(self):
        self.assertEqual(preview(config(), {"p95_seconds": 0})["concurrency"], 1)
        for changes in ({"execution_mode": "unbounded"}, {"planning_latency_seconds": 0},
                        {"planning_latency_seconds": float("nan")}, {"planning_latency_seconds": True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                config(**changes)

    def test_automatic_capacity_does_not_change_budget_rate_duration_or_fees(self):
        auto = config()
        manual = config(execution_mode="manual", concurrency=1, pool_connections=1)
        measurement = {"mean_input_tokens": 49500, "mean_output_tokens": 500, "p95_seconds": 40}
        first, second = preview(auto, measurement), preview(manual, measurement)
        for key in ("stages", "target_rps", "duration_seconds", "estimated_cost_usd", "worst_case_cost_usd"):
            self.assertEqual(first[key], second[key])
        self.assertEqual(auto["budget_usd"], manual["budget_usd"])
        self.assertTrue(first["requires_confirmation"])

    def test_single_machine_rate_and_request_limits_are_enforced_after_calibration(self):
        with self.assertRaisesRegex(ValueError, "请求/秒上限"):
            config(target_tpm=1_000_000_000, input_tokens=64)
        with self.assertRaisesRegex(ValueError, "条请求上限"):
            config(target_duration_seconds=3600, target_tpm=1_000_000_000)
        with self.assertRaisesRegex(ValueError, "请求/秒上限"):
            preview(config(), {"mean_input_tokens": 1, "mean_output_tokens": 1})

    def test_no_hidden_retry_or_provider_parameters(self):
        cfg = config(retries=12, stream=False, cache_control={"type": "ephemeral"})
        self.assertEqual(cfg["retries"], 0)
        self.assertTrue(cfg["stream"])
        self.assertNotIn("cache_control", cfg)


class TpmUsageTests(unittest.TestCase):
    def test_included_cache_not_added_twice(self):
        usage = openai_usage(49500, 500, 47025)
        self.assertEqual(usage["full_input_tokens"], 49500)
        self.assertEqual(usage["input_tokens"], 2475)
        self.assertEqual(usage["total_tokens"], 50000)
        self.assertTrue(usage["billing_complete"])

    def test_independent_cache_input_and_write_are_disjoint(self):
        usage = normalize_usage({"input_tokens": 100, "cache_read_input_tokens": 300, "cache_creation_input_tokens": 200,
                                 "output_tokens": 50}, "independent")
        self.assertEqual(usage["full_input_tokens"], 600)
        self.assertEqual(usage["total_tokens"], 650)
        self.assertEqual(usage["cache_write_tokens"], 200)
        self.assertTrue(usage["billing_complete"])

    def test_missing_zero_and_unknown_have_separate_meanings(self):
        missing = normalize_usage(None)
        partial = normalize_usage({"prompt_tokens": 0, "completion_tokens": 0})
        zero = openai_usage(0, 0, 0)
        self.assertFalse(missing["complete"])
        self.assertIsNone(missing["full_input_tokens"])
        self.assertTrue(partial["complete"])
        self.assertFalse(partial["billing_complete"])
        self.assertIsNone(partial["cache_read_tokens"])
        self.assertEqual(zero["cache_read_tokens"], 0)
        self.assertTrue(zero["billing_complete"])
        self.assertEqual(zero["field_status"]["cache_write_tokens"], "not_applicable")

    def test_protocol_mismatch_and_invalid_or_inconsistent_usage_is_unreliable(self):
        payloads = [
            {"prompt_tokens": 100, "completion_tokens": 20, "cache_read_input_tokens": 50},
            {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 130},
            {"prompt_tokens": 100, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 101}},
            {"prompt_tokens": True, "completion_tokens": 20},
            {"prompt_tokens": "100", "completion_tokens": 20},
            {"prompt_tokens": 100, "completion_tokens": float("inf")},
        ]
        for raw in payloads:
            with self.subTest(raw=raw):
                usage = normalize_usage(raw)
                self.assertFalse(usage["complete"])
                self.assertTrue(usage["issues"])

    def test_duplicate_stream_snapshots_replace_and_do_not_add(self):
        first = {"prompt_tokens": 100, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 50}}
        final = {"prompt_tokens": 100, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 50}}
        state = None
        for value in (first, first, final, final, {}):
            state = merge_usage(state, value)
        self.assertEqual(normalize_usage(state)["total_tokens"], 120)
        self.assertEqual(normalize_usage(state)["cache_read_tokens"], 50)
        self.assertEqual(merge_usage(first, {"completion_tokens": 20}), {"completion_tokens": 20})

    def test_pricing_examples_and_write_charge(self):
        prices = config()["prices"]
        all_normal = openai_usage(198_000_000, 2_000_000, 0)
        cached = openai_usage(198_000_000, 2_000_000, 188_100_000)
        self.assertAlmostEqual(calculate_cost(all_normal, prices), 62.32)
        self.assertAlmostEqual(calculate_cost(cached, prices), 21.57754)
        written = normalize_usage({"input_tokens": 1_000_000, "cache_read_input_tokens": 0,
                                   "cache_creation_input_tokens": 2_000_000, "output_tokens": 0}, "independent")
        self.assertAlmostEqual(calculate_cost(written, {**prices, "cache_write": .5}), 1.304)
        self.assertIsNone(calculate_cost(written, {**prices, "cache_write": None}))


class TpmBudgetTests(unittest.TestCase):
    def test_parallel_reservation_never_exceeds_budget(self):
        ledger = BudgetLedger(1, config()["prices"])
        gate = threading.Barrier(20)
        def reserve(index):
            gate.wait()
            return ledger.reserve(str(index), .1)
        with ThreadPoolExecutor(20) as pool:
            accepted = list(pool.map(reserve, range(20)))
        self.assertEqual(sum(accepted), 10)
        self.assertEqual(ledger.snapshot()["reserved_cost_usd"], 1)
        self.assertEqual(ledger.snapshot()["remaining_usd"], 0)

    def test_duplicate_request_neither_reserves_nor_settles_twice(self):
        ledger = BudgetLedger(1, config()["prices"])
        self.assertTrue(ledger.reserve("r1", .1))
        self.assertFalse(ledger.reserve("r1", .1))
        usage = openai_usage()
        ledger.settle("r1", usage)
        before = ledger.snapshot()
        ledger.settle("r1", usage)
        self.assertEqual(before, ledger.snapshot())
        self.assertEqual(before["reserved_cost_usd"], 0)
        self.assertAlmostEqual(before["known_cost_usd"], calculate_cost(usage, config()["prices"]))

    def test_unknown_failed_or_cancelled_cost_is_not_zero(self):
        ledger = BudgetLedger(1, config()["prices"])
        ledger.reserve("r1", .25)
        ledger.settle("r1", None)
        self.assertEqual(ledger.snapshot()["reserved_cost_usd"], .25)
        self.assertEqual(ledger.snapshot()["unknown_requests"], 1)
        self.assertFalse(ledger.release_unsent("r1"))
        self.assertFalse(ledger.reserve("r2", .76))

    def test_partial_known_cost_and_reserve_do_not_double_charge(self):
        ledger = BudgetLedger(1, config()["prices"])
        ledger.reserve("r1", .1)
        ledger.settle("r1", normalize_usage({"prompt_tokens": 49500, "completion_tokens": 500}))
        state = ledger.snapshot()
        self.assertGreater(state["known_cost_usd"], 0)
        self.assertAlmostEqual(state["known_cost_usd"] + state["reserved_cost_usd"], .1)
        self.assertEqual(state["unknown_requests"], 1)

    def test_only_unsent_can_release_reservation(self):
        ledger = BudgetLedger(1, config()["prices"])
        ledger.reserve("r1", .25)
        self.assertTrue(ledger.release_unsent("r1"))
        self.assertFalse(ledger.release_unsent("r1"))
        self.assertEqual(ledger.snapshot()["reserved_cost_usd"], 0)
        ledger.settle("r1", openai_usage())
        self.assertEqual(ledger.snapshot()["known_cost_usd"], 0)

    def test_worst_reserve_ignores_expected_cache_hit(self):
        cfg = config(prices={"cache_write": .8}, cache_write_price_mode="explicit")
        reserve = worst_case_cost(49500, 1024, cfg["prices"])
        self.assertAlmostEqual(reserve, (49500 * .8 + 1024 * 1.064) / 1e6)
        self.assertIsNone(worst_case_cost(49500, 1024, {**cfg["prices"], "cache_write": None}))


class TpmCorpusTests(unittest.TestCase):
    def test_reproducible_real_input_is_resized_using_observed_usage(self):
        first, second = Corpus(config()), Corpus(config())
        before = first.metadata()
        self.assertEqual(before["fingerprint"], second.metadata()["fingerprint"])
        self.assertIsNone(before["local_input_tokens"])
        self.assertFalse(before["input_verified"])
        self.assertTrue(first.resize_from_usage(2500))
        after = first.metadata()
        self.assertGreater(after["body_bytes"], before["body_bytes"] * 10)
        self.assertGreater(len(first.messages("full-input")[0]["content"]), 100_000)
        self.assertNotEqual(before["fingerprint"], after["fingerprint"])
        self.assertFalse(first.resize_from_usage(49200))
        self.assertTrue(first.metadata()["input_verified"])
        self.assertEqual(first.metadata()["source"], "upstream_usage_calibrated")

    def test_cache_prefix_stable_within_group_and_different_between_groups(self):
        corpus = Corpus(config(template_groups=2))
        a = corpus.messages("request-A", 0)[0]["content"]
        b = corpus.messages("request-B", 0)[0]["content"]
        c = corpus.messages("request-A", 1)[0]["content"]
        self.assertEqual(a[:-60], b[:-60])
        self.assertNotEqual(a[:100], c[:100])
        self.assertIn("request-A", a)
        self.assertIn("request-B", b)

    def test_avoid_cache_changes_early_prefix_without_generating_new_corpus(self):
        corpus = Corpus(config(cache_mode="avoid_cache"))
        fingerprint = corpus.fingerprint
        a = corpus.messages("A")[0]["content"]
        b = corpus.messages("B")[0]["content"]
        self.assertNotEqual(a[:90], b[:90])
        self.assertEqual(len(a), len(b))
        self.assertEqual(fingerprint, corpus.fingerprint)

    def test_abnormal_usage_cannot_amplify_corpus_beyond_memory_cap(self):
        corpus = Corpus(config(template_groups=128))
        with self.assertRaisesRegex(ValueError, "语料规模"):
            corpus.resize_from_usage(1)
        for invalid in (0, -1, True, float("nan"), float("inf")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                Corpus(config()).resize_from_usage(invalid)

    def test_corpus_never_retains_credentials_or_connection_binding(self):
        secret = "private-memory-key-not-for-corpus"
        corpus = Corpus(config(api_key=secret, connection_id="saved-secret-binding"))
        self.assertNotIn("api_key", corpus.config)
        self.assertNotIn("connection_id", corpus.config)
        self.assertNotIn(secret, json.dumps(corpus.metadata()))
        self.assertNotIn(secret, json.dumps(corpus.request_body("request")))
        self.assertNotIn("saved-secret-binding", json.dumps(corpus.config))

    def test_output_limit_and_usage_stream_explicitly_present(self):
        corpus = Corpus(config(output_limit_parameter="max_completion_tokens"))
        body = corpus.request_body("1")
        self.assertEqual(body["max_completion_tokens"], 1024)
        self.assertTrue(body["stream_options"]["include_usage"])
        self.assertNotIn("cache_control", body)
        self.assertNotIn("api_key", json.dumps(body))

    def test_injected_corresponding_tokenizer_is_used_before_usage_calibration(self):
        class LocalTokenizer:
            def encode(self, content):
                return content.split()
        corpus = Corpus(config(input_tokens=2000, target_tpm=1_000_000), tokenizer=LocalTokenizer())
        self.assertLess(abs(corpus.metadata()["local_input_tokens"] - 2000), 20)
        self.assertEqual(corpus.metadata()["source"], "local_tokenizer_content_estimate")
        self.assertFalse(corpus.metadata()["input_verified"])


if __name__ == "__main__":
    unittest.main()
