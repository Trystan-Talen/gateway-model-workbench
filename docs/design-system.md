# Instrument 设计系统

适用于本项目全部前端：网关压测、路由对照、质量评测、模型指纹，以及今后新增的模块、页面、抽屉和弹窗。

> **给人和智能体的一句话规则**：新界面只用 `static/tokens.css` 里的设计变量，只拼装 `static/ui-kit.js` 里的组件；缺什么先补设计系统（变量 → 组件 → 本文档 → 组件画廊），再写业务页面。`npm run check` 会拒绝偏离规则的改动。

## 0. 信息来源与优先级

| 优先级 | 文件 | 作用 |
|---|---|---|
| 1 | `static/tokens.css` | 唯一的设计变量来源：颜色（浅色/深色）、字号、间距、圆角、阴影、层级、动效、布局尺寸 |
| 2 | 本文档 | 使用规则、组件目录、页面配方、检查清单 |
| 3 | `docs/design-system/gallery.html` | 活的组件画廊，直接调用组件库渲染；用浏览器打开即可对照浅色/深色效果 |
| 4 | `static/ui-kit.js` | 组件结构生成函数（`window.UI`），新代码必须优先使用 |
| 5 | `static/app.css` | 共享组件样式；`static/fingerprint.css` 只放模型指纹特有布局 |
| 强制 | `tools/check-ui.cjs` | 规则门禁，由 `npm run check`、`make test` 和持续集成执行 |

文档与代码冲突时，以变量文件和检查脚本为准，并在同一次改动中修正文档。`界面规范.md` 只是指向本文档的入口。

## 1. 设计原则

1. **证据优先，装饰克制**：界面服务于“判断下一步做什么”。一屏只有一个视觉焦点（结论或核心指标），其余信息分层退后。
2. **一个强调色**：品牌强调色只有信号紫 `--color-accent`，用于选中、进度、焦点和主要数据线。绿、黄、红只表达状态，不做装饰。
3. **墨色主操作**：主要按钮用墨色 `--color-ink`，不是强调色。每个区域最多一个主要按钮。
4. **数字用等宽字体**：指标、计数、编号、百分比使用 `--font-mono` 和等宽数字，单位用小一号的常规字体放在后面。
5. **层级靠间距和表面，不靠边框堆叠**：一级卡片有统一阴影；卡片内部的小块用 `--color-surface-2` 底色，不加阴影。
6. **浅色和深色同等重要**：每个颜色变量都必须同时有两套取值，截图验收两种主题。

## 2. 新增模块或页面的步骤

1. **先读**：本文档第 4、5、6 节，并打开组件画廊。
2. **导航**：在 `static/index.html` 的 `.module-nav` 中按现有格式新增 `.module-nav-item`（`data-module`、`title`、`.module-icon` 图标、`.module-copy` 内 `<b>` 名称和 `<small>` 描述）。图标加入 `static/ui-kit.js` 的 `ICONS`，线性 24×24、线宽由样式统一控制。
3. **记录栏**：顶部用 `.record-panel-head`（标题、副标题、唯一的新建按钮）；分类筛选用 `.workspace-subnav` 分段控件；列表项全部用 `UI.recordItem`，空列表用 `UI.recordEmpty`。
   **默认选中**：进入模块时恢复该模块上次查看的记录；首次进入则选中“第一个有记录的筛选”里最新的一条；切换筛选、删除记录后自动打开该筛选的第一条。详情区只有在当前筛选没有任何记录时才显示空状态，不要出现“请选择一条记录”的中间态，也不要在详情里放“返回记录”按钮。
4. **详情页**：固定配方 `UI.pageShell(UI.pageHeader + [UI.tabs] + [UI.progressCard] + UI.section… + UI.actionBar)`，详见第 7 节。
5. **创建流程**：统一使用右侧全高抽屉 `.config`（结构：`.drawer-head` / `.drawer-body` / `.drawer-footer`）。分步标题用 `UI.formSectionTitle`，字段用 `UI.field` + `UI.input` / `UI.select`，开关用 `UI.switchRow`，提交按钮用 `.submit`。
6. **弹窗**：复制 `static/index.html` 中 `.modal` 结构（`.modal-backdrop`、`.modal-panel`、`.modal-head`、`.modal-body`），通过 `openModal` / `closeModal` 打开关闭，获得焦点循环与 Esc（退出键）关闭。需要确认操作的弹窗在滚动正文之后放 `.modal-footer`（固定底栏），主操作靠右，取消在前；内容变长不应把提交按钮推走。
7. **交互**：所有点击用 `data-click-action`（组件函数的 `action` 参数），并在 `static/actions.js` 的 `UI_ACTIONS` 注册处理函数。禁止行内事件。
8. **样式**：确实需要新组件时，在 `static/app.css` 对应分区添加，只引用变量；把生成函数加入 `static/ui-kit.js`，在本文档第 6 节登记，并在组件画廊加示例。
9. **验证**：运行第 11 节的命令，检查浅色、深色和 1440 / 980 / 390 像素宽度截图。

