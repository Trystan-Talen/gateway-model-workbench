"""区分度层与事实层自洽的回归：分数接近时，报告必须说清「为什么分辨不了」。

这批断言直接锚定本轮真实观测：两个资源在 120 题上是 114 平、3 比 3 的
分裂，总分相同但失败题完全不重叠。此前的报告只写「分数接近」，
读者无法区分「两个资源真的差不多」和「这份卷子到顶了」。
本文件把两者的差别固化成可重跑的检查。
"""
import unittest

from quality_report import build_quality_report, render_quality_markdown
from quality_test import default_suite, suite_manifest

# 本轮实测的两侧失败题（互不重叠），用于复现「总分相同、失效点不同」。
GLM_WRONG = ['instruction_following_03', 'instruction_following_08', 'robustness_03']
FLASH_WRONG = ['code_reasoning_07', 'data_analysis_07', 'robustness_04']


class DiscriminationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = default_suite()
        cls.manifest = suite_manifest(cases=cls.cases)

    def resource(self, wrong=(), name='A', n=120):
        values = []
        for case in self.cases[:n]:
            failed = case.id in wrong
            values.append(dict(case_id=case.id, category=case.category, title=case.title, prompt=case.prompt,
                               evaluator=dict(case.evaluator), critical=case.critical,
                               score=int(not failed), passed=not failed, completed=True,
                               total_ms=100, failures=[]))
        return dict(name=name, planned_cases=120, case_results=values, request_errors=[])

    def report(self, *resources):
        return build_quality_report('finished', list(resources), 120, self.manifest)

    def test_split_verdict_is_cannot_resolve_not_equivalent(self):
        r = self.report(self.resource(GLM_WRONG, name='glm-5.3'), self.resource(FLASH_WRONG, name='deepseek-flash'))
        d = r['discrimination']
        self.assertEqual((d['pairs'], d['ties'], d['a_wins'], d['b_wins']), (120, 114, 3, 3))
        self.assertEqual(d['diff_pct'], 0.0)
        self.assertEqual(d['mcnemar_p'], 1.0)
        self.assertFalse(d['detectable'])
        self.assertEqual(d['verdict'], '分数接近，且卷子无法分辨')
        self.assertIn('无法分辨', d['basis'])
        # 置信区间必须跨过 0：这正是「不能判定一侧更好」的证据形式。
        low, high = d['ci95_pct']
        self.assertLess(low, 0)
        self.assertGreater(high, 0)

    def test_disjoint_failures_reported_as_distinct_breakage(self):
        r = self.report(self.resource(GLM_WRONG, name='A'), self.resource(FLASH_WRONG, name='B'))
        d = r['discrimination']
        self.assertEqual(d['disjoint_failure_cases'], 6)
        self.assertTrue(any('互不重叠' in note for note in d['notes']))

    def test_saturation_and_resolution_floor_are_surfaced(self):
        r = self.report(self.resource(GLM_WRONG, name='A'), self.resource(FLASH_WRONG, name='B'))
        d = r['discrimination']
        # 12 个维度里 8 个两侧都满分，对这对资源已无区分力。
        self.assertEqual(len(d['saturated_categories']), 8)
        self.assertEqual(d['per_dimension_cases'], 10)
        self.assertEqual(d['score_resolution_pct'], 10.0)
        self.assertGreaterEqual(d['cases_needed_for_10pct_dimension'], 300)
        self.assertIsNotNone(d['min_detectable_delta_pct'])

    def test_identical_resources_report_zero_discordance(self):
        r = self.report(self.resource(name='A'), self.resource(name='B'))
        d = r['discrimination']
        self.assertEqual(d['ties'], 120)
        self.assertEqual(d['discordant'], 0)
        self.assertEqual(d['band'], '结果完全一致')
        self.assertEqual(d['verdict'], '完全一致，无法比较')

    def test_one_sided_sweep_is_called_out(self):
        extra = sorted(GLM_WRONG + FLASH_WRONG)
        r = self.report(self.resource(name='A'), self.resource(extra, name='B'))
        d = r['discrimination']
        self.assertEqual((d['a_wins'], d['b_wins']), (6, 0))
        self.assertTrue(d['detectable'])
        self.assertLess(d['mcnemar_p'], 0.05)
        self.assertEqual(d['verdict'], '不一致题足以指向一侧')

    def test_single_resource_report_has_no_discrimination_layer(self):
        r = self.report(self.resource())
        self.assertNotIn('discrimination', r)
        self.assertEqual(r['mode'], 'single')

    def test_statistics_notes_do_not_claim_no_interval(self):
        r = self.report(self.resource(GLM_WRONG), self.resource(FLASH_WRONG, name='B'))
        notes = ' '.join(r['layers']['statistics']['notes'])
        self.assertIn('区分度', notes)
        self.assertNotIn('不给置信区间', notes)

    def test_markdown_exposes_verdict_and_sensitivity(self):
        r = self.report(self.resource(GLM_WRONG, name='glm-5.3'), self.resource(FLASH_WRONG, name='deepseek-flash'))
        text = render_quality_markdown({'name': '对照', 'id': 'T', 'case_count': 120,
                                        'config': {'suite_manifest': self.manifest}, 'report': r})
        self.assertIn('区分度与敏感性', text)
        self.assertIn('分数接近，且卷子无法分辨', text)
        self.assertIn('McNemar', text)
        self.assertIn('置信区间', text)


