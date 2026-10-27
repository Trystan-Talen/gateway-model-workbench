"""Contract tests for model fingerprint evidence, using synthetic data only.

Run from the implementation directory:
    python3 -m unittest discover -s tests -p 'test_fingerprint_protocol.py' -v

No request is made to a model endpoint.  The .invalid hostname is descriptive
metadata only; none of these tests imports the collection service.
"""

from __future__ import annotations

import copy
import json
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone

import fingerprint_protocol as fp


NOW = datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc)
ENDPOINT = {"url": "https://fingerprint-tests.invalid/v1", "model": "synthetic-model"}
BASE_CONFIG = {
    "preset": "standard",
    "samples_per_cell": 25,
    "concurrency": 4,
    "timeout_seconds": 30,
    "adapter": "standard",
}


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def protocol(**changes):
    return fp.make_protocol({**BASE_CONFIG, **changes})


def answers_for(cell_id):
    task, language = cell_id.rsplit(":", 1)
    if task in {"random-number-1-100", "random-number-1-10", "favorite-number"}:
        return ("7", "8")
    if task == "random-letter":
        return ("a", "b")
    if task == "random-color":
        return ("blue", "red")
    if task == "coin-flip":
        return ("heads", "tails")
    if task == "random-animal":
        return ("cat", "dog") if language == "en" else ("猫", "狗")
    if task == "random-city":
        return ("paris", "rome") if language == "en" else ("巴黎", "罗马")
    raise AssertionError(f"Test fixture does not recognize cell {cell_id!r}")


def sample(cell_id, index, answer=None, **changes):
    answer = answer if answer is not None else answers_for(cell_id)[0]
    started = NOW - timedelta(hours=1) + timedelta(milliseconds=index * 150)
    data = {
        "index": index,
        "cell_id": cell_id,
        "paraphrase_index": 0,
        "started_at": iso(started),
        "finished_at": iso(started + timedelta(milliseconds=100)),
        "latency_ms": 100.0,
        "status_code": 200,
        "category": "valid",
        "normalized": answer,
        "raw": answer,
        "extra_text": False,
        "finish_reason": "stop",
        "truncated": False,
        "returned_model": ENDPOINT["model"],
        "response_id": f"synthetic-{index}",
        "usage": {
            "prompt_tokens": None,
            "completion_tokens": None,
            "reasoning_tokens": None,
            "cached_tokens": None,
            "reported_cost": None,
        },
        "error": None,
    }
    data.update(changes)
    return data


def samples_for(proto, *, counts=None, pattern=(0,), changes=None):
    """Generate every planned cell, retaining independent within-cell order."""
    rows = []
    for cell_id in proto["cells"]:
        n = proto["samples_per_cell"] if counts is None else counts.get(cell_id, 0)
        for repetition in range(n):
            answer = answers_for(cell_id)[pattern[repetition % len(pattern)]]
            rows.append(sample(cell_id, len(rows), answer, **(changes or {})))
    return rows


def artifact(proto=None, *, samples=None, collected_at=None, endpoint=None, complete=True):
    proto = proto if proto is not None else protocol()
    return fp.build_artifact(
        samples if samples is not None else samples_for(proto),
        proto,
        endpoint or ENDPOINT,
        collected_at=collected_at or iso(NOW - timedelta(hours=1)),
        seed=1729,
        complete=complete,
    )


def upstream_artifact(proto=None):
    proto = proto if proto is not None else protocol()
    cells = {}
    for cell_id in proto["cells"]:
        cells[cell_id] = {
            "cellId": cell_id,
            "counts": {answers_for(cell_id)[0]: 25},
            "validCount": 25,
            "invalidCount": 0,
            "refusalCount": 0,
            "emptyCount": 0,
            "errorCount": 0,
            "totalCount": 25,
            "entropyBits": 0,
            "normalizedEntropy": 0,
            "medianLatencyMs": None,
            "meanCompletionTokens": None,
            "meanReasoningTokens": None,
        }
    return {
        "formatVersion": 1,
        "protocol": "one-token/v1",
        "model": "synthetic-model",
        "collectedAt": iso(NOW - timedelta(hours=1)),
        "samplesPerCell": 25,
        "postReasoning": False,
        "cells": cells,
        "meta": {"tool": "llm-fingerprint-detector", "channel": "synthetic"},
    }


class DivergenceTests(unittest.TestCase):
    def test_identical_distributions_have_zero_distance(self):
        self.assertAlmostEqual(fp.jensen_shannon_divergence({"a": 7, "b": 3}, {"a": 14, "b": 6}), 0)

    def test_disjoint_supports_have_one_bit_distance(self):
        self.assertAlmostEqual(fp.jensen_shannon_divergence({"a": 25}, {"b": 11}), 1)

    def test_divergence_is_symmetric_and_bounded(self):
        p, q = {"a": 10, "b": 2, "c": 3}, {"b": 8, "d": 4}
        forward = fp.jensen_shannon_divergence(p, q)
        self.assertAlmostEqual(forward, fp.jensen_shannon_divergence(q, p))
        self.assertGreater(forward, 0)
        self.assertLess(forward, 1)