## 3. 页面骨架

```
┌──────┬──────────────────────────────────────────────────────────┐
│      │ .top  品牌 / 页面名              服务状态  账号  操作      │
│ .side├───────────────┬──────────────────────────────────────────┤
│ bar  │ .record-panel │ .main  （容器名 detail，最大宽 --content-max）│
│ 导航栏│ 标题 + 新建    │   .report-head      页头卡片            │
│      │ 分段筛选       │   .report-tabs      分段标签            │
│ 工具 │ 记录列表       │   .report-section   内容卡片 × N        │
│      │               │   .report-footer-actions 操作区         │
└──────┴───────────────┴──────────────────────────────────────────┘
        .config 右侧抽屉（全高，宽 --drawer-w）   .modal 居中弹窗
```

- 导航栏宽 `--rail-w`，记录栏宽 `--records-w`，顶栏高 `--top-h`。
- 页面级外边距只由 `.main` 控制；详情内部卡片左右边缘必须对齐，禁止负边距，禁止为单个卡片另设最大宽度。
- 同级卡片间距 `--section-gap`，卡片内边距 `--card-inset`。

### 响应式

| 类型 | 断点 | 行为 |
|---|---|---|
| 视口 `@media (max-width: 1100px)` | 1100 | 记录栏变窄，顶栏操作只保留图标（文字保留给读屏） |
| 视口 `@media (max-width: 980px)` | 980 | 导航栏移到顶部横排；登录页隐藏品牌侧栏 |
| 视口 `@media (max-width: 640px)` | 640 | 单列：六模块入口完整一行，诊断/存储/主题进入顶栏；记录栏限高 30dvh（动态视口高度的 30%）并可收起；抽屉满屏；表单单列 |
| 容器 `@container detail (max-width: 820px)` | 820 | 详情内双栏变单栏，页头状态块换到下方 |
| 容器 `@container detail (max-width: 560px)` | 560 | 卡片内边距降为 `--space-4`，标题缩小，报告标签自动换行 |

只允许以上断点；详情内部的栅格依据**容器宽度**变化，不要依据视口宽度。页面、详情区和抽屉不得出现横向滚动；只有表格容器 `.table-wrap` 可以横向滚动。

## 4. 设计变量

### 4.1 颜色（浅色 / 深色）

