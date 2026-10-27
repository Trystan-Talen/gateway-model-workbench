"""从逐题证据生成单资源诊断或配对对照；不把完成度、速度、题数当质量。"""
from collections import Counter, defaultdict
import math

import quality_discrimination
from run_timing import request_evidence, restored_timing

CATEGORY_LABELS = {
 'numerical_reasoning':'数值与应用计算','logical_reasoning':'逻辑与约束推理',
 'structured_extraction':'结构化抽取','instruction_following':'指令与格式遵循',
 'reading_comprehension':'阅读与流程理解','grounded_answering':'依据与不确定性',
 'code_reasoning':'代码与查询理解','data_analysis':'数据分析','multilingual':'跨语言理解',
 'long_context':'长上下文检索与整合','robustness':'噪声与边界输入','safety_boundary':'给定安全规则执行',
 'basic_response':'基础响应','math':'数学计算','structured_json':'结构化输出',
 'long_text_consistency':'长文本一致性',
}
REQUIRED_CATEGORIES = tuple(key for key in CATEGORY_LABELS if key not in ('basic_response', 'math', 'structured_json', 'long_text_consistency'))

POLICY = {'version': 2, 'total_score_min': 85, 'category_score_min': 70,
          'min_cases':100, 'request_error_max':0, 'critical_failure_max':0,
          'directional_delta_min':5, 'score_scale':1000,
          'note':'本项目内部筛选阈值，不是行业标准或统计显著性阈值；可用于进入业务复核，不能自动准入。'}
SCENARIOS = [
 ('结构化业务接口', ['structured_extraction','instruction_following','robustness']),
 ('依据资料的知识问答', ['reading_comprehension','grounded_answering','long_context']),
 ('计算与数据辅助', ['numerical_reasoning','logical_reasoning','data_analysis']),
 ('代码与查询辅助', ['code_reasoning','logical_reasoning']),
 ('跨语言文本处理', ['multilingual','instruction_following']),
]

def percentile(values, ratio):
    xs=sorted(float(x) for x in values if isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x))
    return round(xs[max(0,math.ceil(len(xs)*ratio)-1)],2) if xs else None


