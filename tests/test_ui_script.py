"""Catch page-wide JavaScript parse failures before shipping the embedded HTML."""
from html.parser import HTMLParser
import json
import os
import shutil
import subprocess
import tempfile
import unittest

import route_test_ui as ui


class InlineScripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == 'script' and not dict(attrs).get('src'):
            self.current = []

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self.current is not None:
            self.scripts.append(''.join(self.current))
            self.current = None


class TemplatePickerScriptTests(unittest.TestCase):
    """模板选择器是纯前端过滤，语法检查抓不到「条件写反」这类错误。

    这里把真实的 `pickTemplate` / `renderTemplatePicker` 放进 node 里跑，
    只替换 DOM 与网络：断言阶梯模板确实可选，而不是被过滤掉。
    """

    def _extract(self, name):
        parser = InlineScripts()
        parser.feed(ui.INDEX_HTML)
        for script in parser.scripts:
            index = script.find(f"function {name}(")
            if index != -1:
                end = script.find("\n", index)
                return script[index:end if end != -1 else len(script)]
        self.fail(f"{name} not found in served script")

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required')
    def test_ramp_templates_are_selectable(self):
        source = self._extract("pickTemplate") + "\n" + self._extract("renderTemplatePicker")
        harness = (
            "const COMPARE_TEMPLATE_MODES=['smoke','ramp','soak'];\n"
            "const esc=s=>String(s??'');const num=v=>String(v??'');const duration=s=>String(s??'');\n"
            "let activeTemplateId=null, rendered='';\n"
            "function $(id){return {set innerHTML(v){rendered=v},"
            "querySelectorAll(){return {forEach(){}}}};}\n"
            "const templateCache=[{id:'s',config:{mode:'smoke',rpm:120,iterations:30,concurrency:2}},"
            "{id:'r',config:{mode:'ramp',ramps:'120,240',duration:180,concurrency:4}},"
            "{id:'k',config:{mode:'soak',rpm:240,duration:1800,concurrency:8}},"
            "{id:'x',config:{mode:'nonsense',rpm:1,concurrency:1}}];\n"
            + source + "\n"
            "const picked=['s','r','k','x'].map(id=>pickTemplate(id)?.id??null);\n"
            "renderTemplatePicker(templateCache);\n"
            "const chips=(rendered.match(/data-template=/g)||[]).length;\n"
            "console.log(JSON.stringify({picked,chips,hasRamp:/data-template=\"r\"/.test(rendered)}));\n"
        )
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as handle:
            handle.write(harness)
            path = handle.name
        try:
            result = subprocess.run([shutil.which('node'), path],
                                    capture_output=True, text=True, timeout=20)
        finally:
            os.unlink(path)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(['s', 'r', 'k', None], data['picked'],
                         "阶梯模板必须可选，未知模式必须被过滤")
        self.assertEqual(3, data['chips'])
        self.assertTrue(data['hasRamp'])


class UIScriptTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for JavaScript syntax validation')
    def test_served_inline_scripts_parse(self):
        parser = InlineScripts()
        parser.feed(ui.INDEX_HTML)
        self.assertTrue(parser.scripts, 'No inline application script found')
        for index, script in enumerate(parser.scripts):
            with self.subTest(script=index):
                result = subprocess.run(
                    [shutil.which('node'), '--check'], input=script,
                    capture_output=True, text=True, timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