| 变量 | 浅色 | 深色 | 用途 |
|---|---|---|---|
| `--color-canvas` | #f2f1ec | #0c0c0f | 页面底色 |
| `--color-surface` | #ffffff | #16161b | 一级卡片、抽屉、弹窗 |
| `--color-surface-2` | #f8f7f3 | #1c1c22 | 卡片内的小块、输入框底色、表头 |
| `--color-surface-3` | #efede6 | #232329 | 轨道、计数胶囊、中性标签 |
| `--color-border` | #e4e1d8 | #2a2a31 | 默认描边、分隔线 |
| `--color-border-strong` | #ccc8bc | #3c3c45 | 悬停描边、虚线提示框 |
| `--color-text` | #16161a | #ededf0 | 标题与主要文字 |
| `--color-text-secondary` | #4b4b53 | #b8b8c0 | 正文、表格内容 |
| `--color-muted` | #6e6d75 | #8c8c96 | 说明、元信息、占位 |
| `--color-accent` | #5b47f5 | #9d8cff | 唯一强调色：选中、进度、焦点、主数据线 |
| `--color-accent-hover` | #4632e0 | #b8abff | 强调色悬停 |
| `--color-accent-soft` | #efecff | #221e3b | 选中底色、强调徽章底色 |
| `--color-accent-line` | #cbc2ff | #4a4190 | 选中描边 |
| `--color-accent-2` | #00a6c7 | #3fd0ea | 只用于渐变末端（进度条、标志、环形图） |
| `--color-success` | #11805a | #5fd4a1 | 成功、通过、正常 |
| `--color-success-soft` | #e2f4eb | #14322a | 成功底色 |
| `--color-danger` | #c83340 | #ff8c96 | 失败、风险、删除 |
| `--color-danger-soft` | #fdecec | #3d1f25 | 风险底色 |
| `--color-warning` | #9a6200 | #f2c26b | 需关注、排队、部分失败 |
| `--color-warning-soft` | #fcf1d9 | #3a2f1a | 关注底色 |
| `--color-purple` | #7b4fd6 | #c2a9ff | 保留给数据可视化第二系列，不用于界面控件 |
| `--color-purple-soft` | #f2ecfd | #2c2446 | 同上 |
| `--color-on-accent` | #ffffff | #0c0c0f | 强调色上的文字与图标 |
| `--color-ink` | #141417 | #f1f0ec | 主要按钮、激活标签、编号块（深色主题反转为浅色） |
| `--color-ink-2` | #26262c | #d9d8d3 | 墨色悬停 |
| `--color-on-ink` | #f7f6f2 | #111114 | 墨色上的文字 |
| `--color-on-ink-muted` | #9e9da6 | #55555d | 墨色上的次要文字 |
| `--color-hero` | #141417 | #221e3b | 核心指标大卡（两种主题都保持深色） |
| `--color-hero-line` | #141417 | #4a4190 | 核心指标大卡描边 |
| `--color-on-hero` | #f7f6f2 | #ededf0 | 核心指标大卡文字 |
| `--color-on-hero-muted` | #9e9da6 | #8c8c96 | 核心指标大卡次要文字 |
| `--color-rail` | #111113 | #070709 | 导航栏与登录品牌区（两种主题都为深色） |
| `--color-rail-line` | #26262b | #1d1d22 | 导航栏分隔线 |
| `--color-rail-text` | #f4f3ef | #f1f0ec | 导航栏文字 |
| `--color-rail-muted` | #8f8e97 | #85848d | 导航栏次要文字 |
| `--color-overlay` | #0d0d10 | #000000 | 遮罩与阴影基色（配合 `color-mix` 使用） |
| `--color-chart-grid` | #e7e4dc | #26262d | 图表网格线 |
| `--color-chart-plan` | #b7b0e8 | #4d4787 | 图表“计划值”虚线 |

透明度统一写成 `color-mix(in srgb, var(--color-…) 16%, transparent)`，不要写十六进制色值或 `rgb()`。画布绘图（如趋势图）通过 `getComputedStyle(document.documentElement)` 读取变量，主题切换后重绘。

**状态语义映射**（组件函数的 `tone` 参数）：`neutral` 中性 · `accent` 进行中/强调 · `success` 通过/完成 · `warning` 关注/排队/部分失败 · `danger` 失败/风险。运行状态键 `running`、`finished`、`finished_with_errors`、`waiting`、`stopping`、`stopped`、`error` 已在 `.status` 上映射好颜色。

### 4.2 字体与字号

- `--font-sans`：界面文字（本机系统字体栈，安全策略禁止加载外部字体）。
- `--font-mono`：数字、编号、代码、计数、小号英文眉标。

| 变量 | 值 | 用途 |
|---|---|---|
| `--text-micro` | 10.5px | 胶囊内计数、导航栏文字 |
| `--text-caption` | 11px | 表头、标签、状态胶囊 |
| `--text-meta` | 11.5px | 指标标签与注释、眉标、元信息 |
| `--text-small` | 12px | 字段说明、次要说明 |
| `--text-body` | 12.5px | 默认正文、按钮 |
| `--text-control` | 13px | 列表标题、小节标题 |
| `--text-emphasis` | 13.5px | 输入值、强调正文 |
| `--text-lead` | 14px | 引导段落、提交按钮 |
| `--text-title` | 15px | 卡片标题、顶栏页面名 |
| `--text-section` | 16px | 报告区块标题 |
| `--text-heading-sm` | 18px | 弹窗标题 |
| `--text-heading` | 20px | 抽屉标题、重点值 |
| `--text-heading-lg` | 22px | 记录栏标题、实验组标题、窄容器页面标题 |
| `--text-display-sm` | 24px | 空状态标题、结论标题、标准指标数值 |
| `--text-page` | 28px | 详情页标题 |
| `--text-number-md` | 30px | 窄容器进度数字 |
| `--text-number-lg` | 40px | 进度百分比 |
| `--text-number-xl` | 44px | 评分 |
| `--text-number-2xl` | 52px | 窄容器核心指标 |
| `--text-number-3xl` | 64px | 核心指标（每页最多一个） |
| `--text-hero` | clamp(32px, 3.4vw, 52px) | 仅登录页品牌标语 |

字重：标题 700–760，小标题 600–650，正文 400–550。大标题字距 `-.03em` 到 `-.035em`；大数字 `-.04em` 到 `-.06em`。

