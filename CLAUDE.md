# 智能体工作约定

本项目是本机运行的网关与模型评测工作台（Python 后端 `route_test_ui.py` + 无框架前端 `static/`）。运行、数据与检查命令见 [README](README.md)，通用约定见 [CONTRIBUTING](CONTRIBUTING.md)。

## 前端与界面：必须遵守 Instrument 设计系统

改动任何界面（新增模块、页面、抽屉、弹窗、组件或样式）之前，先读 [docs/design-system.md](docs/design-system.md)，并打开 [组件画廊](docs/design-system/gallery.html) 对照。

1. **只用设计变量**：颜色、字号、圆角、阴影、层级、字体、动效、布局尺寸全部来自 `static/tokens.css`。组件样式里不得写十六进制/`rgb()`/颜色名、字面字号、字面圆角、字面阴影或字面层级。
2. **只拼装已有组件**：新界面用 `static/ui-kit.js`（`window.UI`）生成结构，例如 `UI.pageShell`、`UI.pageHeader`、`UI.tabs`、`UI.section`、`UI.metrics`、`UI.recordItem`、`UI.field`。不要手写一套新的卡片、按钮或标签。
3. **固定骨架**：模块入口在左侧导航栏；记录在中间记录栏（唯一新建按钮 + 分段筛选 + 记录项）；详情用“页头 → 标签 → 进度 → 内容区块 → 操作区”；创建流程用右侧全高抽屉；弹窗复用 `.modal` 结构和 `openModal`。
4. **交互**：点击用 `data-click-action`（组件函数的 `action` 参数）并在 `static/actions.js` 注册；禁止行内事件和行内样式（仅允许动态百分比宽度与自定义属性）。
5. **断点**：视口只用 1100/980/640，详情内部用容器断点 820/560。
6. **缺什么先补系统**：需要新值或新组件时，按 `docs/design-system.md` 第 12 节在同一改动中更新 `tokens.css` → `app.css` → `ui-kit.js` → 设计系统文档 → 组件画廊 → 测试。不要为单个页面加覆盖样式，也不要放宽 `tools/check-ui.cjs` 的白名单来绕过检查。
7. **浅色与深色**都要验证；文案遵循“结论先行、黑盒观测不写成承诺”。

## 提交前检查

```bash
npm test     # 语法、设计系统规则（tools/check-ui.cjs）、组件库与规则门禁测试、前端逻辑
make test    # 以上 + 后端回归
make e2e     # 涉及交互或布局时
```

界面改动需附 1440、980、390 像素宽度的浅色/深色截图说明，并更新 `CHANGELOG.md`。
