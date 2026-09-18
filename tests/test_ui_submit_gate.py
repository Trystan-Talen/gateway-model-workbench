"""禁止「风险确认闸门」的前端逻辑与端点约束脱节。

真实回归（2026-09-11）：质量评测的「资源对照」走 isCompareCreate()，
提交处理里整段 if(!isCompareCreate()) 被跳过，于是勾选框的确认结果
永远不会写进请求体，而服务端 /quality-tests 是带闸门的 —— 用户勾了
也一直被拦。这里把真实的提交处理放进 node 里跑，断言每个端点都按
服务端闸门的口径决定是否携带 risk_acknowledged。
"""
from html.parser import HTMLParser
import json
import os
import shutil
import subprocess
import tempfile
import unittest

import route_test_ui as ui


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == 'script' and dict(attrs).get('src') == '/static/app.js':
            self.scripts.append((ui.CODE_DIR / 'static' / 'app.js').read_text(encoding='utf-8'))
        if tag == 'script' and not dict(attrs).get('src'):
            self.current = []

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self.current is not None:
            self.scripts.append(''.join(self.current))
            self.current = None


class SubmitGateTests(unittest.TestCase):
    """提交处理必须按 endpoint 判断闸门，不能按 createKind 猜测。"""

    SUBMIT = "$('submit').onclick=async()=>{"

    def _script(self):
        parser = _Scripts()
        parser.feed(ui.INDEX_HTML)
        self.assertTrue(parser.scripts, "no inline script served")
        return parser.scripts[0]

    def _body(self, script, marker):
        """截取 marker 起始、花括号配平的函数体（这套脚本多为一行一函数）。"""
        index = script.find(marker)
        assert index != -1, marker
        start = index + len(marker)
        depth = 1
        for offset, char in enumerate(script[start:], start):
            if char == '{':
                depth += 1
            elif char == '}':
                depth -= 1
                if depth == 0:
                    return script[start:offset]
        raise AssertionError(f"unbalanced braces after {marker!r}")

    def _run(self, create_kind):
        script = self._script()
        parts = [
            # 真实实现：读取质量/路由表单 + 提交处理。
            "function readQuality(){" + self._body(script, "function readQuality(){") + "}",
            "function readRoutingSide(mode){" + self._body(script, "function readRoutingSide(mode){") + "}",
            "function currentRoutingModel(){" + self._body(script, "function currentRoutingModel(){") + "}",
            "function readRouting(){" + self._body(script, "function readRouting(){") + "}",
            self.SUBMIT + self._body(script, self.SUBMIT) + "};",
        ]
        resources = [
            {"name": "资源 A", "url": "https://api.example-provider.com/v1",
             "api_key": "sk-synthetic", "model": "probe-model"},
            {"name": "资源 B", "url": "https://api.example-provider.com/v1",
             "api_key": "sk-synthetic", "model": "probe-model"},
        ]
        harness = (
            "const createKind=%s;\n"
            "const __calls=[];const __nodes={};\n"
            "function __el(id){return __nodes[id]||(__nodes[id]={id,textContent:'',className:'',"
            "value:'',hidden:false,disabled:false,type:'',checked:false,innerHTML:'',styles:{},"
            "querySelector:()=>({value:''}),querySelectorAll:()=>[],"
            "closest:()=>({classList:{add(){},remove(){}}}),"
            "setAttribute(){},appendChild(){}});}\n"
            "function $(id){return __el(id)}\n"
            "const QUALITY_ROWS=%s.map(r=>{const kv={'.quality-name':r.name,'.quality-url':r.url,"
            "'.quality-key':r.api_key,'.quality-model':r.model};"
            "return {querySelector:(sel)=>({value:kv[sel]??''})}});\n"
            "const ROUTING_ROWS=Object.fromEntries(['baseline','candidate'].map((side,i)=>{"
            "const kv={'.routing-side-name':side,'.routing-side-url':'http://127.0.0.1:'+(9001+i)+'/v1',"
            "'.routing-side-key':'synthetic-'+side};"
            "return [side,{querySelector:(sel)=>({value:kv[sel]??''})}]}));\n"
            "const document={querySelectorAll:(sel)=>sel==='#qualityResourceFields .quality-resource'?QUALITY_ROWS:[],"
            "querySelector:sel=>sel.includes('data-routing-side')?ROUTING_ROWS[sel.includes('candidate')?'candidate':'baseline']:null,addEventListener(){}};\n"
            "const esc=s=>String(s??'');const toast=()=>{};const nativeConfirm=()=>true;\n"
            "const isQualityCreate=()=>createKind.startsWith('quality-');\n"
            "const isRoutingCreate=()=>createKind==='routing-compare';\n"
            "const isCompareCreate=()=>createKind.endsWith('-compare');\n"
            "const isGatewayCreate=()=>createKind.startsWith('gateway-');\n"
            "let preflightData={requires_confirmation:true,target:{host:'api.example-provider.com',reason:'公网域名'}};\n"
            "let preflightAckRequested=true;\n"
            "$('riskAck').checked=true;\n"
            "for(const [id,value] of Object.entries({routingTier:'1000',routingName:'路由对照',"
            "routingModel:'shared-routing-model',routingConcurrency:'4',routingRpm:'120',"
            "routingConnectTimeout:'9',routingReadTimeout:'45'}))$(id).value=value;\n"
            "$('routingModelManualWrap').hidden=true;\n"
            "const currentModel=()=> 'unrelated-gateway-model';\n"
            "const refreshPreflight=async()=>{};\n"
            "const ensurePreflightAck=async()=>true;\n"
            "const validateRoutingForm=()=>true;\n"
            "const readComparison=()=>({name:'对照',shared_config:{},groups:[]});\n"
            "const readConfig=()=>({});const saveConfig=()=>{};const resetKeyButton=()=>{};\n"
            "const updateKeyInputHint=()=>{};const toggleConfig=()=>{};\n"
            "const selectDefaultModuleAfterCreate=()=>{};const refresh=async()=>{};\n"
            "const setCreateKind=()=>{};\n"
            "const api=async(url,opts)=>{__calls.push({url,body:JSON.parse(opts.body)});"
            "return {quality_test_id:'x',task_id:'y',comparison_id:'z',routing_comparison_id:'r'}};\n"
            + "\n".join(parts) + "\n"
            "$('submit').onclick();\n"
            "setTimeout(()=>{console.log(JSON.stringify(__calls))},60);\n"
        ) % (json.dumps(create_kind), json.dumps(resources))
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as handle:
            handle.write(harness)
            path = handle.name
        try:
            result = subprocess.run([shutil.which('node'), path],
                                    capture_output=True, text=True, timeout=20)
        finally:
            os.unlink(path)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required')
    def test_quality_compare_carries_acknowledgement(self):
        """质量资源对照提交到带闸门的 /quality-tests，必须带上确认。"""
        calls = self._run('quality-compare')
        self.assertEqual(1, len(calls), calls)
        self.assertEqual('/quality-tests', calls[0]['url'])
        self.assertIs(True, calls[0]['body'].get('risk_acknowledged'),
                      "质量对照端点受服务端闸门约束，勾选结果必须随请求发送")

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required')
    def test_quality_single_carries_acknowledgement(self):
        calls = self._run('quality-single')
        self.assertEqual('/quality-tests', calls[0]['url'])
        self.assertIs(True, calls[0]['body'].get('risk_acknowledged'))

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required')
    def test_gateway_compare_skips_ungated_endpoint(self):
        """压测对照走 /comparisons，服务端没有闸门，不应强加确认字段。"""
        calls = self._run('gateway-compare')
        self.assertEqual('/comparisons', calls[0]['url'])
        self.assertNotIn('risk_acknowledged', calls[0]['body'])

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required')
    def test_gateway_single_carries_acknowledgement(self):
        calls = self._run('gateway-single')
        self.assertEqual('/tasks', calls[0]['url'])
        self.assertIs(True, calls[0]['body'].get('risk_acknowledged'))

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required')
    def test_routing_compare_carries_acknowledgement_and_both_sides(self):
        """路由对照有独立端点和两侧配置，不能误走压测对照的免确认分支。"""
        calls = self._run('routing-compare')
        self.assertEqual(1, len(calls), calls)
        self.assertEqual('/routing-comparisons', calls[0]['url'])
        body = calls[0]['body']
        self.assertIs(True, body.get('risk_acknowledged'))
        self.assertEqual('synthetic-baseline', body['baseline']['api_key'])
        self.assertEqual('synthetic-candidate', body['candidate']['api_key'])
        self.assertEqual('shared-routing-model', body['baseline']['model'])
        self.assertEqual(body['baseline']['model'], body['candidate']['model'])
        self.assertEqual(4, body['baseline']['concurrency'])
        self.assertEqual(120, body['candidate']['rpm'])


if __name__ == '__main__':
    unittest.main()
