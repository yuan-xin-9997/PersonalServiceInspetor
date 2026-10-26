# Personal System Inspector

个人系统统一巡检中心，用一个轻量 Web 应用持续检查个人主机、内外网服务、API、Docker、PostgreSQL、MySQL、日志和 Jenkins。系统保存历史状态，连续失败达到阈值后记录事件，并在北京时间每日 09:00 发送 HTML 日报。

1.1 版本新增“巡检管理”页面：巡检项可以在线启用/停用，巡检间隔、每日时间点、时间窗口、日报时间和阈值参数均可在页面修改并即时生效；首页升级为仪表盘，用环形图、趋势图、分类健康度和状态卡片展示各项巡检的最新情况。

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

- **仪表盘**：总体状态、状态分布环形图、7/14/30 天巡检趋势、分类健康度、每个巡检项的最新状态、当前异常和最近恢复。
- **巡检管理**：巡检计划、巡检参数、巡检项开关。主机、服务和接口、Docker、数据库、日志、Jenkins 和定时任务作为子页面统一归入这一菜单。
- **历史记录**：30 天状态趋势和每日明细。
- **任务中心**：调度计划和执行历史。
- **系统配置**：脱敏后的 `app.json`、`checks.yaml` 默认值及数据库中的运行时配置。
- **权限管理**：普通用户可访问页面的勾选维护。

左下角显示当前用户、角色、版本号和退出按钮。

公开接口仅有：

- `GET /health/live`
- `GET /health/ready`

登录后可访问：

- `GET /api/summary`
- `GET /api/checks`
- `GET /api/incidents`
- `GET /api/inspection`
- `GET /api/tasks`
- `GET /api/history`

管理员接口：

- `POST /api/run`（也可使用 `Authorization: Bearer <RUN_API_TOKEN>`）
- `PUT /api/inspection/checks/{check_id}`：启用/停用单个巡检项
- `PUT /api/inspection/checks`：按 `ids` 批量启用/停用
- `PUT /api/inspection/schedule`：修改巡检模式、间隔、时间点、时间窗口和日报时间
- `PUT /api/inspection/limits`：修改超时、失败阈值、TLS/磁盘/Inode 阈值和日志读取上限

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

- `config/app.json`：端口、时区、SQLite、日志和调度默认值。
- `config/checks.yaml`：非敏感检查目标、断言、超时和阈值默认值。
- `.env`：SSH、数据库、Jenkins、SMTP 与应用密钥。
- `data/password.txt`：`username:password:role`，每次启动同步用户；普通用户的页面权限在管理端维护。
- SQLite `inspection_settings` / `check_settings`：管理员在“巡检管理”页面保存的调度、阈值和开关。它们不会随代码发布被覆盖。

所有环境变量名称见 `.env.example`。生产环境变量由 Jenkins Credentials 注入，不应写进配置、镜像、日志、邮件或前端。

### 巡检管理配置

- 调度模式支持“固定间隔”（如每 15 分钟）和“每日定时”（如 08:00、12:00、18:00）。
- 可以设置巡检时间窗口，支持跨天（如 22:00 - 06:00）；窗口外的触发自动跳过。
- 可以单独启用/停用任一巡检项。停用后保留历史数据，不再执行检查，不计入总体状态和异常统计；当前未恢复事件会被标记为 `CLOSED`。
- 巡检参数作为 `checks.yaml` 中未显式配置时的默认值，保存后下一次执行生效。
- 修改调度会重新加载 APScheduler 任务，无需重启 Web 进程。

## 日报和通知策略

- 周期巡检默认每 15 分钟执行，可在“巡检管理”中改为固定间隔或每日定时。
- HTML 日报默认北京时间每日 09:00 执行，可在“巡检管理”中修改时间或关闭。
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

JSON 断言支持嵌套路径、等值、包含和时间字段 `max_age_seconds`。生产 `app.json` 属于运维配置，Jenkins 增量部署不会覆盖；`checks.yaml` 属于版本化巡检清单，会随发布更新。新增项默认启用；如果希望发布时保持停用，可在“巡检管理”页面关闭，开关保存在 SQLite 中。

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