### 4.3 间距

`--space-1` 4px · `--space-2` 8px · `--space-3` 12px · `--space-4` 16px · `--space-5` 20px · `--space-6` 24px · `--space-7` 28px · `--space-8` 32px · `--space-10` 40px · `--space-12` 48px。

- 同级卡片之间 `--section-gap`（16，窄容器 12）；卡片内边距 `--card-inset`（24，窄容器 16）。
- 卡片正文中相邻内容块之间 `--content-gap`（16）；包含子区块的正文使用 `--content-group-gap`（24）。由内容容器统一管理，指标、表格、提示等子组件不另加外边距。
- 标签与控件 8；字段之间 18；小节标题上方 28、下方 14。
- 紧凑控件内允许 2px 半步（例如 10px、14px），不得出现 5px、7px 这类零散值。

### 4.4 圆角

| 变量 | 值 | 用途 |
|---|---|---|
| `--radius-hair` | 3px | 指示条、图例色块 |
| `--radius-xs` | 6px | 标签、小徽章、必填标记 |
| `--radius-sm` | 8px | 紧凑按钮、面板内列表行 |
| `--radius-control` | 10px | 输入框、按钮、弹窗关闭按钮 |
| `--radius-inner` | 12px | 卡片内的小块、记录项、表格容器 |
| `--radius-lg` | 14px | 分段控件、嵌套面板、核心指标卡 |
| `--radius-card` | 16px | 一级卡片 |
| `--radius-panel` | 20px | 弹窗、抽屉左侧边缘 |
| `--radius-xl` | 24px | 插画图块 |
| `--radius-pill` | 999px | 胶囊、进度轨道 |

嵌套元素比容器小一级。允许的字面值只有 `0`、`50%`、`inherit` 与 `calc(var(--radius-…) + Npx)`（同心装饰圈）。

### 4.5 阴影、层级与动效

| 变量 | 用途 |
|---|---|
| `--shadow-card` | 所有一级卡片（同级卡片必须一致） |
| `--shadow-float` | 抽屉、弹窗、提示条 |
| `--shadow-control` | 普通按钮、分段控件选中项 |
| `--shadow-selected` | 分段控件/标签的选中态 |
| `--shadow-primary` | 墨色主要按钮 |
| `--shadow-glow` | 强调色图块（标志、激活导航、空状态插画） |
| `--shadow-knob` | 开关滑块 |
| `--shadow-ring` | 输入框聚焦光环 |
| `--halo-size` | 状态圆点的柔光：`0 0 0 var(--halo-size) color-mix(…)` |

嵌套小块不加阴影。唯一允许的字面阴影是 1–3px 的描边或内侧色条（例如 `inset 3px 0 0 var(--color-warning)`）。

层级：`--z-behind` -1 · `--z-raised` 1 · `--z-drawer-backdrop` 60 · `--z-drawer` 61 · `--z-module-drawer-backdrop` 110 · `--z-module-drawer` 111 · `--z-modal` 120 · `--z-module-modal` 130 · `--z-auth` 200 · `--z-toast` 300。

动效：缓动 `--ease`；时长 `--duration-fast`（悬停）、`--duration-base`（淡入、开关）、`--duration-slow`（抽屉）。只做透明度与位移；弹窗入场只用淡入，避免测量时位置跳动。必须尊重 `prefers-reduced-motion`（已全局处理）。

### 4.6 布局尺寸与兼容别名

`--rail-w`、`--records-w`、`--top-h`、`--content-max`、`--drawer-w`、`--card-inset`、`--section-gap`。方法说明列最小宽度 `--method-column-min`（320px），不足时按可用宽度显示单列。在断点内只允许覆盖 `--records-w`、`--top-h`、`--rail-w`。

`--card-shadow`、`--float-shadow`、`--control-shadow`、`--ring`、`--line`、`--text`、`--sub`、`--muted`、`--blue` 等是旧代码的兼容别名，新代码不要使用。

## 5. 视觉层级配方

| 层级 | 做法 |
|---|---|
| 页面 | `--color-canvas` 底色，无卡片 |
| 一级卡片 | `--color-surface` + `--color-border` 描边 + `--radius-card` + `--shadow-card` + 内边距 `--card-inset` |
| 卡片内小块 | `--color-surface-2` + 描边 + `--radius-inner`，无阴影 |
| 核心指标 | `--color-hero` 深色大卡，每页最多一个 |
| 结论 / 风险 | 语义色的 `soft` 底色 + 同色文字；或卡片顶部 4px 语义色条 |
| 操作区 | `.report-footer-actions` 无卡片、右对齐 |