class NormalizationTests(unittest.TestCase):
    def test_numeric_scripts_and_number_words_are_canonical(self):
        cases = {"seven": "7", "Seven": "7", "七": "7", "十七": "17", "٤٢": "42", "４２": "42", ' "42" ': "42"}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                result = fp.normalize_answer(raw, "random-number-1-100:en")
                self.assertEqual(result["category"], "valid")
                self.assertEqual(result["normalized"], expected)

    def test_numeric_domain_is_enforced(self):
        for raw in ("0", "101", "1000"):
            with self.subTest(raw=raw):
                self.assertEqual(fp.normalize_answer(raw, "random-number-1-100:en")["category"], "invalid")

    def test_signed_numbers_and_decimals_cannot_be_silently_rewritten(self):
        for raw in ("-7", "−7", "3.14", "-3.14", "负七", "三点一四", "7/8", "7e1"):
            with self.subTest(raw=raw):
                result = fp.normalize_answer(raw, "random-number-1-100:en")
                self.assertEqual(result["category"], "invalid")
                self.assertIsNone(result["normalized"])

    def test_refusals_remain_separate_from_invalid_answers(self):
        for raw in ("I cannot comply with that request.", "Sorry, I can't help with that.", "抱歉，我无法回答这个问题。"):
            with self.subTest(raw=raw):
                result = fp.normalize_answer(raw, "random-animal:en")
                self.assertEqual(result["category"], "refusal")
                self.assertIsNone(result["normalized"])

    def test_explanations_and_multiword_answers_do_not_become_first_word_samples(self):
        for raw, cell_id in (("42 because it is random", "random-number-1-100:en"), ("New York", "random-city:en"), ("我选择猫，因为它很可爱", "random-animal:zh")):
            with self.subTest(raw=raw):
                result = fp.normalize_answer(raw, cell_id)
                self.assertEqual(result["category"], "invalid")
                self.assertIsNone(result["normalized"])
                self.assertTrue(result["extra_text"])

    def test_explicit_chinese_explanations_without_spaces_are_rejected(self):
        for raw in ("猫因为可爱", "所以我选择猫", "巴黎因此成为我的答案", "答案为东京", "我的理由是随机", "我认为是猫"):
            with self.subTest(raw=raw):
                result = fp.normalize_answer(raw, "random-animal:zh")
                self.assertEqual(result["category"], "invalid")
                self.assertTrue(result["extra_text"])

    def test_chinese_open_word_checks_do_not_require_a_fixed_dictionary(self):
        for raw, cell_id in (("猞猁", "random-animal:zh"), ("乌兰巴托", "random-city:zh")):
            with self.subTest(raw=raw):
                self.assertEqual(fp.normalize_answer(raw, cell_id)["category"], "valid")

    def test_empty_values_do_not_become_zero_or_valid_words(self):
        for raw in ("", "   ", "\n\t"):
            with self.subTest(raw=raw):
                result = fp.normalize_answer(raw, "random-number-1-100:en")
                self.assertEqual(result["category"], "empty")
                self.assertIsNone(result["normalized"])

    def test_coin_and_color_aliases_are_canonicalized(self):
        pairs = (("HEADS", "正面", "coin-flip:zh"), ("grey", "gray", "random-color:en"), ("蓝", "蓝色", "random-color:zh"))
        for left, right, cell_id in pairs:
            with self.subTest(cell_id=cell_id):
                a, b = fp.normalize_answer(left, cell_id), fp.normalize_answer(right, cell_id)
                self.assertEqual(a["category"], "valid")
                self.assertEqual(b["category"], "valid")
                self.assertEqual(a["normalized"], b["normalized"])