def _stats(resource, case_count, manifest):
    cases = []
    for item in resource.get('case_results', []):
        score = item.get('score')
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score) or not isinstance(item.get('passed'), bool):
            continue
        case = dict(item, score=max(0, min(1, score)))
        if item.get('completed') is False:
            case.update(score=0, passed=False)
        cases.append(case)
    total=int(resource.get('planned_cases') or case_count or len(cases))
    counts={x['id']:x['count'] for x in manifest.get('categories',[])}
    cats=defaultdict(list)
    for c in cases:cats[c.get('category','unknown')].append(c)
    rows=[]
    for key in dict.fromkeys(list(counts)+list(cats)):
        values=cats[key]; score=round(sum(c['score'] for c in values)/len(values)*100,2) if values else None
        rows.append({'category':key,'label':CATEGORY_LABELS.get(key,key),'score':score,'score_1000':round(score*10,2) if score is not None else None,'completed':len(values),
                     'planned':counts.get(key,len(values)), 'passed':sum(c['passed'] for c in values),
                     'threshold':POLICY['category_score_min'],
                     'failures':[c['case_id'] for c in values if not c['passed']]})
    # 阶段分只按已有证据，各维度等权；未测维度不当零或满分，且明确标为阶段性。
    observed=[r['score'] for r in rows if r['score'] is not None]
    score=round(sum(observed)/len(observed),2) if observed else None
    passed=sum(c['passed'] for c in cases)
    successful=[c for c in cases if c.get('completed') is True]
    errors=max(len(resource.get('request_errors') or []),sum(c.get('completed') is False for c in cases))
    critical=[c['case_id'] for c in cases if c.get('critical') and not c['passed']]
    complete=bool(total) and len(cases)==total and len({c.get('case_id') for c in cases})==total
    unknown_checks=any(r['completed']!=r['planned'] for r in rows)
    complete = complete and not unknown_checks
    coverage=total>=POLICY['min_cases'] and set(CATEGORY_LABELS.keys()).issuperset(cats) and all(counts.get(k,0)>=5 for k in REQUIRED_CATEGORIES)
    weak=[r for r in rows if r['score'] is not None and r['score']<POLICY['category_score_min']]
    gates=[]
    if not complete or unknown_checks:gates.append('题集未完整执行')
    if not coverage:gates.append('题量或维度不足，仅支持冒烟观测')
    if errors:gates.append(f'请求链路错误 {errors} 题')
    if critical:gates.append(f'关键规则未通过 {len(critical)} 题')
    if score is not None and score<POLICY['total_score_min']:gates.append(f'总分低于 {POLICY["total_score_min"]} 分')
    if weak:gates.append('薄弱维度：'+'、'.join(r['label'] for r in weak))
    bad=bool(errors or critical or weak or (score is not None and score<POLICY['total_score_min']))
    level='pending' if not complete else 'risk' if bad else 'caution' if not coverage else 'good'
    title='题集尚未完成' if not complete else '未达到内部筛选条件' if bad else '证据不足，只能冒烟观测' if not coverage else '达到内部筛选条件，待业务复核'
    priority=sorted([dict(c,resource_name=resource.get('name','资源')) for c in cases if not c['passed']],key=lambda c:(not c.get('critical',False),c.get('completed') is not False,c['score'],c['case_id']))
    failure_items=[{'case_id':c['case_id'],'title':c.get('title',c['case_id']),'category':c.get('category'),
                    'reason':'请求链路错误' if c.get('completed') is False else '关键规则未通过' if c.get('critical') else '答案或格式不符合规则',
                    'critical':bool(c.get('critical')), 'failures':c.get('failures',[]), 'resource_name':c['resource_name']} for c in priority]
    scenarios=[]
    by_cat={r['category']:r for r in rows}
    for label,required in SCENARIOS:
        missing=[k for k in required if not by_cat.get(k) or by_cat[k]['completed']<5]
        lows=[k for k in required if by_cat.get(k) and by_cat[k]['score'] is not None and by_cat[k]['score']<POLICY['category_score_min']]
        state='证据不足' if missing or not complete else '不建议' if lows or critical or errors else '可进入业务复核'
        scenarios.append({'name':label,'status':state,'dimensions':[CATEGORY_LABELS[k] for k in required],
                          'reason':'缺少完整维度证据' if missing or not complete else '存在薄弱能力、关键规则或链路问题' if lows or critical or errors else '相关维度达线，但尚未验证真实业务与长期稳定性'})
    constraint_cases=[c for c in cases if c.get('evaluator',{}).get('type')=='verifiable_constraints']
    rule_total=sum(len(c.get('evaluator',{}).get('rules',[])) for c in constraint_cases)
    rule_passed=sum(sum(rule['label'] in c.get('checks',[]) and rule['label'] not in c.get('failures',[]) for rule in c['evaluator']['rules']) for c in constraint_cases if c.get('completed') is True)
    constraints={'cases':len(constraint_cases),'passed_cases':sum(bool(c.get('passed')) for c in constraint_cases),
                 'checks':rule_total,'passed_checks':rule_passed,'score_pct':round(rule_passed/rule_total*100,2) if rule_total else None,
                 'scope':'原创可验证约束的辅助诊断，不计入主分；不是官方指令遵循基准成绩。'}
    return {'name':resource.get('name') or '资源', 'total_cases':total, 'processed_cases':len(resource.get('case_results',[])),
            'completed_cases':len(cases),'successful_responses':len(successful),'failed_cases':errors,'unpassed_cases':len(cases)-passed,'passed_cases':passed,
            'pass_rate_pct':round(passed/len(cases)*100,2) if cases else None,'score_pct':score,
            'conditional_score_pct':round(sum(c['score'] for c in successful)/len(successful)*100,2) if successful else None,
            'request_success_pct':round(len(successful)/len(cases)*100,2) if cases else None,
            'p50_ms':percentile([c.get('total_ms') for c in successful],.5),'p95_ms':percentile([c.get('total_ms') for c in successful],.95),
            'score_1000':round(score*10,2) if score is not None else None,
            'request_errors':errors,'critical_failures':critical,'health_level':level,'health_title':title,
            'constraint_diagnostics':constraints,'categories':rows,'complete':complete,'coverage_sufficient':coverage,'eligible':not gates,'normalized_cases':cases,
            'gate_reasons':gates,'failure_evidence':failure_items,'scenarios':scenarios,
            'scoring_basis':'各已测维度等权；请求失败计零分；未测题不计入阶段分。',
            'error_types':dict(Counter(x.get('error_type','未知') for x in resource.get('request_errors',[])))}


def _failure_kind(case):
    """把一个未通过的题归到唯一一类，用于事实层的「失败构成」计数。

    分类依据只来自评分器已经记录的检查项名和完成标记，不做任何猜测：
    请求没拿到完整响应是链路问题；输出连 JSON 都解析不了是格式问题；
    其余是答案内容或关键规则不符合要求。三类互斥，加总等于失败题数。
    """
    if case.get('completed') is False or case.get('_request_failed'):
        return 'link'
    if 'valid_json' in (case.get('failures') or ()):
        return 'format'
    return 'content'


FAILURE_KINDS = (
    ('link', '请求链路错误'),
    ('format', '输出格式不符合要求'),
    ('content', '答案内容或关键规则未通过'),
)


