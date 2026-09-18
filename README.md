# 网关与模型评测工作台

面向网关接入、路由调整和模型资源验收，在本机采集性能与质量证据，帮助判断下一步应排查、复测还是人工复核。

四个同级模块：网关压测、路由对照、质量评测、模型指纹。结论仅覆盖客户端观测；不自动证明供应商身份、内部容量或批准生产放量。

## 环境与启动

使用 Python（运行环境）3.11 或更新版本；前端检查需要 Node.js（脚本运行环境）20 或更新版本。

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.lock
npm ci
make run
```

打开 [本机工作台](http://127.0.0.1:8666/)。安装完成后，也可以在访达里双击 `启动正式版.command`：它使用正式数据目录启动服务，就绪后自动打开浏览器；服务已在运行时可选择直接打开或重启，关闭终端窗口即停止服务。首次运行创建管理员；已有数据继续使用原账号。可用 `ROUTE_TEST_PORT`（监听端口）设置其他端口。默认只监听本机回环地址；部署到服务器见 [服务器部署](docs/deployment.md)。

版本以 `app_version.py` 为准，页面健康接口 `/health` 可查看当前运行版本。

## 数据与凭据

- 新安装使用 `data/`；`ROUTE_TEST_DATA_DIR`（运行数据目录）可指定其他绝对路径。
- 老安装仍可读取项目根目录的数据。停止服务后运行 `.venv/bin/python tools/migrate_data.py`，校验复制成功后自动切换到 `data/`。原文件保留，不覆盖不同内容，不自动恢复未完成请求。
- 历史索引按记录写入 SQLite（嵌入式数据库）；质量评测逐题证据独立保存，旧索引只用于首次导入。
- 密钥通过系统安全存储保存，无法安全保存时明确提示仅本次运行有效。模型指纹密钥始终只在本次运行内存中使用。
- 会话服务端最长 7 天、闲置 12 小时失效；重启可保留有效登录。退出后令牌立即作废。

## 检查命令

```bash
make test       # 编译、后端回归、前端逻辑、界面规则
make coverage   # 核心模块覆盖率门槛
npx playwright install chromium
make e2e        # 真实页面、临时账号、本机模拟接口
make benchmark  # 有限的本机请求引擎基线
```

浏览器测试只使用合成数据；设置 `CHROME_PATH`（浏览器路径）可选择已有浏览器。开发验证不需要真实上游密钥。持续集成在推送与合并请求时运行相同检查。

## 文档

- [本轮逐项核验与处置](docs/项目建议逐项核验与处置.md)
- [服务器部署](docs/deployment.md)
- [架构与数据恢复](docs/architecture.md)
- [指标口径](docs/metrics.md)
- [贡献与检查约定](CONTRIBUTING.md)
- [变更记录](CHANGELOG.md)
- [设计系统](docs/design-system.md)（[组件画廊](docs/design-system/gallery.html)）
- [智能体工作约定](CLAUDE.md)
- [质量评测方法](质量评测方法.md)
- [旧版交接记录](docs/archive/交接文档-2026.09.16.1.md)
