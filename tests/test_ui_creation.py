"""真实页面结构与创建状态机回归；不冒充浏览器布局验证。

直接解析实际服务的 HTML，并在 Node 中运行实际创建/切换/读取函数。
节点适配器只实现这些函数使用的状态接口，不计算样式与几何。
"""
from collections import Counter
from html.parser import HTMLParser
import json
import shutil
import subprocess
import unittest

import route_test_ui as ui


class Markup(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.nodes = []
        self.scripts = []
        self.script = None
        self.stack = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == 'script' and dict(attrs).get('src') == '/static/app.js':
            self.scripts.append((ui.CODE_DIR / 'static' / 'app.js').read_text(encoding='utf-8'))
        self.nodes.append({'tag': tag, '_parent': self.stack[-1] if self.stack else None,
                           **dict(attrs)})
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
                       'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            self.stack.append(len(self.nodes) - 1)
        if tag == 'script' and not dict(attrs).get('src'):
            self.script = []

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.nodes[self.stack[index]]['tag'] == tag:
                del self.stack[index:]
                break
        if tag == 'script' and self.script is not None:
            self.scripts.append(''.join(self.script))
            self.script = None


class CreationStructureTests(unittest.TestCase):
    def test_form_ids_are_unique_and_all_creation_hosts_exist(self):
        nodes = Markup(ui.INDEX_HTML).nodes
        counts = Counter(node['id'] for node in nodes if node.get('id'))
        self.assertEqual({}, {key: value for key, value in counts.items() if value > 1})
        for identifier in ['config', 'comparisonFields', 'routingFields', 'routingSidePanels',
                           'routingModel', 'routingConnectTimeout', 'routingReadTimeout',
                           'routingCapture', 'recordCreateButton', 'submit']:
            self.assertEqual(1, counts[identifier], identifier)

    def test_drawer_and_shared_routing_controls_are_labelled(self):
        nodes = Markup(ui.INDEX_HTML).nodes
        by_id = {node['id']: node for node in nodes if node.get('id')}
        drawer = by_id['config']
        self.assertEqual('dialog', drawer.get('role'))
        self.assertIn(drawer.get('aria-labelledby'), by_id)
        self.assertIn('inert', drawer, 'closed drawer must not receive keyboard focus')
        labelled = {node.get('for') for node in nodes if node['tag'] == 'label'}
        for identifier in ['routingModel', 'routingTier', 'routingRpm', 'routingConcurrency',
                           'routingConnectTimeout', 'routingReadTimeout']:
            self.assertIn(identifier, labelled)


@unittest.skipUnless(shutil.which('node'), 'Node.js is required')
class CreationStateTests(unittest.TestCase):
    def function(self, script, name):
        start = script.index('function ' + name + '(')
        brace = script.index('{', start)
        depth = 1
        for index, char in enumerate(script[brace + 1:], brace + 1):
            depth += (char == '{') - (char == '}')
            if depth == 0:
                return script[start:index + 1]
        self.fail('Unbalanced function: ' + name)

    def test_create_modes_and_routing_draft_lifecycle(self):
        markup = Markup(ui.INDEX_HTML)
        script = markup.scripts[0]
        names = ['isGatewayCreate', 'isQualityCreate', 'isRoutingCreate', 'isCompareCreate',
                 'setCreateKind', 'routingTierOptions', 'routingSideTemplate', 'renderRoutingFields',
                 'setRoutingSide', 'updateRoutingSideStatus', 'routingHasSavedKey', 'readRoutingSide', 'readRouting',
                 'connectionBindingNode', 'connectionFieldNodes', 'boundConnectionId', 'boundConnectionProfile',
                 'savedConnectionPickerHtml',
                 'currentRoutingModel', 'resetRoutingModels', 'setRoutingModelMode',
                 'resetPressureDrafts', 'resetCreateForm', 'startNewTest', 'openCreateForContext',
                 'openGatewayCreate', 'openQualityCreate', 'openRoutingCreate']
        implementation = '\n'.join(self.function(script, name) for name in names)
        harness = r'''
const assert=require('node:assert/strict');
require(__UI_KIT__);const UI=globalThis.UI;
const raw=__MARKUP__;
const classes=node=>new Set(String(node.class||'').split(/\s+/).filter(Boolean));
function node(attrs={}){
 const result={...attrs,hidden:Object.hasOwn(attrs,'hidden'),value:attrs.value||'',checked:false,
  type:attrs.type||'text',children:[],options:[],dataset:{},textContent:'',disabled:false,
  setAttribute(key,value){this[key]=value},focus(){},querySelector(){return null},querySelectorAll(){return []},
  closest(selector){for(let item=this;item;item=item.parentElement){if(selector.startsWith('.')&&classes(item).has(selector.slice(1)))return item}return null},
  after(item){
   if(!this.parentElement)return;
   if(item.parentElement){const siblings=item.parentElement.children;siblings.splice(siblings.indexOf(item),1)}
   item.parentElement=this.parentElement;
   const siblings=this.parentElement.children;siblings.splice(siblings.indexOf(this)+1,0,item);
  }};
 for(const [key,value] of Object.entries(attrs))if(key.startsWith('data-'))result.dataset[key.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=value;
 const current=classes(attrs);result.classList={toggle(key,on){if(on)current.add(key);else current.delete(key)},contains:key=>current.has(key)};
 return result;
}
const nodes=raw.map(node),byId=Object.fromEntries(nodes.filter(x=>x.id).map(x=>[x.id,x]));
nodes.forEach(item=>{if(item._parent!==null&&item._parent!==undefined){item.parentElement=nodes[item._parent];item.parentElement.children.push(item)}});
function $(id){return byId[id]||null}
let panels=[];
const tabs=['routingBaselineTab','routingCandidateTab'].map(id=>$(id));
tabs.forEach(tab=>{const status=node();tab.querySelector=selector=>selector==='.routing-side-status'?status:null});
const parseAttrs=text=>Object.fromEntries([...text.matchAll(/([\w-]+)(?:="([^"]*)")?/g)].map(match=>[match[1],match[2]??'']));
const panelHost=$('routingSidePanels');
Object.defineProperty(panelHost,'innerHTML',{set(html){
 panels=[...html.matchAll(/<section\b([^>]*)>([\s\S]*?)<\/section>/g)].map(match=>{
  const panel=node(parseAttrs(match[1]));panel.fields=[...match[2].matchAll(/<input\b([^>]*)>/g)].map(input=>node(parseAttrs(input[1])));
  panel.querySelector=selector=>panel.fields.find(field=>classes(field).has(selector.slice(1)))||null;return panel;
 });panelHost.children=panels;
}});
Object.defineProperty($('routingTier'),'innerHTML',{set(html){this.options=[...html.matchAll(/<option value="([^"]*)"/g)].map(match=>({value:match[1]}));this.value=this.options[0]?.value||''}});
const document={
 querySelectorAll(selector){
  if(selector==='#routingSideTabs .tab')return tabs;
  if(selector==='#routingSidePanels [data-routing-side]')return panels;
  return nodes.filter(item=>selector.split(',').some(part=>classes(item).has(part.trim().slice(1))));
 },
 querySelector(selector){
  const match=selector.match(/^\[data-routing-side="([^"]+)"\]$/);if(match)return panels.find(panel=>panel.dataset.routingSide===match[1])||null;
  const label=selector.match(/^label\[for="([^"]+)"\]$/);return label?nodes.find(item=>item.tag==='label'&&item.for===label[1])||null:null;
 }
};
let routingModelRequestSeq=0;
let routingSavedConnections={};const loadRoutingConnections=()=>{};
let savedConnectionProfiles=[],savedConnectionsLoaded=false;const renderSavedConnectionPickers=()=>{};
let createKind='gateway-single',activeModule='gateway',activeView='single',activeTemplateId=null;
let pressureModeDrafts=Object.create(null),templateApplySeq=0;
let preflightData=null,preflightAckRequested=false,preflightTimer=null,preflightSeq=0,templateCache=null;
let routingCorpus=[{tier:1000,note:'合成测试集'}],lastRendered=null,drawerOpen=false;
const esc=value=>String(value??''),num=value=>String(value??'');
const renderComparisonFields=()=>{lastRendered='gateway-compare'};
const renderQualityFields=compare=>{lastRendered=compare?'quality-compare':'quality-single'};
const renderTemplatePicker=()=>{},resetKeyButton=()=>{},updateKeyInputHint=()=>{},setSavedModelOption=()=>{};
const setMode=()=>{},paintMode=()=>{},applyDynamic=()=>{},syncProfileHint=()=>{},currentModel=()=> 'gateway-only-model';
const updateWorkspaceChrome=()=>{},toggleConfig=open=>{drawerOpen=open},schedulePreflight=()=>{};
__IMPLEMENTATION__

for(const kind of ['gateway-single','gateway-compare','quality-single','quality-compare','routing-compare']){
 setCreateKind(kind,true);
 assert.equal($('comparisonFields').hidden,!['gateway-compare','quality-single','quality-compare'].includes(kind),kind+': shared form visibility');
 assert.equal($('routingFields').hidden,kind!=='routing-compare',kind+': route form visibility');
 if(kind!=='gateway-single'&&kind!=='routing-compare')assert.equal(lastRendered,kind);
}
activeModule='routing';activeView='compare';openCreateForContext();assert.equal(drawerOpen,true);
assert.equal(panels.length,2,'both route forms must be mounted at once');
const fallback=$('routingManualModelToggle').closest('.model-fallback');
assert.ok(fallback,'manual model toggle must have its actual fallback container');
for(const manual of [true,false,true,false]){
 setRoutingModelMode(manual);
 const anchor=$(manual?'routingModelManualWrap':'routingModelHint');
 assert.equal(anchor.parentElement.children[anchor.parentElement.children.indexOf(anchor)+1],fallback,'model mode switch must move the toggle after the active input section');
 assert.equal(document.querySelector('label[for="routingModel"]').hidden,manual,'list label follows the active input mode');
 assert.equal($('routingModelRow').hidden,manual);
}
for(const [index,panel] of panels.entries()){
 panel.querySelector('.routing-side-url').value='http://127.0.0.1:'+(9001+index)+'/v1';
 panel.querySelector('.routing-side-key').value='synthetic-'+panel.dataset.routingSide;
}
$('routingModel').value='shared-model';$('routingConcurrency').value='4';$('routingRpm').value='120';
$('routingConnectTimeout').value='9';$('routingReadTimeout').value='45';$('routingCapture').checked=true;
const initial=readRouting();
for(const side of ['candidate','baseline','candidate']){
 setRoutingSide(side);assert.equal(panels.find(panel=>!panel.hidden).dataset.routingSide,side);
 assert.deepEqual(readRouting(),initial,'switching side must not replace entered controls');
}
assert.equal(initial.baseline.model,'shared-model');assert.equal(initial.candidate.model,'shared-model');
assert.equal(initial.baseline.api_key,'synthetic-baseline');assert.equal(initial.candidate.api_key,'synthetic-candidate');
panels[0].dataset.connectionId='synthetic-profile-a';panels[1].dataset.connectionId='synthetic-profile-b';
assert.equal(readRouting().baseline.connection_id,'synthetic-profile-a');assert.equal(readRouting().candidate.connection_id,'synthetic-profile-b');
delete panels[0].dataset.connectionId;delete panels[1].dataset.connectionId;
for(const key of ['model','concurrency','rpm','connect_timeout','read_timeout','capture_raw'])assert.equal(initial.baseline[key],initial.candidate[key]);
renderRoutingFields();assert.deepEqual(readRouting(),initial,'corpus refresh must retain both sides');
toggleConfig(false);openCreateForContext();assert.equal(drawerOpen,true);
assert.equal(readRouting().baseline.api_key,'');assert.equal(readRouting().candidate.api_key,'');
assert.equal($('routingCapture').checked,false);assert.equal($('routingModel').value,'');
for(const [module,view,kind] of [['gateway','single','gateway-single'],['gateway','compare','gateway-compare'],['quality','single','quality-single'],['quality','compare','quality-compare']]){
 activeModule=module;activeView=view;openCreateForContext();assert.equal(createKind,kind);assert.equal(drawerOpen,true);
}
console.log(JSON.stringify({modes:5,contextEntries:5,routeSwitches:3,corpusRefresh:true,credentialsReset:true}));
'''.replace('__MARKUP__', json.dumps(markup.nodes)).replace('__IMPLEMENTATION__', implementation).replace('__UI_KIT__', json.dumps(str(ui.CODE_DIR / 'static' / 'ui-kit.js')))
        result = subprocess.run([shutil.which('node')], input=harness,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(5, json.loads(result.stdout)['modes'])


if __name__ == '__main__':
    unittest.main()
