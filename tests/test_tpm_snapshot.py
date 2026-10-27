"""快照快速复制与标准库语义对照；不读取上游或正式历史。"""
from collections import defaultdict
import copy
from datetime import datetime, timezone
from decimal import Decimal
import json
import unittest

from tpm_snapshot import clone_snapshot


class CustomEvidence:
    def __init__(self, payload):
        self.payload = payload

    def __deepcopy__(self, memo):
        result = type(self)(None)
        memo[id(self)] = result
        result.payload = copy.deepcopy(self.payload, memo)
        return result


class SnapshotCloneTests(unittest.TestCase):
    def test_json_types_nested_content_and_encoded_bytes_equal_deepcopy(self):
        source = {"id": "合成任务", "metrics": {"full_token_tpm": 200000000, "hit": .95,
                   "unknown": None, "window_full": True, "zero": 0},
                  "series": [{"at": 1000.001, "errors": {}}, {"at": 1001.001}],
                  "recent_requests": [{"usage": {"raw": {"total_tokens": 50000}},
                                       "timing": {"started_at": "2026-10-16T10:00:00.001+08:00"}}]}
        original = copy.deepcopy(source)
        result = clone_snapshot(source)
        self.assertEqual(result, original)
        self.assertEqual(json.dumps(result, ensure_ascii=False), json.dumps(original, ensure_ascii=False))
        result["recent_requests"][0]["usage"]["raw"]["total_tokens"] = 1
        result["series"].append({"at": 2})
        self.assertEqual(source, original)

    def test_repeated_child_identity_is_preserved_but_detached(self):
        child = {"samples": [1, 2]}
        source = {"left": child, "right": child, "again": [child]}
        result = clone_snapshot(source)
        self.assertIs(result["left"], result["right"])
        self.assertIs(result["left"], result["again"][0])
        self.assertIsNot(result["left"], child)
        self.assertIsNot(result["left"]["samples"], child["samples"])
        result["left"]["samples"].append(3)
        self.assertEqual(child["samples"], [1, 2])

    def test_direct_and_cross_container_cycles_match_deepcopy(self):
        dictionary, sequence = {}, []
        dictionary.update(self=dictionary, sequence=sequence)
        sequence.extend((sequence, dictionary))
        result = clone_snapshot(dictionary)
        self.assertIs(result["self"], result)
        self.assertIs(result["sequence"][0], result["sequence"])
        self.assertIs(result["sequence"][1], result)
        self.assertIsNot(result, dictionary)
        self.assertIsNot(result["sequence"], sequence)

    def test_tuple_fallback_preserves_type_aliases_and_recursive_links(self):
        child = []
        wrapper = (child,)
        child.append(wrapper)
        source = [wrapper, child, (1, "immutable", None)]
        result = clone_snapshot(source)
        self.assertIsInstance(result[0], tuple)
        self.assertIs(result[0][0], result[1])
        self.assertIs(result[1][0], result[0])
        self.assertIs(result[2], source[2])
        self.assertIsNot(result[1], child)

    def test_unknown_types_and_container_subclasses_keep_standard_behavior(self):
        class MarkedList(list):
            pass

        sequence = MarkedList([{"v": [1]}])
        sequence.label = ["synthetic"]
        source = {"decimal": Decimal("0.0874"), "time": datetime(2026, 10, 16, tzinfo=timezone.utc),
                  "bytes": b"synthetic", "buffer": bytearray(b"synthetic"), "set": {1, 2},
                  "default": defaultdict(list, a=[1]), "subclass": sequence}
        result, expected = clone_snapshot(source), copy.deepcopy(source)
        self.assertEqual(result, expected)
        for key in source:
            self.assertIs(type(result[key]), type(expected[key]))
        result["buffer"][0] = 0
        result["set"].add(3)
        result["subclass"][0]["v"].append(2)
        result["subclass"].label.append("new")
        self.assertEqual(source["buffer"], bytearray(b"synthetic"))
        self.assertEqual(source["set"], {1, 2})
        self.assertEqual(sequence[0]["v"], [1])
        self.assertEqual(sequence.label, ["synthetic"])

    def test_custom_copier_and_fast_containers_share_one_memo(self):
        child = [1]
        custom = CustomEvidence(child)
        source = [child, custom, custom]
        result = clone_snapshot(source)
        self.assertIs(result[1], result[2])
        self.assertIs(result[1].payload, result[0])
        self.assertIsNot(result[1], custom)
        self.assertIsNot(result[0], child)
        reverse = clone_snapshot([custom, child])
        self.assertIs(reverse[0].payload, reverse[1])

    def test_custom_copier_self_cycle_is_preserved(self):
        custom = CustomEvidence(None)
        custom.payload = {"self": custom}
        result = clone_snapshot({"custom": custom})
        self.assertIs(result["custom"].payload["self"], result["custom"])
        self.assertIsNot(result["custom"], custom)

    def test_explicit_memo_overrides_and_lifetime_match_standard_copy(self):
        child, replacement = [1], ["replacement"]
        label = "synthetic-label"
        memo = {id(child): replacement, id(label): "mapped-label"}
        source = {label: child, "empty": None}
        result = clone_snapshot(source, memo)
        expected = copy.deepcopy(source, {id(child): replacement, id(label): "mapped-label"})
        self.assertEqual(result, expected)
        self.assertIs(result["mapped-label"], replacement)
        self.assertIs(memo[id(source)], result)
        self.assertTrue(any(item is source for item in memo[id(memo)]))
        self.assertIsNone(clone_snapshot(source, {id(source): None}))

    def test_dictionary_value_before_key_custom_copy_order_matches_standard(self):
        order = []

        class Traced:
            def __init__(self, name):
                self.name = name

            def __deepcopy__(self, memo):
                order.append(self.name)
                return type(self)(self.name)

        source = {Traced("key"): Traced("value")}
        copy.deepcopy(source)
        expected = list(order)
        order.clear()
        clone_snapshot(source)
        self.assertEqual(order, expected)

    def test_separate_snapshots_share_no_mutable_containers(self):
        source = {"nested": [{"values": [1, 2]}]}
        first, second = clone_snapshot(source), clone_snapshot(source)
        first["nested"][0]["values"].clear()
        self.assertEqual(second, source)
        self.assertIsNot(first["nested"], second["nested"])

    def test_recovered_run_full_and_summary_snapshots_remain_independent(self):
        from tpm_service import FrozenRun

        run = FrozenRun({"id": "f" * 32, "status": "finished", "phase": "finished", "config": {},
                         "metrics": {"errors": {"http_429": 1}},
                         "series": [{"errors": {"http_429": 1}}]})
        full, summary = run.snap(), run.snap(summary=True)
        full["series"][0]["errors"].clear()
        summary["metrics"]["errors"].clear()
        self.assertEqual(run.snap()["series"][0]["errors"], {"http_429": 1})
        self.assertEqual(run.snap(summary=True)["metrics"]["errors"], {"http_429": 1})
        self.assertNotIn("series", summary)


if __name__ == "__main__":
    unittest.main()
