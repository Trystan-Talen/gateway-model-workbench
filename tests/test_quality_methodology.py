"""方法快照、约束评分、交替执行与历史报告的隔离回归。"""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import route_test_ui as ui
from quality_methodology import DIMENSIONS, SOURCES, methodology_snapshot
from quality_report import build_quality_report, render_quality_markdown
from quality_test import QualityCase, default_suite, score_case, suite_manifest


class MethodologyTests(unittest.TestCase):
    def setUp(self):
        self.cases = default_suite()
        self.constraints = [c for c in self.cases if c.evaluator['type'] == 'verifiable_constraints']

    def test_sources_and_dimensions_are_resolvable_not_official_scores(self):
        sources = {s['id'] for s in SOURCES}
        self.assertEqual(len(sources), 7)
        self.assertEqual(set(DIMENSIONS), {c.category for c in self.cases})
        self.assertEqual(len(self.constraints), 10)
        for source in SOURCES:
            self.assertTrue(source['url'].startswith('https://'))
            self.assertTrue(source['reference'].startswith('https://'))
            self.assertTrue(source['adopted'] and source['not_covered'])
        for case in self.cases:
            self.assertTrue(case.benchmark_refs)
            self.assertTrue(set(case.benchmark_refs) <= sources)
            self.assertEqual(QualityCase.from_dict(case.to_dict()), case)
        self.assertIn('未导入官方数据集', methodology_snapshot()['provenance'])

    def test_manifest_preserves_actual_lengths_and_independent_method_copy(self):
        manifest = suite_manifest(cases=self.cases)
        lengths = [len(c.prompt) for c in self.cases]
        self.assertEqual(manifest['input_chars'], {'min': min(lengths), 'max': max(lengths), 'total': sum(lengths)})
        self.assertEqual(manifest['methodology']['version'], 3)
        manifest['methodology']['sources'][0]['adopted'] = 'changed'
        self.assertNotEqual(suite_manifest()['methodology']['sources'][0]['adopted'], 'changed')

    def test_old_suite_is_not_assigned_new_methodology(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'old.json'
            path.write_text(json.dumps({'name': 'old', 'version': 1, 'cases': [self.cases[0].to_dict()]}))
            self.assertFalse(suite_manifest(path)['methodology'])

    def test_valid_alternative_is_not_compared_with_reference_prose(self):
        case = self.constraints[0]
        alternate = '- 备份现有数据\n- 校验备份完整性\n- 预备发布失败后的回滚方案'
        self.assertNotEqual(alternate, case.reference_answer)
        result = score_case(case, alternate)
        self.assertTrue(result.passed, result.failures)
        self.assertEqual(result.score, 1)
        self.assertEqual(len(result.checks), len(case.evaluator['rules']))

    def test_partial_constraints_are_zero_with_exact_failed_label(self):
        case = self.constraints[0]
        # 删除首行前缀，仍满足行数、关键词等其他约束。
        response = str(case.reference_answer).removeprefix('- ')
        result = score_case(case, response)
        failed = next(rule['label'] for rule in case.evaluator['rules'] if rule['kind'] == 'each_line_prefix')
        self.assertEqual(result.failures, (failed,))
        self.assertEqual(result.score, 0)
        self.assertFalse(result.passed)
        self.assertGreater(len(result.checks), len(result.failures))

    def test_invalid_rule_cannot_pass(self):
        raw = self.constraints[0].to_dict()
        raw['evaluator'] = {'type': 'verifiable_constraints', 'rules': [{'kind': 'unknown', 'label': '坏规则'}]}
        result = score_case(QualityCase.from_dict(raw), 'response')
        self.assertFalse(result.passed)
        self.assertEqual(result.score, 0)
        self.assertTrue(result.failures)

    def resource(self, failure=None, network=False):
        results = []
        for case in self.cases:
            response = case.reference_answer
            if case.id == failure:
                response = str(response).removeprefix('- ')
            score = score_case(case, response)
            results.append(dict(case.to_dict(), case_id=case.id, completed=not (case.id == failure and network),
                                score=score.score, passed=score.passed, checks=list(score.checks), failures=list(score.failures)))
        return {'name': '隔离报告样例', 'planned_cases': len(results), 'case_results': results, 'request_errors': []}

    def test_auxiliary_constraint_diagnostics_do_not_inflate_main_score(self):
        case = self.constraints[0]
        report = build_quality_report('finished', [self.resource(failure=case.id)], 120, suite_manifest())
        resource = report['resources'][0]
        checks = resource['constraint_diagnostics']
        total = sum(len(c.evaluator['rules']) for c in self.constraints)
        self.assertEqual((checks['cases'], checks['passed_cases']), (10, 9))
        self.assertEqual((checks['checks'], checks['passed_checks']), (total, total - 1))
        self.assertEqual(resource['score_pct'], 99.17)
        self.assertEqual(resource['passed_cases'], 119)

    def test_network_error_cannot_claim_passed_constraints(self):
        case = self.constraints[0]
        resource = self.resource()
        next(c for c in resource['case_results'] if c['case_id'] == case.id)['completed'] = False
        report = build_quality_report('finished_with_errors', [resource], 120, suite_manifest())
        checks = report['resources'][0]['constraint_diagnostics']
        total = sum(len(c.evaluator['rules']) for c in self.constraints)
        self.assertEqual(checks['passed_cases'], 9)
        self.assertEqual(checks['passed_checks'], total - len(case.evaluator['rules']))
        self.assertFalse(report['resources'][0]['eligible'])

    def test_historical_report_does_not_rewrite_protocol_or_method(self):
        resource = self.resource()
        report = build_quality_report('finished', [resource, resource], 120, {})
        self.assertEqual(report['execution']['id'], 'legacy_resource_sequential')
        self.assertFalse(report['methodology'])
        self.assertNotIn('交替', report['pairwise']['method'])

    def test_export_uses_frozen_protocol_and_source_disclosures(self):
        manifest = suite_manifest()
        manifest['execution'] = methodology_snapshot()['protocol']
        resource = self.resource()
        report = build_quality_report('finished', [resource, resource], 120, manifest)
        self.assertIn('交替', report['pairwise']['method'])
        text = render_quality_markdown({'name': '方法测试', 'id': 'isolated', 'config': {'suite_manifest': manifest}, 'report': report})
        self.assertIn('约束', text)
        self.assertIn('未导入官方数据集', text)
        for source in SOURCES:
            self.assertIn(source['name'], text)
            self.assertIn(source['url'], text)
            self.assertIn(source['not_covered'], text)


class AlternatingRunnerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for field, value in [('BASE_DIR', temp.name), ('HISTORY_FILE', str(self.root/'index.json')), ('TASKS_FILE', str(self.root/'tasks.json'))]:
            patch = mock.patch.object(ui, field, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(ui, 'persist_history')
        patch.start()
        self.addCleanup(patch.stop)
        self.cases = default_suite()[:4]
        self.suite = self.root / 'slice.json'
        self.suite.write_text(json.dumps({'name': 'isolated-slice', 'version': 3, 'methodology_version': 3,
                                         'cases': [c.to_dict() for c in self.cases]}))

    def task(self, count=2):
        return ui.QualityTask(ui.validate_quality_config({'name': '隔离顺序回归', 'capture_raw': True, 'suite_path': str(self.suite),
            'resources': [{'name': name, 'url': 'http://127.0.0.1:9/v1', 'model': name,
                           'api_key': 'local-only-secret'} for name in ('甲', '乙')[:count]]}))

    def test_actual_request_order_progress_and_independent_clients(self):
        task = self.task()
        calls, progress, clients = [], [], {}
        async def request(client, item, payload, *args, **kwargs):
            calls.append((item['id'], payload['model']))
            progress.append([len(r['case_results']) for r in task.results])
            clients.setdefault(payload['model'], set()).add(id(client))
            self.assertEqual(payload['messages'], [{'role': 'user', 'content': self.cases[item['id']].prompt}])
            self.assertNotIn('reference_answer', payload)
            self.assertNotIn('expected', payload)
            self.assertEqual(payload['temperature'], 0)
            return {'status_code': 200, 'completed': True, 'response_text': self.cases[item['id']].reference_answer}
        with mock.patch.object(ui, 'do_request', side_effect=request):
            asyncio.run(ui.run_quality_task(task))
        self.assertEqual(calls, [(0,'甲'),(0,'乙'),(1,'乙'),(1,'甲'),(2,'甲'),(2,'乙'),(3,'乙'),(3,'甲')])
        self.assertEqual(progress[:4], [[0,0],[1,0],[1,1],[1,2]])
        self.assertEqual([len(v) for v in clients.values()], [1,1])
        self.assertNotEqual(clients['甲'], clients['乙'])
        self.assertEqual(task.status, 'finished')
        self.assertEqual(task.snap()['completed_cases'], 8)
        for resource, sequences in zip(task.results, ([1,4,5,8], [2,3,6,7])):
            self.assertEqual([c['execution_sequence'] for c in resource['case_results']], sequences)
            self.assertEqual([c['case_id'] for c in resource['case_results']], [c.id for c in self.cases])
        self.assertTrue(all(not r['api_key'] for r in task.config['resources']))

    def test_single_resource_runs_sequentially_without_false_pairing(self):
        task = self.task(1)
        calls = []
        async def request(client, item, payload, *args, **kwargs):
            calls.append(item['id'])
            return {'status_code': 200, 'completed': True, 'response_text': self.cases[item['id']].reference_answer}
        with mock.patch.object(ui, 'do_request', side_effect=request):
            asyncio.run(ui.run_quality_task(task))
        self.assertEqual(calls, [0,1,2,3])
        self.assertEqual(task.snap()['report']['execution']['id'], 'single_sequential_v1')

    def test_network_failures_are_not_retried_or_removed(self):
        task = self.task()
        calls = []
        async def request(client, item, payload, *args, **kwargs):
            calls.append((item['id'], payload['model']))
            if item['id'] == 1 and payload['model'] == '乙':
                raise OSError('isolated simulated failure')
            return {'status_code': 200, 'completed': True, 'response_text': self.cases[item['id']].reference_answer}
        with mock.patch.object(ui, 'do_request', side_effect=request):
            asyncio.run(ui.run_quality_task(task))
        self.assertEqual(len(calls), 8)
        self.assertEqual(task.status, 'finished_with_errors')
        self.assertEqual(task.results[1]['case_results'][1]['score'], 0)
        self.assertFalse(task.results[1]['case_results'][1]['completed'])
        self.assertEqual(task.snap()['report']['resources'][1]['request_errors'], 1)

    def test_stopping_mid_pair_preserves_only_actual_evidence(self):
        task = self.task()
        calls = []
        async def request(client, item, payload, *args, **kwargs):
            calls.append(item['id'])
            if len(calls) == 5:
                task.stop_event.set()
            return {'status_code': 200, 'completed': True, 'response_text': self.cases[item['id']].reference_answer}
        with mock.patch.object(ui, 'do_request', side_effect=request):
            asyncio.run(ui.run_quality_task(task))
        self.assertEqual(task.status, 'stopped')
        self.assertEqual(len(calls), 5)
        self.assertEqual([len(r['case_results']) for r in task.results], [3,2])
        self.assertFalse(task.snap()['report']['evidence']['complete'])

    def test_failed_evidence_write_does_not_publish_progress(self):
        task = self.task()
        persist = task.persist
        writes = []
        def fail_second_write():
            writes.append(True)
            if len(writes) == 2:
                raise OSError('isolated disk write failure')
            return persist()
        with mock.patch.object(task, 'persist', side_effect=fail_second_write), mock.patch.object(ui, 'do_request', new=mock.AsyncMock(return_value={'status_code':200, 'completed':True, 'response_text':'27'})) as request:
            with self.assertRaises(OSError):
                asyncio.run(ui.run_quality_task(task))
        self.assertEqual(request.call_count, 1)
        self.assertEqual(task.status, 'error')
        self.assertEqual(task.snap()['completed_cases'], 0)
        self.assertFalse(task.snap()['report']['evidence']['complete'])
        self.assertTrue(all(not r['api_key'] for r in task.config['resources']))


if __name__ == '__main__':
    unittest.main()
