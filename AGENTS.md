# AGENTS.md

所有智能体（包括 Claude、Codex 等）在本仓库工作时遵循 [CLAUDE.md](CLAUDE.md)。

界面相关改动的唯一规范是 [docs/design-system.md](docs/design-system.md)：只用 `static/tokens.css` 的设计变量，只用 `static/ui-kit.js` 的组件，提交前运行 `npm test`（包含 `tools/check-ui.cjs` 设计系统门禁）。
