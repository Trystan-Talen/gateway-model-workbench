"""Generate synthetic UI fixtures; never reads user data or calls an upstream."""
import json, time, sys, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
out=Path(sys.argv[1]).resolve();out.mkdir(parents=True,exist_ok=True)
os.environ['ROUTE_TEST_DATA_DIR']=str(out/'data')
os.environ['ROUTE_TEST_DISABLE_CREDENTIAL_STORE']='1'
import route_test_ui as ui
cfg = ui.validate_config({'url':'https://example.test/v1','api_key':'fixture-not-a-real-key','model':'model-fixture','mode':'smoke','name':'单次压测 · 界面验收','concurrency':40})
steps, total = ui.build_plan(cfg); t = ui.Task(cfg, steps, total); t.id='single-fixture';t.status='finished';t.started_at=time.time()-20;t.finished_at=time.time();t.done=t.total;t.scheduled=t.total;t.started=t.total;t.counters['ok']=t.total;t.ttft.extend([110,150,180]);t.total_ms.extend([450,500,520]);t.recent.append({'id':1,'step':1,'status':200,'cls':'ok','label':'成功','ttft':110,'total':450,'reason':''});t.timeline.extend([{'rpm':30,'target_rpm':60},{'rpm':60,'target_rpm':60}])
s=t.snap()
ccfg=ui.validate_comparison_config({'name':'并发变化对照 · 界面验收','shared_config':cfg,'groups':[{'id':'base','name':'40 并发基准','role':'baseline','concurrency':40,'rpm':120,'iterations':10},{'id':'stress','name':'100 并发对比','role':'comparison','concurrency':100,'rpm':120,'iterations':10}]})
c=ui.ComparisonRun(ccfg);c.id='compare-fixture';c.status='finished';c.started_at=time.time()-50;c.finished_at=time.time();c.group_results=[dict(g,status='finished',summary=dict(s,name=g['name'],id=g['id'],done=10,total=10)) for g in ccfg['groups']]
resource = {'name':'资源 A','url':'https://example.test/v1','api_key':'fixture-not-a-real-key','model':'model-fixture'}
quality = ui.QualityTask(ui.validate_quality_config({'name':'质量评测 · 界面验收', 'resources':[resource, dict(resource, name='资源 B')]}))
quality.id='quality-fixture';quality.status='finished';quality.results=[{
    'name':name,'request_errors':[], 'case_results':[
        {'case_id':'basic_ping','completed':True,'passed':True,'score':1,'total_ms':100},
        {'case_id':'math_arithmetic','completed':True,'passed':True,'score':1,'total_ms':120}],
    'summary':{'total_cases':2,'passed_cases':2,'score':1,'percentage':100,'pass_rate':1}
} for name in ['资源 A','资源 B']]
fixture={'tasks':[s]+[dict(s,id=f'single-{n}',name=f'界面验收 · 历史记录 {n}') for n in range(20)],'comparisons':[c.snap()],'quality_tests':[quality.snap()]}
(out/'fixture.json').write_text(json.dumps(fixture,ensure_ascii=False))
(out/'index.html').write_text(ui.INDEX_HTML)