## 6. 组件目录

每个组件列出：组件函数（`window.UI`）→ 生成的类名 → 使用规则。画廊中有全部示例。

### 6.1 基础

- **`UI.esc(text)`**：转义文本。所有外部或上游数据插入结构前必须转义；组件函数已对普通参数转义，名称以 `Html` 结尾的参数接收可信结构。
- **`UI.icon(name)`** → `.ui-icon`：24×24 线性图标，线宽 1.8，装饰图标 `aria-hidden`。常用连接模块使用 `connections`（常用连接）图标。新图标加入 `ICONS`，不要在业务代码里内联 SVG。

### 6.2 操作

- **`UI.button(label, {variant, action, args, iconName, disabled, title})`** → `.btn` / `.btn.primary` / `.btn.danger`。`primary` 每个区域最多一个；危险操作用 `danger` 并在处理函数里 `confirm()` 说明后果。
- **`UI.textButton(label, {action, args})`** → `.text-btn`：次要的内联操作（“手动指定模型”“查看详情”）。
- **`UI.actionBar(buttonsHtml)`** → `.report-section.report-footer-actions`：详情页底部操作区，顺序：进行中操作（停止）→ 下载/导出 → 离线对照 → 删除。持续发请求的长流程可把唯一的停止操作提前到页头 `actionsHtml`（操作结构），避免被指标和曲线推到长页末尾；页头与底部不重复。已结束测试的“再来一次”统一放在页头，不在底部重复。

已结束测试的复测入口统一命名为“再来一次”，放在详情页头右侧，沿用原记录配置创建独立任务。每条详情只显示一个复测按钮；窄屏按既有容器断点将操作与状态折到标题下方。直接复用可读取的已保存密钥；缺失凭据时复用 `.modal` 弹窗与 `UI.passwordInput` 补填，关闭后清空。提交期间显示忙碌状态并阻止重复点击；需要风险确认时仅使用本次用户确认。
- 记录栏新建按钮 `.new-btn`、抽屉提交按钮 `.submit`、表单右侧小按钮 `.icon-btn`、密钥框按钮 `.secret-action` / `.secret-toggle` 由页面骨架提供。

### 6.3 状态与反馈

- **`UI.status(state, label)`** → `.status.<state>`：运行生命周期胶囊，带圆点；`running` 圆点闪烁。
- **`UI.tag(label, tone)`** → `.tag`：表格中的单条结果。
- **`UI.badge(label, {hint})`** → `.evidence-badge`（强调）/ `.table-hint`（中性）：区块标题右侧的证据等级或计数。
- **`UI.notice(text, {tone, title})`** → `.notice` / `.notice.good|caution|risk`：整条提示；`danger` 自动 `role="alert"`。
- **`UI.signal({title, detail, tone})`**、**`UI.signalList(items)`** → `.signal-list` / `.signal`：发现、异常线索列表。
- 页面级：加载 `.loading-state`，空状态 `.empty`（插画 + 标题 + 三步引导），全局提示 `toast(text)`。创建抽屉打开时，操作反馈复用 `UI.hint` 放入 `.drawer-footer` 文档流，已展示的同文错误不重复；禁止浮层提示遮住主操作。旧全局提示在抽屉打开期间隐藏。

按钮在桌面最小高度 38 像素，640 像素以下主要按钮与关闭按钮最小 44 像素；窄屏输入字号使用 16 像素，避免移动浏览器聚焦后自动放大。浅色次要文字在画布和卡片表面保持至少 4.5:1 对比度。

### 6.4 记录栏

- **`UI.recordItem({id, title, state, stateLabel, typeLabel, compare, meta, progress, trailing, selected, action})`** → `.task`：名称单行省略；元信息用“·”分隔；进度 0–100；静态连接记录传 `progress: null`（无进度），不显示进度轨道；`selected`（选中）显示左侧强调条并设置无障碍当前项，悬停名称可读完整标题。
- **`UI.recordEmpty(title, hint)`** → `.task-empty`。
- 分段筛选 `.workspace-subnav` > `.subnav-item`（计数放在 `<em>` 中），汇总行 `.record-summary`。

### 6.5 详情页结构