class FactsLayerTests(unittest.TestCase):
    """事实层的三类失败计数必须与「未通过题数」自洽。"""

    @classmethod
    def setUpClass(cls):
        cls.cases = default_suite()
        cls.manifest = suite_manifest(cases=cls.cases)

    def resource(self, wrong=(), broken_json=(), error=(), name='A'):
        values = []
        for case in self.cases:
            failed = case.id in wrong
            failures = []
            evaluator = dict(case.evaluator)
            if case.id in broken_json:
                # 真实走一遍结构化评分器，直接产出 valid_json 失败标记。
                evaluator = {'type': 'structured_json', 'fields': []}
                failures = ['valid_json:False']
                failed = True
            if case.id in error:
                failed = True
            values.append(dict(case_id=case.id, category=case.category, title=case.title, prompt=case.prompt,
                               evaluator=evaluator, critical=case.critical,
                               score=int(not failed), passed=not failed, completed=case.id not in error,
                               total_ms=100, failures=['valid_json'] if case.id in broken_json else failures))
        return dict(name=name, planned_cases=120, case_results=values,
                    request_errors=[{'case_id': x} for x in error])

    def items(self, report, resource=0):
        return {item['key']: item['value'] for item in report['facts'][resource]['items']}

    def test_unpassed_equals_sum_of_three_kinds(self):
        r = build_quality_report('finished', [self.resource(wrong=['numerical_reasoning_01'],
                                                            broken_json=['structured_extraction_01'],
                                                            error=['logical_reasoning_01'])], 120, self.manifest)
        items = self.items(r)
        self.assertEqual(items['link_failures'], 1)
        self.assertEqual(items['format_failures'], 1)
        self.assertEqual(items['content_failures'], 1)
        self.assertEqual(items['failed_cases'], 3)
        self.assertEqual(items['passed_cases'], 117)

    def test_headline_matches_kind_totals(self):
        r = build_quality_report('finished', [self.resource(wrong=['numerical_reasoning_01',
                                                                    'numerical_reasoning_02'])], 120, self.manifest)
        facts = r['facts'][0]
        items = {item['key']: item['value'] for item in facts['items']}
        self.assertEqual(items['failed_cases'], 2)
        self.assertIn('未通过 2 题', facts['headline'])
        self.assertEqual(items['failed_cases'],
                         items['link_failures'] + items['format_failures'] + items['content_failures'])

    def test_all_passed_shows_zero_unpassed(self):
        r = build_quality_report('finished', [self.resource()], 120, self.manifest)
        items = self.items(r)
        self.assertEqual(items['failed_cases'], 0)
        self.assertEqual(items['passed_cases'], 120)
        self.assertIn('未通过 0 题', r['facts'][0]['headline'])


if __name__ == '__main__':
    unittest.main()