def _facts_layer(stats, case_count, manifest, status):
    """事实层：只陈述「发生了什么」，不做任何推断或建议。

    单独抽出来是因为此前的报告把原始计数、统计判断和业务建议混在一段文字里，
    读者无法分清哪些是观测结果、哪些是判断。事实层要求每个数字都能在
    逐题证据里逐条核对，且不依赖任何阈值。
    """
    planned = stats.get('total_cases') or case_count or 0
    processed = stats.get('processed_cases') or 0
    kinds = {key: 0 for key, _ in FAILURE_KINDS}
    for case in stats.get('normalized_cases') or []:
        if case.get('passed'):
            continue
        kinds[_failure_kind(case)] += 1
    unpassed = kinds['link'] + kinds['format'] + kinds['content']
    items = [
        {'key': 'planned_cases', 'label': '计划题数', 'value': planned,
         'note': '本次题集在该资源上的总题数。'},
        {'key': 'processed_cases', 'label': '已产生结果题数', 'value': processed,
         'note': '已收到响应并完成评分；小于计划题数说明执行被中断。'},
        {'key': 'passed_cases', 'label': '通过题数', 'value': stats.get('passed_cases') or 0,
         'note': '全部检查项通过且达到该题阈值的题数。',
         'ratio': round((stats.get('passed_cases') or 0) / processed * 100, 2) if processed else None},
        {'key': 'failed_cases', 'label': '未通过题数', 'value': unpassed,
         'note': '至少一项检查未通过的题数；等于下面链路、格式与内容三类之和。'},
        {'key': 'link_failures', 'label': '其中：请求链路错误', 'value': kinds['link'],
         'note': '没有拿到完整成功响应，属于链路或超时问题，不是模型能力结论。'},
        {'key': 'format_failures', 'label': '其中：输出格式不符合要求', 'value': kinds['format'],
         'note': '响应无法按要求解析（最常见的是不是合法 JSON），属于格式遵循问题。'},
        {'key': 'content_failures', 'label': '其中：答案或关键规则未通过', 'value': kinds['content'],
         'note': '响应可解析，但答案内容或可验证约束不符合题目要求。'},
        {'key': 'request_errors', 'label': '请求错误题数', 'value': stats.get('request_errors') or 0,
         'note': '上游返回错误状态、超时或协议异常；这些题在计分中记零分，与上面的链路类可能重叠但不必然相等。'},
    ]
    return {
        'resource': stats.get('name'),
        'status': status,
        'suite_title': manifest.get('title') or '历史题集（未记录标题）',
        'suite_fingerprint': manifest.get('fingerprint'),
        'items': items,
        'headline': f'计划 {planned} 题，已产生结果 {processed} 题，通过 {stats.get("passed_cases") or 0} 题，未通过 {unpassed} 题。',
        'note': '本层只陈述观测计数，不做判断；每个数字都可在逐题证据中逐条核对。分数与结论见下一层。',
    }