- **`UI.pageShell(childrenHtml)`** → `.detail-layout.report-shell`：详情页根容器。
- **`UI.pageHeader({kicker, title, description, meta, statusLabel, statusNote, statusTone, actionsHtml})`** → `.report-head`：眉标（模块 · 类型）、标题、一句话说明、元信息胶囊；右侧放结论状态块。`actionsHtml`（操作结构）非空时，以 `.hero-actions` 同时显示操作按钮和状态块，操作排在前面，按钮顶部对齐、不被状态卡拉高。长流程进行中可放唯一的“停发并收尾”；收尾时移除停止操作并明确当前状态。已结束测试在这里放唯一的“再来一次”按钮。
- **`UI.tabs({label, items, active, action, ownerId})`** → `.report-tabs` / `.report-tab`：分段标签，激活项为墨色；方向键、Home、End 已全局支持。第一个标签永远是结论，并且是打开详情时的默认标签：对照类记录（压测对照、资源对照、路由对照、指纹参考对照）命名为“对照结论”，单对象记录命名为“结论”；其后才是各侧/各组/证据标签。
- **`UI.stack(childrenHtml)`（纵向内容组）** → `.report-stack`：统一安排相邻指标、表格、说明、提示与折叠区的间距；自由组合多个内容块时使用。不要靠空行、换行标签或特定组件组合的外边距补间距。
- **`UI.section({title, description, badgeHtml, bodyHtml, className})`** → `.report-section`：内容卡片，标题 + 一句话说明 + 内容。正文自动包含纵向内容组，传入多个组件即可，不必重复包裹。
- **`UI.disclosure({title, bodyHtml, id, open, section})`（折叠区）**：低频口径与执行记录默认收起；独立区块传 `section: true`（独立卡片），区块内使用默认样式。正文自动包含纵向内容组；正文内的子区块用标题与分隔线分组，避免多重卡片边框。稳定的 `id`（标识）用于刷新后恢复展开状态。已结束任务先展示结果与异常，再折叠执行进度；指标卡只留数值、单位和一句口径，完整方法放折叠区。影响当前判断的异常与结论边界必须保持可见。
- **`UI.progressCard({kicker, value, summary, note})`** → `.report-progress-card`：带 `role="progressbar"` 的进度卡；放在区块内时自动去掉卡片外观。

### 6.6 数据展示

- **`UI.metric({label, value, unit, note, primary})`**、**`UI.metrics(items)`** → `.report-metrics` / `.report-metric`：自动换列的指标网格；四项或八项指标在详情中排为四列，容器 820 像素以下两列，560 像素以下单列，避免末行悬空。`primary` 使用深色核心指标样式，每个网格最多一个。空值显示“—”。
- **`UI.methodCards(items)`** → `.method-grid` / `.method-card`：口径、方法、配置说明。正文最多两列，窄容器自动单列；卡片按自身内容高度呈现，不为了最长的一张撑出大块空白。指标数值仍使用指标网格，不能为了节省高度把长说明排成四列。
- **`UI.steps(items)`** → `.next-step-grid` / `.next-step`：编号的下一步动作。
- **`UI.limitations(items)`** → `.report-limitations`：结论边界与不能推出的内容（虚线框）。
- **`UI.formatTimestamp(value, missing)`**：统一将已知时刻转换为新加坡时间（UTC+08:00），输出完整日期、时分秒、毫秒和明确时区；固定 +08:00，不跟随浏览器或系统时区。带旧时区的记录转换实际时刻，不能只替换后缀；无时区的旧值明确标记，不推测本地时区。
- **`UI.formatDurationMs(value)`**：耗时统一保留毫秒，明确区分 0 与未记录。
- **`UI.timeRecords(record, {request, live, key, title})`**：复用高级折叠与方法卡展示任务 / 请求时间轴；紧凑摘要、展开后查看全部时间，`key` 保持轮询后的展开状态；只显示已记录事件，禁止从开始加耗时伪造结束时间。多次真实发送时，明确标注最终尝试并逐次展示时间、耗时和状态；一次发送不重复展示。
- 时间记录使用 `.time-records`（时间折叠区），直接放在详情中不叠加外边距。表格中的 `.request-time-records`（请求时间折叠区）标题保持单行，展开内容保留可读最小宽度，允许表格容器横向滚动，不能把日期挤成竖字。大段原文与多次尝试证据优先放详情弹窗。
- **`UI.table({columns, rowsHtml, empty})`** → `.table-wrap.report-wrap` 内的表格：表头吸顶，数字列等宽；单元格结构需自行转义。窄屏需要卡片化的对照表额外加 `comparison-result-table` 类并由脚本补 `data-label`。这类摘要表在窄屏随页面完整展开，不保留桌面表格的固定高度与内部纵向滚动；大量逐条记录仍使用可滚动表格与分页。
- 其他既有组件：结论大卡 `.report-decision-hero`（`hold` / `review` / `observe` 顶部色条）、事实卡 `.fact-grid` / `.fact-card`、评分卡 `.quality-score-card`、结论横幅 `.conclusion-banner`、单任务核心卡 `.success-card`（配 `.success-ring`）、趋势图 `.chart-wrap`。

