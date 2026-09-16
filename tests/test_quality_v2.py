"""120题、诊断报告、资源对照和真实执行状态的可重复回归。"""
import asyncio
from collections import Counter
import copy
from fractions import Fraction
import itertools
import json
import math
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import route_test_ui as ui
from quality_test import default_suite, score_case, suite_manifest
from quality_report import build_quality_report, render_quality_markdown, REQUIRED_CATEGORIES


class SuiteV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = default_suite()
        cls.by_id = {c.id: c for c in cls.cases}

    def test_full_suite_is_diverse_and_auditable(self):
        self.assertEqual(len(self.cases), 120)
        self.assertEqual(len({c.prompt for c in self.cases}), 120)
        self.assertEqual(len(self.by_id), 120)
        self.assertEqual(Counter(c.category for c in self.cases), dict.fromkeys(REQUIRED_CATEGORIES, 10))
        self.assertEqual(Counter(c.difficulty for c in self.cases), {'基础':48,'进阶':48,'挑战':24})
        for c in self.cases:
            self.assertTrue(c.title and c.reference_answer and c.rationale)
            self.assertEqual(len(c.negative_examples), 3)
            self.assertNotIn('expected', c.request)
            self.assertNotIn('messages', c.request)
            self.assertEqual(c.request['temperature'], 0)
        for c in self.cases:
            if c.category == 'long_context':
                self.assertGreater(len(c.prompt), 4000)

    def test_all_120_references_pass_and_360_negatives_fail(self):
        for c in self.cases:
            with self.subTest(case=c.id):
                self.assertEqual(score_case(c, c.reference_answer).score, 1)
                for wrong in (*c.negative_examples, '', None):
                    self.assertEqual(score_case(c, wrong).score, 0)

    def test_json_rules_reject_duplicate_keys_types_and_extra_fields(self):
        c=self.by_id['structured_extraction_01']
        for wrong in ['{"name":"澄川","count":3,"paid":true,"count":3}',
                      '{"name":"澄川","count":3,"paid":1}',
                      '{"name":"澄川","count":"3","paid":true}',
                      '{"name":"澄川","count":3.0,"paid":true}',
                      '{"name":"澄川","count":3,"paid":true,"extra":0}',
                      '{"name":"澄川","count":NaN,"paid":true}']:
            self.assertFalse(score_case(c,wrong).passed)
        self.assertTrue(score_case(c,' { "paid":true, "count":3, "name":"澄川" } ').passed)

    def test_independent_numeric_and_scheduling_oracles(self):
        # 独立计算而不是再次使用题目中的 expected 字段作为预期。
        schedule=min(max(sum(d for d,m in zip([8,7,6,5,4],assignment) if m==i) for i in range(3))
                     for assignment in itertools.product(range(3),repeat=5))
        expected=[27,240*85//100+12,'75%',(11*60+12)-(9*60+47),(10*80+30*120)//40,
                  math.ceil(487/24),1000*120*80//10000,96*3//12,30+28-(50-6),schedule]
        for i,value in enumerate(expected,1):
            self.assertEqual(self.by_id[f'numerical_reasoning_{i:02}'].reference_answer,str(value))
        self.assertEqual(self.by_id['data_analysis_05'].reference_answer,str(Fraction(95,110)))
        orders=[''.join(x) for x in itertools.permutations('ABCD') if x.index('A')<x.index('C') and x.index('B')==x.index('A')+1 and x.index('D')<x.index('B')]
        self.assertEqual(orders,[self.by_id['logical_reasoning_05'].reference_answer])

    def test_independent_python_and_sql_oracles(self):
        # 仅执行测试作者写死的安全代码，绝不执行上游生成代码。
        xs=[1]; ys=xs; ys.append(2)
        nested=[[1],[2]]; copied=nested.copy(); copied[0].append(3)
        fs=[lambda:i for i in range(3)]
        expected={1:[1,2,3,4,5][1:4:2],2:xs,3:nested,4:sum(i for i in range(1,5) if i%2==0),
                  5:False and (1/0),6:[] or [7],9:[f() for f in fs]}
        with sqlite3.connect(':memory:') as db:
            db.executescript('CREATE TABLE T(v); INSERT INTO T VALUES (1),(NULL),(3);')
            expected[7]=list(db.execute('SELECT COUNT(v),COUNT(*) FROM T').fetchone())
            db.executescript('CREATE TABLE A(id); CREATE TABLE B(id); INSERT INTO A VALUES(1),(2); INSERT INTO B VALUES(2),(3);')
            expected[8]=[row[0] for row in db.execute('SELECT B.id FROM A LEFT JOIN B ON A.id=B.id ORDER BY A.id')]
            db.executescript("CREATE TABLE sales(category,amount); INSERT INTO sales VALUES('A',4),('A',7),('B',5),('B',3);")
            expected[10]=dict(db.execute('SELECT category,SUM(amount) FROM sales GROUP BY category HAVING SUM(amount)>9'))
        for n,value in expected.items():
            self.assertEqual(json.loads(self.by_id[f'code_reasoning_{n:02}'].reference_answer),value)

    def test_fingerprint_tracks_actual_case_content(self):
        m=suite_manifest(cases=self.cases)
        raw=self.cases[0].to_dict();raw['prompt']+='变更'
        from quality_test import QualityCase
        updated=(QualityCase.from_dict(raw),)+self.cases[1:]
        self.assertNotEqual(m['fingerprint'],suite_manifest(cases=updated)['fingerprint'])
        self.assertEqual(m['case_count'],120)


class ReportV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases=default_suite();cls.manifest=suite_manifest(cases=cls.cases)

    def resource(self, wrong=(), error=(), name='A', n=120):
        values=[]
        for case in self.cases[:n]:
            values.append(dict(case_id=case.id,category=case.category,title=case.title,prompt=case.prompt,
                               evaluator=dict(case.evaluator),critical=case.critical,
                               score=int(case.id not in wrong and case.id not in error),
                               passed=case.id not in wrong and case.id not in error,
                               completed=case.id not in error,total_ms=100,failures=[]))
        return dict(name=name,planned_cases=120,case_results=values,request_errors=[{'case_id':x} for x in error])

    def report(self,*resources,status='finished',manifest=None,count=120):
        return build_quality_report(status,list(resources),count,self.manifest if manifest is None else manifest)

    def test_single_report_has_diagnosis_not_selection(self):
        r=self.report(self.resource())
        self.assertEqual(r['mode'],'single');self.assertIn('single',r);self.assertNotIn('pairwise',r)
        self.assertEqual(r['resources'][0]['score_pct'],100)
        self.assertTrue(r['resources'][0]['eligible'])
        self.assertEqual(len(r['single']['strengths']),12)
        self.assertIn('复核',r['title']);self.assertIn('未检验',r['evidence']['repeatability'])

    def test_partial_high_score_is_not_final(self):
        r=self.report(self.resource(n=5),status='running')
        self.assertEqual(r['resources'][0]['score_pct'],100)
        self.assertFalse(r['evidence']['complete']);self.assertFalse(r['resources'][0]['eligible'])
        self.assertNotIn('single',r);self.assertEqual(r['decision']['status'],'pending')

    def test_request_failure_cannot_pass_even_if_stored_score_claims_success(self):
        a=self.resource();a['case_results'][0]['completed']=False
        r=self.report(a)['resources'][0]
        self.assertLess(r['score_pct'],100);self.assertEqual(r['passed_cases'],119)
        self.assertFalse(r['eligible']);self.assertEqual(r['request_errors'],1)

    def test_high_total_does_not_override_critical_failure(self):
        a=self.resource(wrong=['safety_boundary_01'])
        r=self.report(a)['resources'][0]
        self.assertGreater(r['score_pct'],99);self.assertFalse(r['eligible'])
        self.assertEqual(r['critical_failures'],['safety_boundary_01'])

    def test_both_bad_are_not_qualified_because_similar(self):
        wrong=[c.id for c in self.cases]
        r=self.report(self.resource(wrong),self.resource(wrong,name='B'))
        self.assertEqual(r['mode'],'comparison');self.assertNotIn('single',r)
        self.assertEqual(r['pairwise']['both_failed'],120);self.assertEqual(r['pairwise']['ties'],120)
        self.assertEqual(r['pairwise']['selection'],'两侧均暂不建议')

    def test_equal_score_never_means_equivalent(self):
        r=self.report(self.resource(),self.resource(name='B'))
        self.assertTrue(r['pairwise']['comparable']);self.assertEqual(r['pairwise']['ties'],120)
        self.assertIn('尚无充分依据',r['title']);self.assertIn('不能把接近视为等效',r['decision']['recommendation'])
        self.assertFalse(r['pairwise']['same_endpoint_model'])

    def test_paired_wins_gains_regressions_and_shared_failures(self):
        a=self.resource(wrong=['numerical_reasoning_01','numerical_reasoning_03'])
        b=self.resource(wrong=['numerical_reasoning_02','numerical_reasoning_03'],name='B')
        p=self.report(a,b)['pairwise']
        self.assertEqual((p['a_wins'],p['b_wins'],p['ties'],p['both_failed']),(1,1,118,1))
        self.assertEqual(p['gains'][0]['case_id'],'numerical_reasoning_01')
        self.assertEqual(p['regressions'][0]['case_id'],'numerical_reasoning_02')

    def test_only_eligible_resource_is_recommended_despite_lower_total(self):
        a=self.resource(wrong=['numerical_reasoning_01','numerical_reasoning_02'])
        b=self.resource(wrong=['safety_boundary_01'],name='B')
        r=self.report(a,b)
        self.assertGreater(r['resources'][1]['score_pct'],r['resources'][0]['score_pct'])
        self.assertEqual(r['pairwise']['selection'],'优先复核 A')

    def test_pairing_requires_actual_prompts_and_rules(self):
        for field in ('prompt','evaluator','category'):
            a=self.resource();b=self.resource(name='B');b['case_results'][0].pop(field)
            p=self.report(a,b)['pairwise']
            self.assertFalse(p['comparable']);self.assertEqual(p['paired_cases'],119)
        a=self.resource();b=self.resource(name='B');b['case_results'][0]['prompt']+='changed'
        self.assertFalse(self.report(a,b)['pairwise']['comparable'])

    def test_old_five_case_history_is_never_promoted(self):
        a=self.resource(n=5);a['planned_cases']=5
        r=self.report(a,count=5,manifest={})
        self.assertTrue(r['evidence']['complete']);self.assertFalse(r['resources'][0]['eligible'])
        self.assertEqual(r['resources'][0]['total_cases'],5);self.assertIn('证据不足',r['title'])

    def test_duplicate_evidence_does_not_make_complete(self):
        a=self.resource();a['case_results'][-1]=a['case_results'][0]
        self.assertFalse(self.report(a)['evidence']['complete'])

    def test_mismatched_category_coverage_cannot_claim_complete(self):
        a=self.resource();a['case_results'][0]['category']='another_category'
        report=self.report(a)
        self.assertFalse(report['evidence']['complete'])
        self.assertFalse(report['resources'][0]['eligible'])

    def test_readable_exports_are_mode_specific_and_auditable(self):
        for resources in ([self.resource()], [self.resource(),self.resource(wrong=['safety_boundary_01'],name='B')]):
            snapshot={'name':'验收报告','id':'local-report','case_count':120,
                      'config':{'suite_manifest':self.manifest,'capture_raw':False},
                      'report':self.report(*resources)}
            exported=render_quality_markdown(snapshot)
            self.assertIn(self.manifest['fingerprint'],exported)
            self.assertIn('仅保留输出预览',exported)
            self.assertIn('不是行业标准',exported)
            if len(resources)==1:
                self.assertIn('单资源能力诊断',exported)
                self.assertIn('适用场景',exported)
                self.assertNotIn('## 配对选择依据',exported)
            else:
                self.assertIn('## 配对选择依据',exported)
                self.assertIn('safety_boundary_01',exported)
                self.assertNotIn('## 单资源诊断',exported)


class RunnerV2Tests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        for field,val in [('BASE_DIR',temp.name),('HISTORY_FILE',str(Path(temp.name)/'index.json')),('TASKS_FILE',str(Path(temp.name)/'tasks.json'))]:
            patch=mock.patch.object(ui,field,val);patch.start();self.addCleanup(patch.stop)
        self.resource={'name':'本地测试','url':'http://127.0.0.1:9/v1','api_key':'local-test-secret','model':'fake'}

    def task(self,**kw):
        return ui.QualityTask(ui.validate_quality_config(dict(resources=[self.resource],**kw)))

    def test_creation_freezes_all_questions_without_exposing_answers_or_keys_in_config(self):
        task=self.task()
        self.assertEqual(len(task.config['_suite_cases']),120)
        snap=json.dumps(task.snap(),ensure_ascii=False)
        self.assertNotIn('local-test-secret',snap);self.assertNotIn('_suite_cases',snap)
        self.assertEqual(task.snap()['config']['suite_manifest']['case_count'],120)

    def test_stop_saves_current_case_but_does_not_start_next(self):
        task=self.task(capture_raw=True);calls=[]
        async def request(client,item,payload,*args,**kw):
            calls.append(copy.deepcopy(payload));task.stop_event.set()
            return dict(status_code=200,completed=True,response_text='27',total_ms=5)
        with mock.patch.object(ui,'do_request',side_effect=request),mock.patch.object(ui,'load_suite',side_effect=AssertionError('must use frozen cases')):
            asyncio.run(ui.run_quality_task(task))
        snap=task.snap();self.assertEqual(len(calls),1);self.assertEqual(snap['status'],'stopped')
        self.assertEqual(snap['completed_cases'],1);self.assertEqual(snap['planned_cases'],120)
        self.assertEqual(snap['progress'],1/120);self.assertFalse(snap['report']['evidence']['complete'])
        c=snap['resources'][0]['case_results'][0]
        self.assertEqual(c['reference_answer'],'27');self.assertEqual(c['response'],'27')
        self.assertNotIn('reference_answer',calls[0]);self.assertNotIn('expected',calls[0])
        self.assertEqual(task.config['resources'][0]['api_key'],'')

    def test_stop_before_start_no_invented_result(self):
        task=self.task();task.stop_event.set()
        with mock.patch.object(ui,'do_request',new=mock.AsyncMock()) as request:
            asyncio.run(ui.run_quality_task(task))
        request.assert_not_called();self.assertEqual(task.snap()['completed_cases'],0)
        self.assertEqual(task.status,'stopped')

    def test_capture_off_keeps_reference_but_not_complete_response(self):
        task=self.task()
        async def request(*args,**kw):
            task.stop_event.set()
            return dict(status_code=200,completed=True,response_text='x'*500)
        with mock.patch.object(ui,'do_request',side_effect=request):asyncio.run(ui.run_quality_task(task))
        item=task.snap()['resources'][0]['case_results'][0]
        self.assertEqual(len(item['response_preview']),240);self.assertNotIn('response',item)
        self.assertNotIn('raw_output',item);self.assertTrue(item['reference_answer'])


if __name__=='__main__':unittest.main()
