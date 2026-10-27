# coding: utf-8
import unittest

import smart_route_load as load


class CacheProfilePayloadTests(unittest.TestCase):
    def test_cache_profile_keeps_prefix_stable_and_varies_only_question(self):
        first = load.build_payload("synthetic-model", "cache", None, None, None,
                                   stream=True, request_index=0)
        second = load.build_payload("synthetic-model", "cache", None, None, None,
                                    stream=True, request_index=1)
        first_text = first["messages"][0]["content"]
        second_text = second["messages"][0]["content"]
        prefix, first_question = first_text.rsplit("\n\n", 1)
        second_prefix, second_question = second_text.rsplit("\n\n", 1)
        self.assertEqual(prefix, second_prefix)
        self.assertNotEqual(first_question, second_question)
        self.assertNotIn("Q-000000", first_text)
        self.assertNotIn("Q-000001", second_text)
        self.assertGreater(len(prefix), 2000)
        self.assertEqual(first["max_tokens"], 64)

    def test_cache_profile_repeats_question_set_without_changing_prefix(self):
        first = load.build_payload("m", "cache", None, None, None, request_index=0)
        repeated = load.build_payload("m", "cache", None, None, None,
                                      request_index=len(load.CACHE_QUESTIONS))
        self.assertEqual(first["messages"][0]["content"], repeated["messages"][0]["content"])

    def test_cli_accepts_cache_profile(self):
        args = type("Args", (), {
            "concurrency": 1, "queue_size": 1, "rps": 1.0, "duration": 1,
            "iterations": 1, "mode": "smoke", "ramps": None,
            "profile": "cache", "connect_timeout": 1.0, "read_timeout": 1.0,
            "write_timeout": 1.0,
        })()
        load._validate_cli(args)