def build_quality_report(status, resources, case_count, manifest=None):
    manifest=manifest or {}
    rs=[_stats(r,case_count,manifest) for r in resources or []]
    compared=len(rs)==2
    complete=bool(rs) and all(r['complete'] for r in rs) and status in ('finished','finished_with_errors')
    evidence_sufficient=complete and all(r['coverage_sufficient'] for r in rs)
    report={'version':4,'mode':'comparison' if compared else 'single','resources':rs,'policy':dict(POLICY),
            'facts':[_facts_layer(r, case_count, manifest, status) for r in rs],
            'evidence':{'complete':complete,'suite':manifest,'runs_per_resource':1,
                        'repeatability':'未检验；本次每题每资源仅请求一次',
                        'grade':'覆盖充分、单轮证据' if evidence_sufficient else '证据不足' if complete else '执行中或已中断',
                        'scope':'仅针对本次自建固定题集；不能证明资源来源或真实性，也不能保证长期稳定。'},
            'limitations':list(manifest.get('limitations') or ['历史题集较小，不能用于完整质量筛选。'])}
    report['execution']=dict(manifest.get('execution') or {'id':'legacy_resource_sequential','label':'历史执行策略：按资源顺序执行' if compared else '历史执行策略未单独记录','description':'历史结果不追溯升级为新策略。'})
    report['methodology']=manifest.get('methodology')
    reference_index = manifest.get('reference_index')
    if isinstance(reference_index, int) and 0 <= reference_index < len(rs):
        ref = rs[reference_index]
        report['reference'] = {'index': reference_index, 'name': ref.get('name'), 'score_pct': ref.get('score_pct'), 'score_1000': ref.get('score_1000'), 'source': manifest.get('reference_source'), 'same_claimed_model': bool(manifest.get('same_claimed_model'))}
    pending=status in ('waiting','running')
    title='对照尚未完成' if compared else '单资源评测尚未完成'
    summary='仅展示已评分题的阶段分；等待完整证据后再给出筛选建议。'
    recommendation='等待完成，未执行题不能当作通过。'
    level='neutral';decision='pending'
    if not pending and not complete:
        level='risk';decision='hold';title='评测未完整执行，不能形成完整质量结论'
        summary='保留已评测题证据，但不产生最终准入或资源优劣判断。'
        recommendation='先排查中断原因，再使用同一题集重新执行。'
    elif complete:
        risk=any(r['health_level']=='risk' for r in rs)
        level='risk' if risk else 'caution' if not evidence_sufficient else 'good'
        decision='hold' if risk else 'review'
        if not compared:
            r=rs[0];title=r['health_title']
            summary=f'{r["name"]}质量得分 {r["score_1000"]}/1000（内部仍按百分比阈值判断）；通过 {r["passed_cases"]}/{r["total_cases"]} 题，请求错误 {r["request_errors"]} 题，关键规则失败 {len(r["critical_failures"])} 题。'
            recommendation='可进入对应场景的小范围业务复核；先补充私有业务样本和不同时间重复运行，不能据此自动准入。' if r['eligible'] else '暂不建议直接使用：'+'；'.join(r['gate_reasons'])+'。先复核下方失败题，区分链路、格式与能力问题。'
            report['single']={'strengths':[x['label'] for x in r['categories'] if x['score'] is not None and x['score']>=90 and x['completed']>=5],
                              'weaknesses':[x['label'] for x in r['categories'] if x['score'] is not None and x['score']<70],
                              'scenarios':r['scenarios'],'gate_reasons':r['gate_reasons']}
    if compared:
        left,right=rs
        maps=[{c['case_id']:c for c in r['normalized_cases']} for r in rs]
        keys=sorted(maps[0].keys() & maps[1].keys())
        same_ids=set(maps[0])==set(maps[1]) and len(maps[0])==case_count
        counts={'a_wins':0,'b_wins':0,'ties':0,'both_failed':0}
        gains=[];regressions=[];shared=[];pairs_by_cat=defaultdict(list)
        valid=[];mismatch=[]
        for key in keys:
            a,b=maps[0][key],maps[1][key]
            # 历史记录也必须比较同题；若题目或规则不同，不能纳入配对指标。
            if any(not a.get(k) or not b.get(k) or a.get(k)!=b.get(k) for k in ('prompt','evaluator','category')):
                mismatch.append(key);continue
            delta=b['score']-a['score'];valid.append(key)
            counts['a_wins' if delta<0 else 'b_wins' if delta>0 else 'ties']+=1
            evidence={'case_id':key,'title':a.get('title',key),'category':a.get('category','unknown'),'delta':round(delta*100,2),'a_passed':a.get('passed'),'b_passed':b.get('passed')}
            if delta>0:gains.append(evidence)
            elif delta<0:regressions.append(evidence)
            if not a.get('passed') and not b.get('passed'):counts['both_failed']+=1;shared.append(evidence)
            pairs_by_cat[a.get('category','unknown')].append((a,b))
        categories=[]
        for key,values in pairs_by_cat.items():
            a_score=sum(a['score'] for a,b in values)/len(values)*100;b_score=sum(b['score'] for a,b in values)/len(values)*100
            categories.append({'category':key,'label':CATEGORY_LABELS.get(key,key),'paired':len(values),'a_score':round(a_score,2),'b_score':round(b_score,2),'delta':round(b_score-a_score,2)})
        delta=round(sum(r['delta'] for r in categories)/len(categories),2) if categories else None
        comparable=complete and same_ids and not mismatch
        same_resource=bool(resources[0].get('url') and resources[0].get('model')) and resources[0].get('url')==resources[1].get('url') and resources[0].get('model')==resources[1].get('model')
        report['pairwise']={'left':left['name'],'right':right['name'],'comparable':comparable,'paired_cases':len(valid),
                            'missing_or_mismatched':sorted(set(maps[0])^set(maps[1]))+mismatch,
                            'score_delta_pct':delta, 'pass_rate_delta_pct':round(sum(int(maps[1][k].get('passed',False))-int(maps[0][k].get('passed',False)) for k in valid)/len(valid)*100,2) if valid else None,
                            'request_errors_delta':right['request_errors']-left['request_errors'],
                            'p95_delta_ms':round(right['p95_ms']-left['p95_ms'],2) if left['p95_ms'] is not None and right['p95_ms'] is not None else None,
                            **counts,'categories':categories,'gains':gains,'regressions':regressions,'shared_failures':shared,
                            'same_endpoint_model':same_resource,'selection':'待完成',
                            'repeatability':'未验证稳定性，不能把单轮差异说成稳定优势或把小差异说成等效。',
                            'method':'按相同题目及规则配对；乙减甲。'+report['execution']['label']+'。顺序、缓存和状态变化仍可能影响结果。'}
        ref_idx = manifest.get('reference_index')
        if isinstance(ref_idx, int) and ref_idx in (0, 1):
            tested_idx = 1 - ref_idx
            tested, reference = rs[tested_idx], rs[ref_idx]
            ts, rs_ref = tested.get('score_pct'), reference.get('score_pct')
            retention = round(ts / rs_ref * 100, 2) if isinstance(ts, (int,float)) and isinstance(rs_ref, (int,float)) and rs_ref > 0 else None
            delta_ref = round(ts - rs_ref, 2) if retention is not None else None
            if retention is None: level_ref, label_ref = 'insufficient_evidence', '参考分不足，无法判断兑现程度'
            elif retention >= 95: level_ref, label_ref = 'near_reference', '与参考资源接近'
            elif retention >= 90: level_ref, label_ref = 'minor_mismatch', '与参考资源轻微不一致'
            elif retention >= 75: level_ref, label_ref = 'mismatch', '与参考资源明显不一致'
            else: level_ref, label_ref = 'severe_mismatch', '与参考资源严重不一致'
            report['benchmark_comparison'] = {'tested_index': tested_idx, 'reference_index': ref_idx, 'tested_name': tested.get('name'), 'reference_name': reference.get('name'), 'tested_score_pct': ts, 'tested_score_1000': tested.get('score_1000'), 'reference_score_pct': rs_ref, 'reference_score_1000': reference.get('score_1000'), 'delta_pct': delta_ref, 'delta_1000': round(delta_ref * 10, 2) if delta_ref is not None else None, 'retention_pct': retention, 'risk_level': level_ref, 'risk_label': label_ref, 'same_claimed_model': bool(manifest.get('same_claimed_model'))}
        if complete:
            selection='没有充分依据选出更优资源'
            if not comparable:
                level='risk';title='题目或评分规则不一致，不能对照';recommendation='用相同题集快照重测两侧。'
            elif not evidence_sufficient:
                title='对照证据不足，不能据此选型';recommendation='当前仅为小样本对照；用完整多维题集和重复运行补足证据。'
            else:
                if not left['eligible'] and not right['eligible']:
                    title='两侧均未达筛选条件，不能因为接近就认为合格';selection='两侧均暂不建议';recommendation='优先修复共同失败题和两侧各自的关键问题，再复测。'
                elif left['eligible'] != right['eligible']:
                    selected=left if left['eligible'] else right
                    title=f'{selected["name"]}达到筛选条件，另一侧未达标';selection=f'优先复核 {selected["name"]}';recommendation='只将达标侧作为业务复核候选；不是来源鉴定或长期稳定保证。'
                elif delta is not None and abs(delta)>=POLICY['directional_delta_min']:
                    selected=right if delta>0 else left
                    title=f'{selected["name"]}在本轮有分值优势，稳定性尚未验证';selection=f'倾向 {selected["name"]}，须复核退化题';recommendation='结合优势维度和退化题选择场景；重复运行并交替资源顺序后再确认。'
                else:
                    title='本轮分数接近，尚无充分依据选出更优资源';recommendation='不能把接近视为等效；查看不同场景维度和共同失败，加入业务题并重复运行。'
            summary=f'A：{left["score_1000"]}/1000，B：{right["score_1000"]}/1000；有效配对 {len(valid)} 题，A独胜 {counts["a_wins"]}、B独胜 {counts["b_wins"]}、平分 {counts["ties"]}；两侧共同未通过 {counts["both_failed"]} 题。'
            report['pairwise']['selection']=selection
        report['limitations'].append('同一地址且模型名相同：可能是重复观测，并不能确认是两个独立资源。' if same_resource else '逐题交替先后顺序只减轻固定顺序偏差，不消除缓存和时段影响。' if report['execution']['id']=='paired_alternating_v1' else '资源串行执行，非同时观测；差异可能受时间顺序影响。')
    next_steps=[recommendation]
    if complete:next_steps+=['按下方题号复核完整证据，先处理请求错误和关键规则失败，再处理薄弱维度。','在不同时间重复本题集，并加入私有业务样本；单轮数据不能验证稳定性。']

    # 三层结论：事实（观测计数）→ 统计判断（相对阈值/相对参照）→ 业务建议（要做什么）。
    # 拆开是因为混在一起时，读者会把阈值判断当成观测事实、把建议当成结论。
    statistics_notes = []
    if not complete:
        statistics_notes.append('题集未完整执行，仅能给出阶段分；缺失题不能按通过或不通过计。')
    else:
        statistics_notes.append(
            f'总分 {rs[0]["score_1000"]}/1000 为各已测维度等权平均，尺度与阈值均为本项目内部定义（总分 ≥ {POLICY["total_score_min"]}、各维度 ≥ {POLICY["category_score_min"]}），不是行业标准。')
        if not evidence_sufficient:
            statistics_notes.append(f'题量 {case_count or 0} 少于内部要求的 {POLICY["min_cases"]} 题，或存在维度覆盖不足，因此只能冒烟观测。')
        if compared:
            statistics_notes.append('两侧分差是同一题集内的相对比较；未做重复运行，跨运行波动未估计，小分差不能当作等效或优劣证据。')
        statistics_notes.append('每题每资源只请求一次，未估计运行间波动；下方区分度层的置信区间只刻画本轮配对结果，不代表重跑稳定性。')
    report['layers'] = {
        'facts': {'title': '发生了什么', 'summary': '观测计数，不含判断',
                  'resources': [f['headline'] for f in report['facts']]},
        'statistics': {'title': '这些数字说明什么', 'summary': title,
                       'notes': statistics_notes,
                       'basis': f'判定口径：{report["evidence"]["scope"]}'},
        'advice': {'title': '建议怎么做', 'summary': recommendation, 'steps': next_steps,
                   'boundary': '建议面向业务复核，不构成自动准入、来源鉴定或容量承诺。'},
    }
    report.update(level=level,title=title,summary=summary,
                  decision={'status':decision,'label':title,'scope':report['evidence']['scope'],'recommendation':recommendation,
                            'next_steps':next_steps,'evidence_grade':report['evidence']['grade'],'completed_cases':sum(r['completed_cases'] for r in rs)},
                  health_rule='绝对筛选与相对差异分开判断；高总分不抵消关键规则失败，分数接近不等于合格。')
    quality_discrimination.overlay(report)
    for resource_stats in rs:
        resource_stats.pop('normalized_cases', None)
    return report