class FrozenProtocolTests(unittest.TestCase):
    def test_reasoning_adapters_freeze_a_separate_bounded_protocol(self):
        direct = protocol()
        for adapter, token_field in (("openai_reasoning", "max_completion_tokens"), ("deepseek_reasoning", "max_tokens"), ("zhipu_reasoning", "max_tokens")):
            with self.subTest(adapter=adapter):
                value = protocol(adapter=adapter)
                self.assertEqual(value["version"], fp.REASONING_METHOD_VERSION)
                self.assertEqual(value["max_tokens"], 4096)
                self.assertIsNone(value["temperature"])
                self.assertIsNone(value["top_p"])
                self.assertEqual(value["token_field"], token_field)
                self.assertEqual(value["extra_body"]["reasoning_effort"], "low")
                self.assertEqual(value["max_retries"], 0)
                self.assertTrue(fp._protocol_valid(value))
                self.assertNotEqual(value["id"], direct["id"])
                imported = fp.validate_import(artifact(value))
                self.assertEqual(imported["protocol"], value)
                self.assertEqual(imported["source"]["method_version"], fp.REASONING_METHOD_VERSION)
                mutated = artifact(value)
                mutated["protocol"]["max_tokens"] = 16384
                mutated["protocol"]["id"] = fp._digest({k: v for k, v in mutated["protocol"].items() if k != "id"})
                with self.assertRaisesRegex(ValueError, "协议摘要"):
                    fp.validate_import(mutated)
        self.assertEqual(direct["max_tokens"], 16)
        self.assertEqual(direct["temperature"], 1)
        self.assertEqual(direct["version"], fp.METHOD_VERSION)

    def test_reasoning_comparison_remains_exploratory_even_when_usage_is_missing(self):
        value = artifact(protocol(adapter="openai_reasoning"))
        compared = fp.compare_artifacts(value, copy.deepcopy(value), now=NOW)
        self.assertTrue(compared["protocol_compatible"])
        self.assertEqual(compared["mean_jsd"], 0)
        self.assertEqual(compared["verdict"], "incomparable")
        self.assertTrue(any("阈值尚未校准" in reason for reason in compared["reasons"]))
        mixed = fp.compare_artifacts(value, artifact(), now=NOW)
        self.assertFalse(mixed["protocol_compatible"])
        self.assertEqual(mixed["verdict"], "incomparable")

    def test_catalog_discloses_each_adapter_budget_without_mutating_manifest(self):
        before = protocol()
        adapters = {entry["id"]: entry for entry in fp.catalog()["adapters"]}
        self.assertEqual(adapters["standard"]["max_tokens"], 16)
        self.assertEqual(adapters["openai_reasoning"]["max_tokens"], 4096)
        self.assertEqual(adapters["zhipu_reasoning"]["max_tokens"], 4096)
        self.assertEqual(adapters["zhipu_reasoning"]["extra_body"], {"thinking": {"type": "enabled"}, "reasoning_effort": "low"})
        adapters["openai_reasoning"]["extra_body"]["reasoning_effort"] = "none"
        self.assertEqual(protocol(adapter="openai_reasoning")["extra_body"]["reasoning_effort"], "low")
        self.assertEqual(protocol(), before)

    def test_new_collection_defaults_include_frozen_rate(self):
        value = fp.make_protocol({})
        self.assertEqual((value["concurrency"], value["rpm"]), (20, 100))
        self.assertTrue(fp._protocol_valid(value))
        self.assertEqual(fp.catalog()["limits"]["default_rpm"], 100)
        for invalid in (0, -1, True, 600001, float("nan")):
            with self.subTest(rpm=invalid), self.assertRaises(ValueError):
                fp.make_protocol({"rpm": invalid})

    def test_legacy_manifest_preserves_hash_and_no_rate_semantics(self):
        legacy = protocol()
        legacy.pop("rpm")
        legacy["id"] = fp._digest({k: v for k, v in legacy.items() if k != "id"})
        self.assertTrue(fp._protocol_valid(legacy))
        self.assertNotEqual(legacy["id"], protocol()["id"])
        evidence = artifact(legacy)
        imported = fp.validate_import(evidence)
        self.assertEqual(imported["protocol"], legacy)
        self.assertNotIn("rpm", imported["protocol"])

    def test_semantic_configuration_changes_protocol_hash(self):
        base = protocol()
        variants = ({"samples_per_cell": 30}, {"concurrency": 2}, {"rpm": 200}, {"timeout_seconds": 60}, {"adapter": "openrouter"}, {"preset": "strict"})
        for change in variants:
            with self.subTest(change=change):
                self.assertNotEqual(base["id"], protocol(**change)["id"])

    def test_endpoint_credentials_names_and_schedule_seed_do_not_change_protocol(self):
        base = protocol()
        other = protocol(endpoint={"url": "https://other.invalid/v1", "model": "other", "api_key": "NEVER-PERSIST-THIS-SECRET"}, seed=999, name="different display name")
        self.assertEqual(base["id"], other["id"])
        self.assertNotIn("NEVER-PERSIST-THIS-SECRET", json.dumps(other))
        self.assertNotIn("other.invalid", json.dumps(other))

    def test_protocol_contains_locked_direct_answer_parameters(self):
        value = protocol()
        for key, expected in (("temperature", 1), ("top_p", 1), ("max_tokens", 16), ("max_retries", 0)):
            self.assertEqual(value[key], expected)
        for key in ("version", "system_prompts", "normalizer_version", "battery_hash", "token_field"):
            self.assertTrue(value[key])
        self.assertEqual(len(value["id"]), 64)
        self.assertIs(type(value["temperature"]), int)
        self.assertIs(type(value["top_p"]), int)

    def test_protocol_and_plan_are_deterministic(self):
        one, two = protocol(), protocol()
        self.assertEqual(one, two)
        self.assertEqual(fp.make_plan(one, 17), fp.make_plan(two, 17))
        self.assertNotEqual(fp.make_plan(one, 17), fp.make_plan(one, 18))

    def test_hash_normalizes_nested_integral_floats_without_collapsing_booleans(self):
        floats = {"nested": [1.0, {"limit": 16.0, "enabled": True}], "zero": -0.0}
        integers = {"nested": [1, {"limit": 16, "enabled": True}], "zero": 0}
        self.assertEqual(fp._digest(floats), fp._digest(integers))
        self.assertNotEqual(fp._digest({"temperature": True}), fp._digest({"temperature": 1}))

    def test_plan_contains_exact_budget_and_complete_single_turn_inputs(self):
        value = protocol()
        plan = fp.make_plan(value, 42)
        self.assertEqual(len(plan), len(value["cells"]) * value["samples_per_cell"])
        self.assertEqual(Counter(item["cell_id"] for item in plan), Counter({cell: value["samples_per_cell"] for cell in value["cells"]}))
        self.assertEqual(sorted(item["index"] for item in plan), list(range(len(plan))))
        first_cell = plan[0]["cell_id"]
        self.assertTrue(any(item["cell_id"] != first_cell for item in plan[:value["samples_per_cell"]]))
        for item in plan:
            self.assertIsInstance(item["paraphrase_index"], int)
            self.assertGreaterEqual(item["paraphrase_index"], 0)
            self.assertIsInstance(item["system_prompt"], str)
            self.assertIsInstance(item["user_prompt"], str)
            self.assertTrue(item["system_prompt"])
            self.assertTrue(item["user_prompt"])
            self.assertFalse({"history", "memory", "tools", "assistant_prompt"}.intersection(item))
            if "messages" in item:
                self.assertEqual([message["role"] for message in item["messages"]], ["system", "user"])