### 6.7 表单

- **`UI.formSectionTitle(index, text)`** → `.form-section-title`：编号步骤标题。
- **`UI.field({id, label, controlHtml, hint, required, hintId})`** → `.field`：标签在上、控件、说明在下；必填显示 `.field-required`。
- **`UI.input({...})`** → `.input`；**`UI.select({id, options, value, required})`** → 带统一下拉箭头的 `select.input`。
- **`UI.passwordInput({id, action, args, visibilityLabel, ...inputOptions})`** → `.password-field` / `.password-toggle`：默认遮盖的密码或密钥输入框，眼睛按钮通过委托动作显示/隐藏；按钮带 `aria-controls`（关联控件）、`aria-pressed`（按下状态）和中文操作说明。密钥使用 `visibilityLabel: 'API Key（接口密钥）'`；取回已保存密钥必须由用户主动点击，隐藏或关闭时清空取回值，输入的新值仍可遮盖保留。
- **`UI.inlineFields(fieldsHtml)`** → `.inline`：两列字段，640 以下自动单列。
- **`UI.switchRow({id, title, description, checked})`** → `.adv-toggle` + `.switch`：开关放右侧，说明写清代价（如“会增加存储占用”）。
- **`UI.hint(text, {title})`** → `.comparison-hint`：虚线说明框。
- 其他既有结构：分段选择 `.tabs` / `.tab`、折叠高级项 `details.gateway-single-advanced`、模板卡 `.template-chip`、资源分组 `.quality-resource`、实验组行 `.group-row`、预检卡 `.preflight-card.level-ok|warn|risk`。

常用连接的创建、编辑和管理复用 `.config.fp-drawer` / `.drawer-backdrop.fp-backdrop` 的模块抽屉层级，可叠在测试抽屉上。打开时禁止下层表单交互，关闭时恢复原输入和焦点；已保存密钥只允许主动点击眼睛取回，取回值不随普通编辑再次提交。

网关创建遵循“连接与模型 → 请求内容 → 压力计划／实验组 → 可选记录”的顺序。快速模板只更改明确标注的压力参数，不覆盖独立选择；不同压力模式分别保留草稿。刷新远端选项保留仍有效的选择，不能默选第一项或把手动值静默替换为缺省值；列表不可用时提供明确的恢复入口。隐藏字段不参与当前表单校验。

## 7. 页面配方示例

```js
// 详情页：新模块 renderXxxDetail(run)
host.innerHTML = UI.pageShell([
  UI.pageHeader({ kicker: '缓存评测 · 单次运行', title: run.name, description: '同一清单重复请求，观察缓存命中与首字耗时。',
    meta: [statusLabel(run.status), `${run.total} 条请求`], statusLabel: '运行中', statusNote: '停止后等待已发送请求结束', statusTone: 'accent',
    actionsHtml: UI.button('停发并收尾', { action: 'stopCache', args: [run.id], iconName: 'stop' }) }),
  UI.tabs({ label: '缓存评测详情', items: [['report', '结论'], ['raw', '逐条结果']], active: tab, action: 'setCacheTab', ownerId: run.id }),
  UI.progressCard({ kicker: '执行进度', value: run.pct, summary: `${run.done} / ${run.total} 条` }),
  UI.section({ title: '关键指标', description: '只描述客户端观测窗口。', badgeHtml: UI.badge('有限证据'),
    bodyHtml: UI.metrics([{ label: '命中率', value: '82.4', unit: '%', primary: true }, { label: '首字 p95', value: '640', unit: 'ms' }]) }),
  UI.section({ title: '下一步动作', bodyHtml: UI.steps(['扩大样本后复测', '检查未命中的请求']) }),
  UI.actionBar(UI.button('下载结论报告', { action: 'download', args: [`/cache-report/${run.id}`] }) + UI.button('删除本地记录', { variant: 'danger', action: 'deleteCache', args: [run.id] }))
].join(''));

// 记录栏
list.innerHTML = runs.length
  ? runs.map(r => UI.recordItem({ id: r.id, title: r.name, state: r.status, stateLabel: statusLabel(r.status),
      typeLabel: '缓存评测', meta: [`${r.total} 条`], progress: r.pct, selected: r.id === selected, action: 'selectCache' })).join('')
  : UI.recordEmpty('暂无缓存评测记录', '创建一次缓存评测后，结果会出现在这里。');
```

