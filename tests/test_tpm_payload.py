"""预编码请求体的逐字节等价与缓存生命周期；不访问任何上游。"""
import json
import unittest
from unittest.mock import patch

from tpm_config import validate_config
from tpm_corpus import Corpus


def config(**overrides):
    return validate_config({
        "url": "http://127.0.0.1:9/v1", "api_key": "synthetic-local-only",
        "model": '合成模型/"metadata-request"\\🧪', "seed": "独立合成种子🌱",
        "cache_write_price_mode": "input", **overrides,
    })


def serialize(corpus, request_id, group=0):
    return json.dumps(corpus.request_body(request_id, group), ensure_ascii=False,
                      separators=(",", ":")).encode()


class TpmPayloadTests(unittest.TestCase):
    def test_bytes_match_original_body_for_modes_groups_escaping_and_output_limits(self):
        identifiers = ("same-id", "", '引号"反斜线\\换行\n回车\r制表\t空字节\0🧪',
                       'metadata-request local-tokenizer-probe "content":""')
        for mode in ("prefer_cache", "avoid_cache"):
            for parameter in ("max_tokens", "max_completion_tokens"):
                corpus = Corpus(config(cache_mode=mode, template_groups=3,
                                       output_limit_parameter=parameter, max_output_tokens=777))
                for group in (-1, 0, 1, 2, 5, "7"):
                    for identifier in identifiers:
                        with self.subTest(mode=mode, parameter=parameter, group=group, identifier=identifier):
                            actual = corpus.request_bytes(identifier, group)
                            self.assertEqual(actual, serialize(corpus, identifier, group))
                            self.assertEqual(json.loads(actual), corpus.request_body(identifier, group))
                            self.assertEqual(json.loads(actual)[parameter], 777)

    def test_full_length_requests_do_not_reencode_or_rebuild_stable_prefix(self):
        for mode in ("prefer_cache", "avoid_cache"):
            with self.subTest(mode=mode):
                corpus = Corpus(config(cache_mode=mode, template_groups=2))
                self.assertTrue(corpus.resize_from_usage(2500))
                identifiers = [f"synthetic-{index}-转义\"\\\n" for index in range(8)]
                expected = [serialize(corpus, identifier, index % 2)
                            for index, identifier in enumerate(identifiers)]
                self.assertGreater(min(map(len, expected)), 200000)
                encoded_prefixes = corpus._encoded_prefixes
                original_dumps, encoded_values = json.dumps, []

                def encode_short_only(value, **kwargs):
                    self.assertIsInstance(value, str)
                    self.assertLess(len(value), 1000, "request encoding revisited the long prefix")
                    encoded_values.append(value)
                    return original_dumps(value, **kwargs)

                with patch("tpm_corpus.json.dumps", side_effect=encode_short_only), \
                        patch.object(corpus, "request_body", side_effect=AssertionError("rebuilt full request")), \
                        patch.object(corpus, "messages", side_effect=AssertionError("rebuilt long messages")):
                    actual = [corpus.request_bytes(identifier, index % 2)
                              for index, identifier in enumerate(identifiers)]
                self.assertEqual(actual, expected)
                self.assertIs(corpus._encoded_prefixes, encoded_prefixes)
                self.assertEqual(len(encoded_values), len(identifiers) * (2 if mode == "avoid_cache" else 1))

    def test_usage_resize_refreshes_every_group_and_noop_preserves_cached_prefixes(self):
        corpus = Corpus(config(template_groups=3))
        old = [corpus.request_bytes("unchanged", group) for group in range(3)]
        old_prefixes, old_fingerprint = corpus._encoded_prefixes, corpus.fingerprint
        self.assertTrue(corpus.resize_from_usage(2500))
        self.assertIsNot(corpus._encoded_prefixes, old_prefixes)
        self.assertNotEqual(corpus.fingerprint, old_fingerprint)
        for group in range(3):
            with self.subTest(group=group):
                encoded = corpus.request_bytes("unchanged", group)
                self.assertNotEqual(encoded, old[group])
                self.assertGreater(len(encoded), len(old[group]) * 10)
                self.assertEqual(encoded, serialize(corpus, "unchanged", group))
        self.assertEqual(corpus.metadata()["request_body_bytes"],
                         len(serialize(corpus, "metadata-request")))
        prefixes = corpus._encoded_prefixes
        self.assertFalse(corpus.resize_from_usage(49500))
        self.assertIs(corpus._encoded_prefixes, prefixes)
        self.assertEqual(corpus.request_bytes("after-verification"), serialize(corpus, "after-verification"))

    def test_tokenizer_fitting_and_later_usage_resize_keep_encoding_current(self):
        class SyntheticTokenizer:
            def encode(self, text):
                return text.split()

        corpus = Corpus(config(input_tokens=6000, target_tpm=1_000_000,
                               template_groups=2), tokenizer=SyntheticTokenizer())
        self.assertNotEqual(corpus.units, 2048)
        self.assertLess(abs(corpus.local_input_tokens - 6000), 40)
        before = corpus._encoded_prefixes
        for group in range(2):
            self.assertEqual(corpus.request_bytes("fitted", group), serialize(corpus, "fitted", group))
        self.assertTrue(corpus.resize_from_usage(3000))
        self.assertIsNot(corpus._encoded_prefixes, before)
        for group in range(2):
            self.assertEqual(corpus.request_bytes("resized", group), serialize(corpus, "resized", group))

    def test_instances_groups_and_callers_cannot_reuse_or_mutate_other_payloads(self):
        original_config = config(template_groups=2)
        first, repeat = Corpus(original_config), Corpus(original_config)
        different = Corpus(config(template_groups=2, seed="不同种子", model="独立模型",
                                  output_limit_parameter="max_completion_tokens", max_output_tokens=600))
        expected = first.request_bytes("same")
        original_config["model"] = "caller-mutated"
        body = first.request_body("same")
        body["messages"][0]["content"] = "caller-mutated"
        body["stream_options"]["include_usage"] = False
        self.assertEqual(first.request_bytes("same"), expected)
        self.assertEqual(repeat.request_bytes("same"), expected)
        self.assertNotEqual(first.request_bytes("same", 1), expected)
        self.assertNotEqual(different.request_bytes("same"), expected)
        self.assertEqual(different.request_bytes("same"), serialize(different, "same"))
        self.assertTrue(first.resize_from_usage(2500))
        self.assertEqual(repeat.request_bytes("same"), expected)
        self.assertNotEqual(first.request_bytes("same"), expected)

    def test_avoid_cache_marker_remains_early_and_only_request_specific(self):
        corpus = Corpus(config(cache_mode="avoid_cache"))
        cached_prefix = corpus._encoded_prefixes[0]
        first = json.loads(corpus.request_bytes("request-A"))["messages"][0]["content"]
        second = json.loads(corpus.request_bytes("request-B"))["messages"][0]["content"]
        self.assertNotEqual(first.splitlines()[0], second.splitlines()[0])
        self.assertIn(corpus.prefixes[0], first)
        self.assertIn(corpus.prefixes[0], second)
        self.assertIs(corpus._encoded_prefixes[0], cached_prefix)
        self.assertEqual(corpus.request_bytes("request-A"), serialize(corpus, "request-A"))


if __name__ == "__main__":
    unittest.main()