class ArtifactAndSelfCheckTests(unittest.TestCase):
    def test_build_does_not_mutate_inputs_or_save_credentials(self):
        value = protocol()
        rows = samples_for(value)
        before_protocol, before_rows = copy.deepcopy(value), copy.deepcopy(rows)
        result = artifact(value, samples=rows, endpoint={**ENDPOINT, "api_key": "NEVER-PERSIST-THIS-SECRET"})
        self.assertEqual(value, before_protocol)
        self.assertEqual(rows, before_rows)
        self.assertNotIn("NEVER-PERSIST-THIS-SECRET", json.dumps(result))
        self.assertEqual(result["format"], "workbench-fingerprint")

    def test_only_valid_samples_contribute_to_distribution(self):
        value = protocol()
        rows = samples_for(value)
        cell = value["cells"][0]
        additions = (("invalid", {"extra_text": True}), ("refusal", {}), ("empty", {}), ("error", {"status_code": 500, "error": "synthetic transport failure"}))
        for category, changes in additions:
            rows.append(sample(cell, len(rows), "DO-NOT-COUNT", category=category, normalized=None, **changes))
        result = artifact(value, samples=rows)
        entry = result["cells"][cell]
        self.assertEqual(entry["valid_count"], 25)
        self.assertEqual(entry["total_count"], 29)
        self.assertEqual(sum(entry["counts"].values()), 25)
        self.assertNotIn("DO-NOT-COUNT", entry["counts"])
        for category in ("invalid", "refusal", "empty", "error"):
            self.assertEqual(entry[f"{category}_count"], 1)

    def test_unknown_usage_and_cache_are_not_fabricated_as_zero(self):
        result = artifact()
        for key in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "cached_tokens", "reported_cost"):
            self.assertIsNone(result["diagnostics"]["usage"][key])
        cache = result["diagnostics"]["cache"]
        self.assertEqual(cache["unknown_samples"], 200)
        self.assertIsNone(cache["cached_median_latency_ms"])
        self.assertIsNone(cache["uncached_median_latency_ms"])

    def test_cache_latencies_are_grouped_using_reported_cache_tokens(self):
        value = protocol()
        rows = samples_for(value)
        for position, row in enumerate(rows):
            row["latency_ms"] = 20 if position % 2 == 0 else 200
            row["usage"]["cached_tokens"] = 10 if position % 2 == 0 else 0
        cache = artifact(value, samples=rows)["diagnostics"]["cache"]
        self.assertEqual(cache["cached_samples"], 100)
        self.assertEqual(cache["uncached_samples"], 100)
        self.assertEqual(cache["unknown_samples"], 0)
        self.assertEqual(cache["cached_median_latency_ms"], 20)
        self.assertEqual(cache["uncached_median_latency_ms"], 200)

    def test_within_cell_split_does_not_use_global_index_parity(self):
        value = protocol(samples_per_cell=10)
        rows = []
        for cell_number, cell_id in enumerate(value["cells"]):
            for repetition in range(10):
                rows.append(sample(cell_id, cell_number * 100 + repetition * 2))
        result = artifact(value, samples=list(reversed(rows)))
        for method in ("alternating", "temporal"):
            split = result["diagnostics"]["split_half"][method]
            self.assertEqual(split["comparable_cells"], len(value["cells"]))
            self.assertAlmostEqual(split["mean_jsd"], 0)

    def test_fewer_than_five_valid_samples_per_half_is_unavailable(self):
        value = protocol(samples_per_cell=10)
        counts = {cell: 9 for cell in value["cells"]}
        result = artifact(value, samples=samples_for(value, counts=counts))
        for method in ("alternating", "temporal"):
            split = result["diagnostics"]["split_half"][method]
            self.assertEqual(split["comparable_cells"], 0)
            self.assertIsNone(split["mean_jsd"])

    def test_temporal_split_detects_change_hidden_by_interleaved_split(self):
        value = protocol(samples_per_cell=40)
        rows = samples_for(value, pattern=(0,) * 20 + (1,) * 20)
        split = artifact(value, samples=rows)["diagnostics"]["split_half"]
        self.assertAlmostEqual(split["alternating"]["mean_jsd"], 0)
        self.assertAlmostEqual(split["temporal"]["mean_jsd"], 1)
        self.assertTrue(split["warning"])

    def test_stable_mixture_can_pass_both_splits_so_pass_is_not_identity_proof(self):
        value = protocol(samples_per_cell=40)
        rows = samples_for(value, pattern=(0, 1, 1, 0))
        split = artifact(value, samples=rows)["diagnostics"]["split_half"]
        self.assertAlmostEqual(split["alternating"]["mean_jsd"], 0)
        self.assertAlmostEqual(split["temporal"]["mean_jsd"], 0)
        self.assertFalse(split["warning"])

    def test_reported_output_above_fixed_budget_is_recorded_without_inferring_reasoning(self):
        value = protocol()
        rows = samples_for(value)
        for row in rows:
            row["usage"]["completion_tokens"] = 16
        rows[0]["usage"]["completion_tokens"] = 17
        result = artifact(value, samples=rows)
        self.assertEqual(result["diagnostics"]["token_budget_exceeded_count"], 1)
        self.assertFalse(result["diagnostics"]["reasoning_observed"])
        self.assertTrue(any("参数回报不一致" in warning for warning in result["diagnostics"]["warnings"]))
        compared = fp.compare_artifacts(result, artifact(value), now=NOW)
        self.assertEqual(compared["verdict"], "uncertain")

    def test_output_at_the_fixed_budget_is_not_an_overrun(self):
        value = protocol()
        rows = samples_for(value)
        for row in rows:
            row["usage"]["completion_tokens"] = 16
        result = artifact(value, samples=rows)
        self.assertEqual(result["diagnostics"]["token_budget_exceeded_count"], 0)
        self.assertEqual(fp.compare_artifacts(result, artifact(value), now=NOW)["verdict"], "match")

    def test_partial_usage_has_coverage_and_is_not_presented_as_complete_cost(self):
        value = protocol()
        rows = samples_for(value)
        for row in rows[:100]:
            row["usage"]["prompt_tokens"] = 10
        rows[0]["usage"]["reported_cost"] = 0.005
        result = artifact(value, samples=rows)
        diagnostic = result["diagnostics"]
        self.assertEqual(diagnostic["usage"]["prompt_tokens"], 1000)
        self.assertEqual(diagnostic["usage_observed_samples"]["prompt_tokens"], 100)
        self.assertEqual(diagnostic["usage_observed_samples"]["reported_cost"], 1)
        self.assertEqual(diagnostic["usage_observed_samples"]["completion_tokens"], 0)
        self.assertIsNone(diagnostic["usage"]["completion_tokens"])
        self.assertTrue(any("1/200" in warning and "完整费用" in warning for warning in diagnostic["warnings"]))
        # Missing cost metadata affects accounting, not the distribution test.
        self.assertEqual(fp.compare_artifacts(result, artifact(value), now=NOW)["verdict"], "match")


