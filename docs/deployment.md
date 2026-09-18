# 服务器部署

面向公司内部少量成员（约 10 人）在测试环境服务器上使用。单进程服务，数据存放在本机磁盘（SQLite 与 JSON 文件），不需要额外数据库。

下文以访问地址 `http://103.186.215.40:3003/` 为例，代码放在 `/opt/gateway-workbench`，数据放在 `/srv/gateway-workbench-data`。

## 部署前确认

- **来源限制**：服务器是公网地址时，先在云安全组或防火墙里把 3003 端口限制为公司出口 IP 或 VPN 网段。不要对全网开放。
- **端口空闲**：`ss -ltnp | grep 3003` 无输出。
- **Python 3.11 或更新版本**：`python3 --version`。Ubuntu 22.04 自带 3.10，需要另装 3.11（例如 deadsnakes 源）；Ubuntu 24.04 及以上可以直接用。
- **磁盘**：数据目录预留 10 GB 以上。压测结果文件会持续增长，单个可达数十 MB。

## 首次部署

```bash
# 1. 运行用户与目录
sudo useradd --system --home /srv/gateway-workbench-data --shell /usr/sbin/nologin workbench
sudo install -d -o workbench -g workbench -m 700 /srv/gateway-workbench-data /srv/gateway-workbench-backup

# 2. 代码与运行环境（只装运行依赖，不需要 Node.js）
sudo git clone <仓库地址> /opt/gateway-workbench
cd /opt/gateway-workbench
sudo python3.11 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt

# 3. 配置
sudo cp deploy/gateway-workbench.env.example /etc/gateway-workbench.env
sudo chmod 600 /etc/gateway-workbench.env
sudo vi /etc/gateway-workbench.env       # 核对端口、数据目录等

# 4. 创建管理员（必须在服务器上完成，网页只接受本机初始化）
sudo -u workbench bash -c 'set -a; . /etc/gateway-workbench.env; set +a; cd /opt/gateway-workbench && .venv/bin/python tools/create_admin.py'

# 5. 启动并设为开机自启
sudo cp deploy/gateway-workbench.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gateway-workbench

# 6. 验证
curl -s http://127.0.0.1:3003/health
```

然后在浏览器打开 `http://103.186.215.40:3003/`，用第 4 步创建的管理员登录，在“账号管理”里给成员开账号。

## 配置项

配置写在 `/etc/gateway-workbench.env`，修改后执行 `sudo systemctl restart gateway-workbench` 生效。

| 变量 | 作用 | 直接用 IP:端口访问 | 前面有 Caddy/Nginx |
|---|---|---|---|
| `ROUTE_TEST_HOST` | 监听地址 | `0.0.0.0` | `127.0.0.1` |
| `ROUTE_TEST_PORT` | 监听端口 | `3003` | 内部端口，如 `8666` |
| `ROUTE_TEST_DATA_DIR` | 数据目录（绝对路径） | `/srv/gateway-workbench-data` | 同左 |
| `ROUTE_TEST_TRUSTED_PROXIES` | 可信代理地址，逗号分隔 | **留空** | `127.0.0.1` |
| `ROUTE_TEST_SECURE_COOKIE` | 登录 Cookie 只走 HTTPS | `0` | `1` |
| `ROUTE_TEST_DISABLE_CREDENTIAL_STORE` | 关闭系统安全存储 | `1` | `1` |

需要 HTTPS 时参考 `deploy/Caddyfile.example`。

## 备份

```bash
sudo cp deploy/backup.sh /usr/local/bin/gateway-workbench-backup
sudo crontab -u workbench -e
# 加入一行：每天 3 点备份，保留 14 天
0 3 * * * /usr/local/bin/gateway-workbench-backup /srv/gateway-workbench-data /srv/gateway-workbench-backup 14
```

服务运行中也可以备份：数据库用 SQLite 在线备份导出一致副本。**不要直接复制运行中的 `.sqlite3` 文件。** 恢复时先停服务，再用备份目录替换数据目录，然后启动服务。

建议再把 `/srv/gateway-workbench-backup` 同步到另一台机器或对象存储：测试环境被重装时，本机备份会一起丢失。

## 更新版本

选在没有任务运行时操作。重启会中断正在执行的任务，已采集的证据会保留，但任务不会自动继续。

```bash
cd /opt/gateway-workbench
sudo git pull
sudo .venv/bin/pip install -r requirements.txt
sudo systemctl restart gateway-workbench
curl -s http://127.0.0.1:3003/health     # 核对 version 字段
```

## 日常运维

- **日志**：`journalctl -u gateway-workbench -f`。结构化事件在数据目录的 `events.jsonl`，超过 32 MB 自动轮转。
- **磁盘**：界面里的“存储”可以查看占用并清理无任务引用的结果文件。数据目录占用超过预期时先清理，再考虑扩容。
- **账号**：成员账号由管理员在界面上添加或删除；删除账号会立即作废其登录。

## 使用限制

- **密钥重启后丢失**：服务器没有系统安全存储，网关 API Key 只保存在内存里，服务重启后需要重新填写。
- **同一时间默认只跑一个任务**：其余任务排队，避免多人同时压测互相干扰。管理员可以在设置里调整。
- **所有成员共享记录**：大家看到的是同一批任务；谁创建、删除了什么，记录在审计日志里。
- **压测会产生真实费用**：每个请求都会消耗目标网关的 token。先小规模试跑，参考创建前预检的请求量估算。
- **会访问任意目标地址**：服务会向成员填写的地址发请求，这些地址也可以是服务器所在内网的地址。不要把服务部署在能直接访问生产数据库等敏感网段的机器上。

## 常见问题

| 现象 | 原因与处理 |
|---|---|
| 浏览器打不开 | 检查安全组是否放行 3003、`ROUTE_TEST_HOST` 是否为 `0.0.0.0`、`systemctl status gateway-workbench` |
| 登录成功后立刻又回到登录页 | 用 `http://` 访问时 `ROUTE_TEST_SECURE_COOKIE` 设成了 `1`，改为 `0` |
| 页面提示“首次初始化只能在服务器本机完成” | 按“首次部署”第 4 步在服务器上创建管理员 |
| 提交操作提示“已拒绝跨站状态变更请求” | 反向代理改写了 `Host` 头，需要原样转发 |
| 所有人同时被提示“登录失败次数过多” | 前面有代理但 `ROUTE_TEST_TRUSTED_PROXIES` 没填 |
| 启动报端口被占用 | `ss -ltnp \| grep 3003` 查看占用进程，或换一个端口 |