## 8. 文案与数据展示

- 眉标格式“模块 · 类型”；标题用名词短语；说明一句话讲清“这里回答什么问题”。
- 结论先行：先给判断与适用范围，再给指标，再给方法与边界。黑盒观测不写成事实承诺（例如写“客户端观测完成吞吐”，不写“网关容量”）。
- 数字：千分位由 `num()` 处理；单位紧跟数字并缩小；缺失值统一“—”；差值带正负号，好坏用 `.delta-good` / `.delta-risk`，不要只用颜色表达。
- 专业缩写首次出现附中文解释，例如“TTFT（首字耗时）”。
- 按钮文案用动词开头（“开始路由对照”“下载结论报告”）；危险操作写明后果。

## 9. 可访问性与交互状态

- 每个可交互元素具备悬停、按下、键盘焦点（全局 `:focus-visible` 描边）和禁用（`cursor: not-allowed`，透明度降低）状态。
- 仅图标的按钮必须有 `aria-label`；窄屏隐藏文字时使用视觉隐藏，不用 `display:none`。
- 标签页 `role="tablist"` / `role="tab"` / `aria-selected`；进度条 `role="progressbar"` 带数值；错误提示 `role="alert"` 或 `aria-live`。
- 状态不能只靠颜色：状态胶囊有文字，差值有正负号。
- 文字与背景对比度至少 4.5:1；新增颜色变量需在两种主题下验证。

## 10. 自动检查（`tools/check-ui.cjs`）

| 规则 | 失败时怎么改 |
|---|---|
| 前端文件必须登记 | 新脚本或样式需同时登记到检查脚本、`route_test_ui.py` 资源白名单与 `tests/browser/assets.cjs` |
| 禁止行内事件与动态执行字符串 | 使用 `data-click-action` 并在 `static/actions.js` 注册 |
| 颜色变量浅色、深色成对 | 在 `tokens.css` 两个主题块中都补上 |
| 设计变量必须写进本文档 | 在第 4 节表格中登记 |
| 组件样式不得定义设计变量 | 变量移到 `tokens.css`；主题差异用新的角色变量表达 |
| 禁止字面颜色（十六进制、`rgb()`、颜色名） | 使用 `--color-*` 与 `color-mix()` |
| 字号、字体、圆角、层级、阴影必须用变量 | 选用第 4 节对应角色变量 |
| 断点白名单：视口 1100/980/640，容器 820/560 | 按第 3 节调整，不新增断点 |
| 引用的变量必须存在 | 检查拼写或先在 `tokens.css` 定义 |
| 行内样式只允许动态百分比宽度与自定义属性 | 新增类名 |
| 结构中的类名必须有样式 | 复用已有组件；纯脚本钩子加入检查脚本的钩子名单并说明 |
| 组件库每个函数都有画廊示例和文档说明 | 同步更新画廊与第 6 节 |

规则拦截时请修正组件去符合设计系统，不要放宽白名单；确实需要演进设计系统时，在同一改动里同步更新变量、组件库、本文档、画廊和检查脚本。

## 11. 验收清单

```bash
npm run check          # 语法 + 设计系统规则
npm test               # 前端逻辑测试（含组件库测试）
make test              # 后端回归 + 前端检查
make e2e               # 真实页面冒烟（需要 Playwright）
```

- 用浏览器打开 `docs/design-system/gallery.html`，切换浅色/深色，对照新组件。
- 新页面截图验收：1440、980、390 像素宽度；浅色与深色；空状态、加载、运行中、完成、失败各一次。
- 确认没有横向滚动，同级卡片左右边缘对齐，每页只有一个核心指标、一个主要操作。
- 报告默认态、各折叠区展开态都要检查完整滚动内容；除同一表格行或同排网格单元外，相邻内容块不得贴边或重叠。间距回归应测量实际元素边界，不能仅凭无溢出判定布局合格。
- 更新 `CHANGELOG.md`。

## 12. 演进设计系统

1. 先判断能否用现有变量和组件表达；能则不新增。
2. 需要新值：在 `tokens.css` 增加角色明确的变量（颜色要两套），在第 4 节登记。
3. 需要新组件：`app.css` 加样式 → `ui-kit.js` 加生成函数并导出 → 第 6 节登记 → 画廊加示例 → 组件库测试覆盖。
4. 修改已有组件的外观时，全局生效，不要为某个页面加覆盖规则或“最终修复”样式。
5. 运行第 11 节全部检查，附浅色/深色截图说明变化。