class ComparisonGateTests(unittest.TestCase):
    def setUp(self):
        self.protocol = protocol()
        self.reference = artifact(self.protocol)

    def compare(self, candidate=None, reference=None):
        return fp.compare_artifacts(candidate or artifact(self.protocol), reference or self.reference, now=NOW)

    def assert_degraded(self, result):
        self.assertNotIn(result["verdict"], ("match", "mismatch"))
        self.assertTrue(result["reasons"] or result["warnings"])

    def test_compatible_identical_samples_match_without_claiming_probability(self):
        result = self.compare()
        self.assertEqual(result["verdict"], "match")
        self.assertAlmostEqual(result["mean_jsd"], 0)
        self.assertTrue(result["protocol_compatible"])
        self.assertEqual(result["comparable_cells"], len(self.protocol["cells"]))
        self.assertNotIn("confidence", result)
        self.assertNotIn("probability_same_model", result)

    def test_compatible_disjoint_distributions_show_behavioral_difference(self):
        result = self.compare(artifact(self.protocol, samples=samples_for(self.protocol, pattern=(1,))))
        self.assertEqual(result["verdict"], "mismatch")
        self.assertAlmostEqual(result["mean_jsd"], 1)

    def test_protocol_mismatch_downgrades_even_with_identical_counts(self):
        result = self.compare(artifact(protocol(concurrency=2)))
        self.assertEqual(result["verdict"], "incomparable")
        self.assertFalse(result["protocol_compatible"])
        self.assert_degraded(result)

    def test_stale_reference_cannot_yield_match_or_mismatch(self):
        old = artifact(self.protocol, collected_at=iso(NOW - timedelta(days=31)))
        result = self.compare(reference=old)
        self.assertGreater(result["reference_age_days"], 30)
        self.assert_degraded(result)

    def test_future_reference_cannot_yield_match_or_mismatch(self):
        future = artifact(self.protocol, collected_at=iso(NOW + timedelta(days=1)))
        self.assert_degraded(self.compare(reference=future))

    def test_future_candidate_cannot_yield_match_or_mismatch(self):
        future = artifact(self.protocol, collected_at=iso(NOW + timedelta(days=1)))
        self.assert_degraded(self.compare(candidate=future))

    def test_less_than_ten_valid_samples_in_a_cell_excludes_that_cell(self):
        counts = {cell: 25 for cell in self.protocol["cells"]}
        counts[self.protocol["cells"][0]] = 9
        result = self.compare(artifact(self.protocol, samples=samples_for(self.protocol, counts=counts)))
        self.assertEqual(result["comparable_cells"], len(self.protocol["cells"]) - 1)
        self.assertNotIn(self.protocol["cells"][0], [cell["cell_id"] for cell in result["cells"]])

    def test_less_than_eighty_percent_coverage_is_insufficient(self):
        counts = {cell: 25 for cell in self.protocol["cells"][:6]}
        result = self.compare(artifact(self.protocol, samples=samples_for(self.protocol, counts=counts)))
        self.assertEqual(result["comparable_cells"], 6)
        self.assertEqual(result["verdict"], "insufficient")

    def test_less_than_four_comparable_cells_is_insufficient(self):
        counts = {cell: 25 for cell in self.protocol["cells"][:3]}
        result = self.compare(artifact(self.protocol, samples=samples_for(self.protocol, counts=counts)))
        self.assertEqual(result["verdict"], "insufficient")

    def test_reasoning_evidence_downgrades_identical_distributions(self):
        rows = samples_for(self.protocol)
        rows[0]["usage"]["reasoning_tokens"] = 24
        candidate = artifact(self.protocol, samples=rows)
        self.assertTrue(candidate["diagnostics"]["reasoning_observed"])
        self.assert_degraded(self.compare(candidate))

    def test_reference_reasoning_also_downgrades(self):
        rows = samples_for(self.protocol)
        rows[0]["usage"]["reasoning_tokens"] = 24
        self.assert_degraded(self.compare(reference=artifact(self.protocol, samples=rows)))

    def test_high_transport_error_rate_downgrades_even_when_each_cell_has_ten_valid(self):
        rows = samples_for(self.protocol)
        for position, row in enumerate(rows):
            if position % 25 >= 12:
                row.update(category="error", normalized=None, raw="", status_code=500, error="synthetic error")
        candidate = artifact(self.protocol, samples=rows)
        self.assertGreater(candidate["diagnostics"]["error_rate"], 0.5)
        self.assert_degraded(self.compare(candidate))

    def test_truncation_downgrades_even_when_remaining_cells_are_comparable(self):
        rows = samples_for(self.protocol)
        for position, row in enumerate(rows):
            if position % 25 >= 12:
                row.update(category="invalid", normalized=None, truncated=True, finish_reason="length")
        self.assert_degraded(self.compare(artifact(self.protocol, samples=rows)))

    def test_incomplete_collection_cannot_yield_final_behavioral_verdict(self):
        self.assert_degraded(self.compare(artifact(self.protocol, complete=False)))

    def test_high_split_half_difference_downgrades(self):
        value = protocol(samples_per_cell=40)
        candidate = artifact(value, samples=samples_for(value, pattern=(0,) * 20 + (1,) * 20))
        reference = artifact(value, samples=samples_for(value, pattern=(0, 1, 1, 0)))
        self.assert_degraded(self.compare(candidate=candidate, reference=reference))

    def test_returned_model_alias_is_a_diagnostic_and_does_not_override_distribution(self):
        rows = samples_for(self.protocol, changes={"returned_model": "synthetic-dated-alias"})
        candidate = artifact(self.protocol, samples=rows)
        self.assertEqual(candidate["diagnostics"]["model_echo_mismatch_count"], 200)
        self.assertEqual(self.compare(candidate)["mean_jsd"], 0)

    def test_direct_comparison_derives_error_rates_from_cells_not_summary(self):
        candidate = artifact(self.protocol)
        for cell in candidate["cells"].values():
            answer = next(iter(cell["counts"]))
            cell["counts"][answer] = 12
            cell["valid_count"] = 12
            cell["error_count"] = 13
        self.assert_degraded(self.compare(candidate))

    def test_positive_reasoning_usage_cannot_be_hidden_by_false_flag(self):
        candidate = artifact(self.protocol)
        candidate["diagnostics"]["usage"]["reasoning_tokens"] = 20
        candidate["diagnostics"]["usage_observed_samples"]["reasoning_tokens"] = 1
        candidate["diagnostics"]["reasoning_observed"] = False
        self.assert_degraded(self.compare(candidate))

    def test_missing_self_check_evidence_downgrades_instead_of_implying_zero_distance(self):
        candidate = artifact(self.protocol)
        candidate["diagnostics"].pop("split_half")
        self.assert_degraded(self.compare(candidate))

    def test_malformed_direct_comparison_is_a_validation_error(self):
        for value in (None, [], False, {"cells": []}, {"cells": {"random-animal:en": None}}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    fp.compare_artifacts(value, self.reference, now=NOW)


class ImportValidationTests(unittest.TestCase):
    def test_local_artifact_round_trips_without_mutating_input(self):
        data = artifact()
        before = copy.deepcopy(data)
        imported = fp.validate_import(data)
        self.assertEqual(data, before)
        self.assertEqual(imported["protocol"]["id"], data["protocol"]["id"])
        self.assertEqual(imported["cells"], data["cells"])

    def test_browser_json_round_trip_preserves_protocol_identity(self):
        data = artifact()
        # Python can serialize 1.0 while a browser emits 1 for the same value.
        # Exercise both representations through the actual import validator.
        data["protocol"]["temperature"] = 1.0
        data["protocol"]["top_p"] = 1.0
        data["protocol"]["samples_per_cell"] = 25.0
        before = fp.validate_import(data)

        def browser_numbers(value):
            if isinstance(value, float) and value.is_integer():
                return int(value)
            if isinstance(value, dict):
                return {key: browser_numbers(item) for key, item in value.items()}
            if isinstance(value, list):
                return [browser_numbers(item) for item in value]
            return value

        browser_data = json.loads(json.dumps(browser_numbers(data)))
        after = fp.validate_import(browser_data)
        self.assertEqual(before["protocol"]["id"], after["protocol"]["id"])
        self.assertEqual(fp.compare_artifacts(before, after, now=NOW)["verdict"], "match")

    def test_browser_round_trip_does_not_authorize_changed_protocol_values(self):
        for key, changed in (("temperature", 0.5), ("top_p", 0.5), ("concurrency", 2), ("temperature", True)):
            with self.subTest(key=key, changed=changed):
                data = json.loads(json.dumps(artifact()))
                data["protocol"][key] = changed
                with self.assertRaises(ValueError):
                    fp.validate_import(data)

    def test_upstream_artifact_keeps_original_date_and_is_external(self):
        data = upstream_artifact()
        imported = fp.validate_import(data)
        self.assertEqual(imported["format"], "workbench-fingerprint")
        self.assertEqual(imported["collected_at"], data["collectedAt"])
        self.assertIn("one-token/v1", json.dumps(imported["protocol"]))
        self.assertIn("external_protocol", json.dumps(imported))
        result = fp.compare_artifacts(artifact(), imported, now=NOW)
        self.assertFalse(result["protocol_compatible"])
        self.assertEqual(result["verdict"], "incomparable")

    def test_count_sum_must_equal_valid_count(self):
        data = artifact()
        cell = next(iter(data["cells"].values()))
        cell["valid_count"] += 1
        with self.assertRaises(ValueError):
            fp.validate_import(data)

    def test_total_count_must_equal_category_counts(self):
        data = artifact()
        next(iter(data["cells"].values()))["total_count"] += 1
        with self.assertRaises(ValueError):
            fp.validate_import(data)

    def test_count_values_reject_negative_fractional_boolean_and_nonfinite_numbers(self):
        for invalid in (-1, 0.5, True, float("nan"), float("inf"), float("-inf")):
            with self.subTest(invalid=invalid):
                data = artifact()
                cell = next(iter(data["cells"].values()))
                token = next(iter(cell["counts"]))
                cell["counts"][token] = invalid
                with self.assertRaises(ValueError):
                    fp.validate_import(data)

    def test_nonfinite_diagnostic_values_are_rejected(self):
        for invalid in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(invalid=invalid):
                data = artifact()
                data["diagnostics"]["error_rate"] = invalid
                with self.assertRaises(ValueError):
                    fp.validate_import(data)

    def test_imported_summary_cannot_hide_error_counts_and_bypass_comparison_gate(self):
        data = artifact()
        for cell in data["cells"].values():
            token = next(iter(cell["counts"]))
            cell["counts"][token] = 12
            cell["valid_count"] = 12
            cell["error_count"] = 13
        # The original summary still claims a perfect 200/200 collection.
        # Import may reject that contradiction or recompute it from counts.
        try:
            imported = fp.validate_import(data)
        except ValueError:
            return
        self.assertAlmostEqual(imported["diagnostics"]["error_rate"], 13 / 25)
        self.assertAlmostEqual(imported["diagnostics"]["valid_rate"], 12 / 25)
        compared = fp.compare_artifacts(artifact(), imported, now=NOW)
        self.assertNotIn(compared["verdict"], ("match", "mismatch"))

    def test_upstream_inconsistent_and_negative_counts_are_rejected(self):
        for invalid in (-1, float("nan"), float("inf"), 26):
            with self.subTest(invalid=invalid):
                data = upstream_artifact()
                cell = next(iter(data["cells"].values()))
                token = next(iter(cell["counts"]))
                cell["counts"][token] = invalid
                with self.assertRaises(ValueError):
                    fp.validate_import(data)

    def test_protocol_hash_cannot_be_kept_after_editing_locked_parameters(self):
        data = artifact()
        data["protocol"]["temperature"] = 0.5
        with self.assertRaises(ValueError):
            fp.validate_import(data)

    def test_invalid_or_missing_collection_dates_are_not_replaced_with_today(self):
        for invalid in (None, "", "not-a-date"):
            with self.subTest(invalid=invalid):
                data = artifact()
                data["collected_at"] = invalid
                with self.assertRaises(ValueError):
                    fp.validate_import(data)

    def test_arbitrary_objects_are_not_accepted_as_reference_artifacts(self):
        for invalid in (None, [], "{}", {}, {"cells": {}}):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    fp.validate_import(invalid)

    def test_diagnostic_counts_require_nonnegative_integers_not_null_or_bool(self):
        paths = (
            ("model_echo_mismatch_count",),
            ("duplicate_response_id_count",),
            ("token_budget_exceeded_count",),
            ("returned_models", "synthetic-model"),
            ("split_half", "alternating", "comparable_cells"),
            ("cache", "observed_samples"),
            ("cache", "unknown_samples"),
        )
        for path in paths:
            for invalid in (None, True, False, "8", 0.5, -1):
                with self.subTest(path=path, invalid=invalid):
                    data = artifact()
                    target = data["diagnostics"]
                    for key in path[:-1]:
                        target = target[key]
                    target[path[-1]] = invalid
                    with self.assertRaises(ValueError):
                        fp.validate_import(data)

    def test_diagnostic_flags_require_booleans(self):
        for invalid in (None, 0, 1, "false", []):
            with self.subTest(invalid=invalid):
                for path in (("reasoning_observed",), ("split_half", "warning")):
                    data = artifact()
                    target = data["diagnostics"]
                    for key in path[:-1]:
                        target = target[key]
                    target[path[-1]] = invalid
                    with self.assertRaises(ValueError):
                        fp.validate_import(data)

    def test_self_check_cannot_claim_coverage_without_a_distance(self):
        for mean, coverage in ((None, 8), (0, 0)):
            with self.subTest(mean=mean, coverage=coverage):
                data = artifact()
                data["diagnostics"]["split_half"]["alternating"] = {"mean_jsd": mean, "comparable_cells": coverage}
                with self.assertRaises(ValueError):
                    fp.validate_import(data)

    def test_import_recomputes_self_check_warning_from_distances(self):
        data = artifact()
        data["diagnostics"]["split_half"]["alternating"]["mean_jsd"] = 0.5
        data["diagnostics"]["split_half"]["warning"] = False
        imported = fp.validate_import(data)
        self.assertTrue(imported["diagnostics"]["split_half"]["warning"])

    def test_positive_imported_reasoning_usage_sets_observed_flag(self):
        data = artifact()
        data["diagnostics"]["usage"]["reasoning_tokens"] = 20
        data["diagnostics"]["usage_observed_samples"]["reasoning_tokens"] = 1
        data["diagnostics"]["reasoning_observed"] = False
        self.assertTrue(fp.validate_import(data)["diagnostics"]["reasoning_observed"])

    def test_cache_and_echo_group_totals_cannot_exceed_sample_count(self):
        for field in ("cache", "returned_models"):
            with self.subTest(field=field):
                data = artifact()
                if field == "cache":
                    data["diagnostics"][field].update(observed_samples=100, cached_samples=100, unknown_samples=200)
                else:
                    data["diagnostics"][field] = {"first": 150, "second": 150}
                with self.assertRaises(ValueError):
                    fp.validate_import(data)

    def test_unobserved_cache_group_cannot_have_measured_latency(self):
        data = artifact()
        data["diagnostics"]["cache"]["cached_median_latency_ms"] = 0.01
        with self.assertRaises(ValueError):
            fp.validate_import(data)

    def test_tokens_are_optional_integer_counts_and_cost_can_be_fractional(self):
        data = artifact()
        data["diagnostics"]["usage"]["reported_cost"] = 0.001
        data["diagnostics"]["usage_observed_samples"]["reported_cost"] = 1
        self.assertEqual(fp.validate_import(data)["diagnostics"]["usage"]["reported_cost"], 0.001)
        for invalid in (True, "1", 0.5, -1):
            with self.subTest(invalid=invalid):
                data["diagnostics"]["usage"]["prompt_tokens"] = invalid
                with self.assertRaises(ValueError):
                    fp.validate_import(data)

    def test_nested_import_shape_errors_raise_value_error(self):
        for field in ("diagnostics", "endpoint", "source", "protocol", "cells"):
            for invalid in (None, False, [], "invalid"):
                with self.subTest(field=field, invalid=invalid):
                    data = artifact()
                    data[field] = invalid
                    with self.assertRaises(ValueError):
                        fp.validate_import(data)
        for field in ("cache", "usage", "split_half", "returned_models"):
            for invalid in (None, False, [], "invalid"):
                with self.subTest(field=field, invalid=invalid):
                    data = artifact()
                    data["diagnostics"][field] = invalid
                    with self.assertRaises(ValueError):
                        fp.validate_import(data)

    def test_upstream_shape_errors_and_boolean_format_versions_are_rejected(self):
        mutations = (("cells", ["broken"]), ("cells", {"random-animal:en": None}), ("meta", []), ("postReasoning", "false"), ("formatVersion", True))
        for field, invalid in mutations:
            with self.subTest(field=field):
                data = upstream_artifact()
                data[field] = invalid
                with self.assertRaises(ValueError):
                    fp.validate_import(data)
        data = artifact()
        data["format_version"] = True
        with self.assertRaises(ValueError):
            fp.validate_import(data)

    def test_text_metadata_cannot_serialize_nested_credentials(self):
        for container, key in (("endpoint", "model"), ("source", "source_note"), ("protocol", "version")):
            with self.subTest(container=container, key=key):
                data = artifact()
                data[container][key] = {"api_key": "must-not-persist"}
                with self.assertRaises(ValueError):
                    fp.validate_import(data)

    def test_nonfinite_or_out_of_range_dates_raise_value_error(self):
        for invalid in (float("inf"), float("nan"), 10**100):
            with self.subTest(invalid=invalid):
                data = artifact()
                data["collected_at"] = invalid
                with self.assertRaises(ValueError):
                    fp.validate_import(data)

    def test_invalid_unicode_does_not_escape_as_an_encoding_error(self):
        data = artifact()
        data["endpoint"]["model"] = "\\ud800".encode("ascii").decode("unicode_escape")
        with self.assertRaises(ValueError):
            fp.validate_import(data)

    def test_import_preserves_token_overrun_and_comparison_downgrades(self):
        value = protocol()
        rows = samples_for(value)
        rows[0]["usage"]["completion_tokens"] = 32
        imported = fp.validate_import(artifact(value, samples=rows))
        self.assertEqual(imported["diagnostics"]["token_budget_exceeded_count"], 1)
        self.assertFalse(imported["diagnostics"]["reasoning_observed"])
        self.assertEqual(fp.compare_artifacts(artifact(value), imported, now=NOW)["verdict"], "uncertain")

    def test_legacy_usage_without_coverage_keeps_completeness_unknown(self):
        value = protocol()
        rows = samples_for(value)
        rows[0]["usage"]["reported_cost"] = 0.005
        data = artifact(value, samples=rows)
        data["diagnostics"].pop("usage_observed_samples")
        imported = fp.validate_import(data)
        self.assertIsNone(imported["diagnostics"]["usage_observed_samples"]["reported_cost"])
        self.assertEqual(imported["diagnostics"]["usage"]["reported_cost"], 0.005)
        self.assertTrue(any("无法核实" in warning and "完整" in warning for warning in imported["diagnostics"]["warnings"]))

    def test_usage_coverage_rejects_invalid_counts_and_contradictory_totals(self):
        for invalid in (True, "1", 0.5, -1, 201):
            with self.subTest(invalid=invalid):
                data = artifact()
                data["diagnostics"]["usage_observed_samples"]["reported_cost"] = invalid
                with self.assertRaises(ValueError):
                    fp.validate_import(data)
        for amount, coverage in ((0.01, 0), (None, 1)):
            with self.subTest(amount=amount, coverage=coverage):
                data = artifact()
                data["diagnostics"]["usage"]["reported_cost"] = amount
                data["diagnostics"]["usage_observed_samples"]["reported_cost"] = coverage
                with self.assertRaises(ValueError):
                    fp.validate_import(data)

    def test_overrun_count_cannot_exceed_observed_output_usage_count(self):
        data = artifact()
        data["diagnostics"]["token_budget_exceeded_count"] = 1
        with self.assertRaises(ValueError):
            fp.validate_import(data)


if __name__ == "__main__":
    unittest.main()
