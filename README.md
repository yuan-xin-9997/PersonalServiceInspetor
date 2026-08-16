# Personal System Inspector

个人系统统一巡检中心，用一个轻量 Web 应用持续检查个人主机、内外网服务、API、Docker、PostgreSQL、MySQL、日志和 Jenkins。系统保存历史状态，连续失败达到阈值后记录事件，并在北京时间每日 09:00 发送 HTML 日报。

## 架构

```text
APScheduler -> InspectionEngine -> TCP / HTTP / TLS / SSH / DB / Jenkins 检查器
                              -> SQLite 历史、状态、事件、任务
                              -> SMTP HTML 日报
FastAPI + Vue 管理页面 ---------^
```

- Python 3.12、FastAPI、Vue 3、APScheduler
- SQLAlchemy + SQLite 持久化，无 Redis、Celery 或监控重型组件
- HTTPX、Paramiko、psycopg、PyMySQL 只读采集
- Python 3.12 独立 virtualenv + systemd 生产运行，Jenkins 版本化发布与失败回滚

## 页面

管理端提供总览、主机、服务和接口、Docker、数据库、日志、Jenkins、30 天历史、任务中心、脱敏配置和权限管理。左下角显示当前用户、角色、Git 提交版本和退出按钮。

公开接口仅有：

- `GET /health/live`
- `GET /health/ready`

登录后可访问：

- `GET /api/summary`
- `GET /api/checks`
- `GET /api/incidents`

`POST /api/run` 需要管理员会话或 `Authorization: Bearer <RUN_API_TOKEN>`。

## 快速开始

```bash
cp .env.example .env
# 编辑 .env，至少配置 APP_SECRET_KEY、ADMIN_INITIAL_PASSWORD、RUN_API_TOKEN
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e .
# 按文档创建 data/password.txt 后启动本地进程
uvicorn app.main:app --host 0.0.0.0 --port 31880 --proxy-headers --env-file .env
curl --fail http://127.0.0.1:31880/health/ready
```

首次启动会以 `ADMIN_INITIAL_PASSWORD` 创建权限为 `0600` 的 `data/password.txt`。密码文件、`.env`、SQLite 和运行日志均被 Git 忽略。已有 `password.txt` 不会被覆盖。

本地开发：

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
ruff check .
pytest -q
```

## 配置说明

- `config/app.json`：端口、时区、SQLite、日志和调度配置。
- `config/checks.yaml`：非敏感检查目标、断言、超时和阈值。
- `.env`：SSH、数据库、Jenkins、SMTP 与应用密钥。
- `data/password.txt`：`username:password:role`，每次启动同步用户；普通用户的页面权限在管理端维护。

所有环境变量名称见 `.env.example`。生产环境变量由 Jenkins Credentials 注入，不应写进配置、镜像、日志、邮件或前端。

## 日报和通知策略

- 周期巡检默认每 15 分钟执行。
- HTML 日报默认北京时间每日 09:00 执行。
- CRITICAL 默认连续失败 3 次才开启事件；同一事件不重复通知。
- WARNING 立即形成可见事件并进入日报，但不发送即时邮件。
- 故障和恢复的即时邮件默认关闭，避免 09:00 以外发送任何报告类邮件；如需启用，可在 `config/app.json` 的 `email.send_incident_notifications` 中显式配置。
- 未配置 SMTP 时，日报写入 `data/reports/`，邮件测试接口返回 `preview` 模式。

管理员可打开 `/api/email/preview` 预览日报，或调用 `POST /api/email/test` 发送测试邮件。

## 添加新系统

在 `config/checks.yaml` 的 `checks` 列表新增唯一 `id`。支持：

- `tcp`：`host`、`port`
- `http`：`url`、`expected_status`、`body_contains`、`json_assertions`、`warning_response_ms`
- `tls`：`host`、`port`
- `host_ssh`、`docker_ssh`、`log_ssh`：使用 `*_env` 指向环境变量
- `postgres`、`mysql`：连接参数全部通过环境变量
- `jenkins`：Jenkins URL 和 API 凭据通过环境变量

JSON 断言支持嵌套路径、等值、包含和时间字段 `max_age_seconds`。生产 `app.json` 属于运维配置，Jenkins 增量部署不会覆盖；`checks.yaml` 属于版本化巡检清单，会随发布更新。

## Jenkins 部署

任务使用 `Pipeline script from SCM`：

- Git：`git@github.com:yuan-xin-9997/PersonalServiceInspetor.git`
- 分支：`*/main`
- Credentials：`truthanalysis-github-ssh`
- Script Path：`src/JenkinsConfig/Jenkinsfile`
- SCM 轮询：`H/30 * * * *`

流水线通过受管 SSH 凭据在目标机创建 Python 3.12 virtualenv，执行 Ruff、pytest、`/opt/personal-system-inspector/releases/<build>-<sha>` 版本化发布、systemd 重启、双健康检查和失败回滚。每个发布版本拥有独立 `.venv`，线上保留当前版本和两个历史版本。详情见 [部署说明](docs/部署说明.md)。

## 运维

```bash
systemctl status personal-system-inspector
journalctl -u personal-system-inspector -n 200 --no-pager
curl http://127.0.0.1:31880/health/ready
```

生产服务由 `deploy/personal-system-inspector.service` 直接托管 Uvicorn。默认不执行自动修复、日志清理、容器重启或数据库写入。

## 常见故障排查

- `UNKNOWN`：检查对应环境变量/只读凭据是否已配置。
- 页面登录失败：检查共享 `data/password.txt` 格式与权限。
- `ready` 返回 503：检查共享 data 目录可写和 SQLite 文件权限。
- Jenkins 无法创建运行环境：确认 Python 3.12、`python3.12-venv`、回环 SSH 凭据和 `sshpass` 可用，并查看 pip 下载错误。
- systemd 启动失败：运行 `journalctl -u personal-system-inspector -n 200 --no-pager`，检查共享目录权限和 `.env`。
- HTTP 200 但业务异常：为该检查增加 JSON、关键字或数据更新时间断言。
- SMTP 未发送：检查 `SMTP_HOST/SMTP_FROM/SMTP_TO`，再调用测试邮件接口；不要把授权码发到日志。

更多内容见 `docs/运维手册.md`、`docs/安全说明.md` 和 `docs/测试报告.md`。