def render_quality_markdown(snapshot):
    """导出可直接阅读的结论；逐题原文另由证据文件保留，不补造缺失正文。"""
    report = snapshot.get('report') or {}
    manifest = snapshot.get('config', {}).get('suite_manifest') or {}
    paired = report.get('mode') == 'comparison'

    def cell(value):
        if value is None:
            return '未测或未记录'
        return str(value).replace('\\', '\\\\').replace('|', '\\|').replace('<', '&lt;').replace('>', '&gt;').replace('\n', ' ')

    lines = [f'# {cell(snapshot.get("name", "质量评测"))}', '',
             f'报告类型：{"双资源配对对照" if paired else "单资源能力诊断"}',
             f'任务编号：{cell(snapshot.get("id"))}',
             f'证据状态：{cell(report.get("evidence", {}).get("grade"))}', '',
             '## 第一层 · 事实（观测计数）', '']
    for facts in report.get('facts', []):
        lines += [f'**{cell(facts.get("resource"))}**：{cell(facts.get("headline"))}', '',
                  '| 项目 | 数值 | 说明 |', '| --- | ---: | --- |']
        for item in facts.get('items', []):
            ratio = f'（占已测 {item["ratio"]}%）' if item.get('ratio') is not None else ''
            lines.append(f'| {cell(item["label"])} | {item["value"]}{ratio} | {cell(item["note"])} |')
        lines += ['', cell(facts.get('note')), '']
    lines += ['## 第二层 · 判断（相对内部阈值与参照）', '',
              f'**{cell(report.get("title"))}**', '', cell(report.get('summary')), '']
    for note in (report.get('layers', {}).get('statistics', {}).get('notes') or []):
        lines.append(f'- {cell(note)}')
    lines += ['', cell(report.get('layers', {}).get('statistics', {}).get('basis')), '',
              '## 第三层 · 建议（要做什么）', '',
              cell(report.get('decision', {}).get('recommendation')), '']
    for index, step in enumerate(report.get('layers', {}).get('advice', {}).get('steps') or [], 1):
        lines.append(f'{index}. {cell(step)}')
    lines += ['', cell(report.get('layers', {}).get('advice', {}).get('boundary')), '',
              '## 质量得分', '',
             '| 资源 | 质量分 / 1000 | 已测 / 计划 | 通过题数 | 请求错误 | 关键失败 | 筛选结果 |',
             '| --- | ---: | ---: | ---: | ---: | ---: | --- |']
    for r in report.get('resources', []):
        lines.append(f'| {cell(r["name"])} | {cell(r.get("score_1000", round(r.get("score_pct", 0)*10, 2)))} | {r["completed_cases"]} / {r["total_cases"]} | {r["passed_cases"]} | {r["request_errors"]} | {len(r["critical_failures"])} | {cell(r["health_title"])} |')
    lines += ['', '总分按已测维度等权汇总，展示尺度为1000分；未完成任务仅为阶段分。完整响应失败计零分，完成度和速度不计入质量分。', '']
    if report.get('benchmark_comparison'):
        b = report['benchmark_comparison']
        lines += ['## 官方参考资源对照', '', f'- 被测资源：{cell(b.get("tested_name"))}，{cell(b.get("tested_score_1000"))}/1000；参考资源：{cell(b.get("reference_name"))}，{cell(b.get("reference_score_1000"))}/1000。', f'- 保持率：{cell(b.get("retention_pct"))}%；分差：{cell(b.get("delta_1000"))}/1000；结论：{cell(b.get("risk_label"))}。', '- 该分差是异常信号，不是单凭一次测试证明掺水；必须确认同模型版本、参数、题集、评分器，并结合隐藏题与多轮复测。', '']
    if paired:
        p = report.get('pairwise') or {}
        lines += ['## 配对选择依据', '', cell(p.get('selection')), '',
                  f'有效配对 {p.get("paired_cases", 0)} 题；B（资源乙）减 A（资源甲）质量分差：{cell(p.get("score_delta_pct"))} 分。',
                  f'A（资源甲）独胜 {p.get("a_wins",0)} 题；B（资源乙）独胜 {p.get("b_wins",0)} 题；平分 {p.get("ties",0)} 题；共同未通过 {p.get("both_failed",0)} 题。',
                  cell(p.get('method')), cell(p.get('repeatability')), '',
                  '### 改善、退化与共同失败', '', '| 变化 | 题号 | 题目 | 分差 |', '| --- | --- | --- | ---: |']
        for key, label in [('regressions','乙侧退化'),('gains','乙侧改善'),('shared_failures','共同失败')]:
            for item in p.get(key, []):
                lines.append(f'| {label} | {cell(item["case_id"])} | {cell(item["title"])} | {cell(item["delta"])} |')
        d = report.get('discrimination') or {}
        if d:
            lines += ['', '### 区分度与敏感性', '',
                      f'**结论：{cell(d.get("verdict"))}**（{cell(d.get("band"))}）', '',
                      cell(d.get('basis')), '',
                      '| 配对题 | 同结果 | 不一致 | A 独胜 | B 独胜 | 共同失败 | 分差（百分点） | 95% 置信区间 | McNemar p |',
                      '| ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |',
                      f'| {d.get("pairs")} | {d.get("ties")} | {d.get("discordant")} | {d.get("a_wins")} | {d.get("b_wins")} | {d.get("both_failed")} | {cell(d.get("diff_pct"))} | '
                      f'{cell((d.get("ci95_pct") or [None, None])[0])} ~ {cell((d.get("ci95_pct") or [None, None])[1])} | {cell(d.get("mcnemar_p"))} |', '']
            for note in d.get('notes') or []:
                lines.append(f'- {cell(note)}')
            lines += ['', '解读：一致题不提供优劣信息；只看不一致题的 McNemar 精确检验。分差与置信区间只刻画本轮配对结果，',
                      f'不代表重跑稳定性。这份卷子在本轮的 80% 功效分辨下限约 {cell(d.get("min_detectable_delta_pct"))} 个百分点；',
                      '维度分每档约 '+cell(d.get('score_resolution_pct'))+' 个百分点，要稳定分辨 10 分档每维度约需 '+cell(d.get('cases_needed_for_10pct_dimension'))+' 题。', '',
                      cell(d.get('scope')), '']
    else:
        single = report.get('single') or {}
        lines += ['## 单资源诊断', '', '优势维度：'+('、'.join(single.get('strengths', [])) or '暂无充分证据'),
                  '薄弱维度：'+('、'.join(single.get('weaknesses', [])) or '未发现低于内部阈值的已测维度；未测不等于通过'), '',
                  '### 适用场景与边界', '']
        for scenario in single.get('scenarios', []):
            lines.append(f'- {cell(scenario["name"])}：{cell(scenario["status"])}。{cell(scenario["reason"])}。')
    for r in report.get('resources', []):
        lines += ['', f'## {cell(r["name"])}：维度与复核清单', '',
                  '| 维度 | 得分 | 已测 / 计划 | 通过 |', '| --- | ---: | ---: | ---: |']
        for c in r.get('categories', []):
            lines.append(f'| {cell(c["label"])} | {cell(c["score"])} | {c["completed"]} / {c["planned"]} | {c["passed"]} |')
        lines += ['', '筛选阻断项：'+('；'.join(r.get('gate_reasons', [])) or '无；仍需私有业务复核'), '']
        if not r.get('failure_evidence'):
            lines.append('当前已测题未发现失败；这不是未覆盖能力的通过证明。')
        for f in r.get('failure_evidence', []):
            lines.append(f'- {cell(f["case_id"])} · {cell(f["title"])}：{cell(f["reason"])}；{cell("；".join(f.get("failures", [])))}。')
    lines += ['', '## 可追溯信息与限制', '',
              f'- 题集：{cell(manifest.get("title", "历史小题集"))}；版本 {cell(manifest.get("version", 1))}；每资源 {snapshot.get("case_count", 0)} 题。',
              f'- 题集内容指纹：{cell(manifest.get("fingerprint", "历史未记录"))}。',
              '- 内部参考线：总分至少85分、每维度至少70分、请求错误和关键失败均为零；至少100题且12个指定维度各至少5题。不是行业标准或统计显著性阈值。',
              '- 判定只适用于本次题集。单轮、每题单次，不证明跨时间稳定性、来源身份或资源真伪。',
              '- 原始记录：'+('本次开启；已保存题目可在界面查看或下载完整证据。' if snapshot.get('config', {}).get('capture_raw') else '本次未开启；仅保留输出预览，无法补造完整正文。')]
    lines += ['', '## 执行策略与评分诊断', '- '+cell(report.get('execution',{}).get('label','未记录'))]
    for r in report.get('resources',[]):
        checks=r.get('constraint_diagnostics',{})
        if checks.get('checks'):
            lines.append(f"- {cell(r['name'])}：约束通过 {checks['passed_checks']} / {checks['checks']}，整题通过 {checks['passed_cases']} / {checks['cases']}；辅助诊断，不计入主分。")
    methodology=report.get('methodology') or {}
    if methodology:
        lines += ['', '## 方法依据与来源', '- '+cell(methodology.get('provenance')),
                  '- 方法版本：'+str(methodology.get('version'))+'；资料核对日期：'+cell(methodology.get('reviewed_at'))]
        for source in methodology.get('sources',[]):
            lines.append(f"- {cell(source['name'])}：{source['url']}；采用：{cell(source['adopted'])}；边界：{cell(source['not_covered'])}")
    timing = restored_timing(snapshot)
    def recorded(value):
        return '未记录' if value is None else cell(value)

    lines += ['', '## 时间证据', '',
              '绝对时间统一为新加坡时间 UTC+08:00（协调世界时加八小时），保留毫秒；新记录的耗时使用单调时钟。历史带时区时间按同一时刻转换，缺失或无时区时间显示“未记录”，不按耗时反推。', '',
              '| 任务节点 | 时间或耗时 |', '| --- | --- |']
    for key, label in (('created_at', '创建'), ('started_at', '实际开始'),
                       ('stop_requested_at', '请求停止'), ('dispatch_ended_at', '停止发新请求'),
                       ('ended_at', '结束'), ('interruption_detected_at', '发现意外中断')):
        lines.append(f'| {label} | {recorded(timing[key])} |')
    for key, label in (('queue_ms', '任务等待（毫秒）'), ('elapsed_ms', '运行耗时（毫秒）'),
                       ('drain_ms', '收尾耗时（毫秒）')):
        lines.append(f'| {label} | {recorded(timing[key])} |')
    lines += ['', '### 逐题请求时间', '',
              '| 资源 | 题号 | 执行顺序 | 实际开始 | 结束 | 请求耗时（毫秒） |',
              '| --- | --- | ---: | --- | --- | ---: |']
    rows = []
    for resource in snapshot.get('resources') or []:
        for case in resource.get('case_results') or []:
            sequence = case.get('execution_sequence')
            sequence = sequence if isinstance(sequence, int) and not isinstance(sequence, bool) and sequence > 0 else None
            rows.append((sequence, resource.get('name'), case))
    # 新版按实录执行顺序核查；旧记录没有顺序时保留资源与题目原顺序，不编号补造。
    rows.sort(key=lambda row: (row[0] is None, row[0] or 0))
    for sequence, resource_name, case in rows:
        evidence = request_evidence(case)
        lines.append(f'| {cell(resource_name)} | {cell(case.get("case_id"))} | {recorded(sequence)} | '
                     f'{recorded(evidence["started_at"])} | {recorded(evidence["ended_at"])} | '
                     f'{recorded(evidence["total_ms"])} |')
    if not rows:
        lines += ['', '未记录逐题请求时间证据。']
    lines += ['', '## 限制与复核']
    lines += ['- '+cell(x) for x in report.get('limitations', [])]
    return '\n'.join(lines)+'\n'
