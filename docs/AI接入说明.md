# AI 接入与运维

本系统开放真实巡检业务，复用 InspectionEngine、SettingsStore 与当前用户权限；没有外部服务重启、远程命令、删除、批量修改或发布工具。

## 入口

- 原网页保持 `http://192.168.0.111:31880/`；推荐使用内网 HTTPS `https://192.168.0.111:31881/`。
- 网页登录后：系统配置 → AI 接入；直接入口 `https://192.168.0.111:31881/#ai`。普通用户须管理员在权限管理中授予 `ai` 页面，以及相应业务页面。

生产启用 `AI_REQUIRE_HTTPS=true`：原 HTTP 登录和业务页面保留，AI 导航自动转到 HTTPS；HTTP 不能创建/列出/撤销凭据、调用机器 API/MCP 或换取 OAuth 令牌，防止明文传输完整密钥和令牌。开发隔离环境可显式关闭。
- API：`https://192.168.0.111:31881/api/v1`。
- OpenAPI：`https://192.168.0.111:31881/openapi.json`；Swagger：同入口 `/docs`。
- MCP Streamable HTTP：`https://192.168.0.111:31881/mcp`。
- OAuth 元数据：`/.well-known/oauth-authorization-server`；资源元数据：`/.well-known/oauth-protected-resource/mcp`。

所有部署地址来自 `AI_PUBLIC_BASE_URL` 或 `app.ai_public_base_url`；旧 `app.public_base_url` 保留。没有增加公网 DNS、端口映射或 Cloudflare 发布。

## HTTPS 和凭据

内部 CA 位于服务器 `shared/tls/ca.crt`，通过受保护 SSH 获取并核对指纹。客户端使用 `INSPECTOR_CA_FILE`，浏览器由操作者按组织策略信任此 CA；不要关闭证书校验。CA 私钥仅部署用户可读，服务只读取 leaf 私钥。证书有效期一年，应在到期前续签并重启。初始化脚本 `deploy/init-ai-tls.sh` 必须显式提供 DEPLOY_ROOT、AI_TLS_HOST、AI_TLS_PORT，且不会替换已有私钥。内置 TLS 监听与 HTTP 共用同一应用实例、引擎和任务队列，不会重复启动调度器。

凭据页面只能管理自己：填写名称、1–366 天有效期和当前可授权权限；默认仅 status:read。创建响应仅一次包含 secret，立即复制到受保护的凭据存储。关闭后只显示名称、随机 ID、非敏感前缀、权限、创建/过期时间和状态。自动刷新不清除正在显示的密钥，导航被阻止直到用户关闭。撤销需要确认影响并立即使 API Key、access_token、refresh_token 全部失效；遗失密钥应撤销后重建。

密钥和令牌仅短暂存于前端内存，不进入 localStorage、sessionStorage、URL、前端日志或下载文件。敏感响应 `Cache-Control: no-store`，验证错误不会回显请求值。网页登录使用 Strict SameSite 会话，凭据写接口还校验 CSRF Token；Origin 校验覆盖所有写请求。HTTP 保留给历史网页登录，生产自动化推荐只走 HTTPS。

## 权限

| scope | 所属页面 | 行为 |
|---|---|---|
| status:read | dashboard | 总体状态、异常事件 |
| checks:read | inspection | 全部巡检项、任务逐项结果 |
| history:read | history | 最近 1–90 天趋势 |
| tasks:read | tasks | 自己提交的 AI 任务、结果及失败原因 |
| inspection:run | inspection + 管理员 | 异步巡检，另需 tasks:read |
| checks:write | inspection + 管理员 | 单项开关，必须 confirm=true |

每次调用以“凭据签发范围 ∩ 所属用户当前页面/角色权限”为准，删除 password.txt 中用户即停用。降级后的普通用户不会继承 `*`。普通用户无法伪造创建管理员 scope、管理别人的凭据、读别人的 AI 任务。API Key 不支持指定其他 username。旧会话角色也实时校验。原 `/api/run` 的环境 Token 如需继续使用，必须额外设置 `RUN_API_USERNAME` 绑定当前有效管理员；建议迁移到个人凭据。

## API 和工具

| API | MCP tool | 权限 |
|---|---|---|
| GET /api/v1/summary | get_summary | status:read |
| GET /api/v1/checks | list_checks | checks:read |
| GET /api/v1/incidents | list_incidents | status:read |
| GET /api/v1/history?days=30 | get_history | history:read |
| GET /api/v1/tasks | list_tasks | tasks:read |
| GET /api/v1/tasks/{id} | get_task | tasks:read |
| GET /api/v1/tasks/{id}/results | get_task_results | tasks:read + checks:read |
| POST /api/v1/tasks，JSON {} | run_inspection | inspection:run + tasks:read |
| PUT /api/v1/checks/{id} | set_check_enabled | checks:write + confirm |

分页采用 offset≥0、limit=1–100，响应 `{items,total,offset,limit}`。任务创建返回 HTTP 202 `{id,status_url}`；查询状态有 QUEUED、RUNNING、SUCCESS、SKIPPED、FAILED，结果含 run_id/计数，逐项结果另行分页。引擎不提供中间百分比时 progress=null，终态为 100。原任务中心仍显示巡检引擎执行记录；AI 任务是额外的按用户隔离的持久化排队记录。

任务提交和开关写入必须携带 `Idempotency-Key`（1–128 字符）；MCP 使用 idempotency_key。相同用户、操作和键的重试返回同一结果；改变请求正文返回 409。SQLite BEGIN IMMEDIATE 使幂等记录与单项开关/异常关闭、任务 ID 预留同事务提交，支持并发重复请求。幂等记录当前不自动过期，请为新业务操作使用新键。

排队任务重启后继续执行；正在运行的任务如中断，保留 FAILED 与原因，不自动重放。已有巡检正运行时新执行可能 SKIPPED，可查询结果并使用新键重试。巡检只读目标服务，但可能产生现有通知配置指定的邮件，故没有开放普通用户执行权限。

错误格式 `{error:{code,message}}`，HTTP 401/403/404/409/422/429/500；为历史接口兼容可同时返回 detail。OAuth 使用标准 OAuth 错误格式，MCP 工具失败返回协议 isError。匿名、空、错误、过期、撤销凭据均拒绝；密钥为空不放行。每个来源 IP 以及凭据摘要默认每分钟 120 次，配置 `security.ai_rate_limit_per_minute`，429 返回 Retry-After。当前运行模式为单服务进程，不支持多实例任务消费者；如扩容需先引入分布式任务认领和共享限流。

公开入口仅健康检查、登录页/登录提交、静态资源、API schema/Swagger、OAuth discovery/register/authorize/token/revoke 的协议入口。公开协议入口不提供业务数据，授权/换令牌均需有效授权。其余业务需会话或独立机器凭据；浏览器 Cookie 不能代替机器 Bearer。

### 请求、响应示例

```bash
# INSPECTOR_API_KEY 由环境或凭据管理器注入，禁止提交实际值。
export INSPECTOR_URL='https://192.168.0.111:31881'
export INSPECTOR_CA_FILE='/protected/path/ca.crt'
curl --cacert "$INSPECTOR_CA_FILE" -H "Authorization: Bearer $INSPECTOR_API_KEY" "$INSPECTOR_URL/api/v1/checks?offset=0&limit=20"
curl --cacert "$INSPECTOR_CA_FILE" -H "Authorization: Bearer $INSPECTOR_API_KEY" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: unique-operation-id' \
  -d '{}' "$INSPECTOR_URL/api/v1/tasks"
```

任务响应示例：`{"id":"example-task-id","status_url":"/api/v1/tasks/example-task-id"}`。
开关正文：`{"enabled":true,"confirm":true}`。停用会关闭当前未恢复事件，调用前必须取得操作者对影响的明确确认。

## MCP 鉴权和兼容性

官方 Python SDK 固定 `mcp==2.3.0`，支持当前规范以及 SDK 协商的旧协议。MCP 服务接受目标资源绑定为本端点的短期令牌，拒绝普通 API Key 直接访问 MCP。

1. **无人值守程序（已实测）**：`examples/ai_client.py` 从 INSPECTOR_URL、INSPECTOR_API_KEY、INSPECTOR_CA_FILE 读取配置，自动以 API Key 调用 POST /api/v1/token 获得最长 15 分钟令牌，到期前自动换取。完整密钥与短期令牌不会打印。以官方 ClientSession/Streamable HTTP 初始化、发现工具和调用，无需浏览器 Cookie。
2. **交互 OAuth 客户端（协议适配）**：RFC9728 资源发现、RFC8414 服务发现、公共客户端动态注册（auth method none）、authorization_code + S256 PKCE、resource 校验、会话登录和用户勾选 consent、refresh_token 轮换及撤销。授权码两分钟且一次使用，refresh_token 最多 30 天，二者不超过所属凭据有效期。OAuth 授权也出现在网页凭据列表，可随时撤销。机密客户端、Client ID Metadata Documents 和企业 ID-JAG 暂不支持；动态注册为当前规范允许的兼容方式。
3. **验证边界**：官方 Python SDK 真实客户端已验证 initialize、tools/list、查询及保持原状态的开关写入；OAuth PKCE/轮换/撤销通过真实 SDK 路由的端到端测试。没有宣称 Claude、Cursor、VS Code、ChatGPT 云端连接器已逐一验证；具备内网可达性、内部 CA 信任及公共 PKCE/DCR 的客户端仅在协议上适配。要求公网受信 HTTPS、无法访问内网或不支持所述 OAuth 流程的客户端目前不支持。

只读工具声明 readOnlyHint=true、destructiveHint=false、idempotentHint=true；执行巡检声明非只读、有外部访问、按幂等键幂等；开关工具声明 destructiveHint=true，因停用会关闭异常。协议属性是提示，服务端仍严格校验 scope 和 confirm。

## 审计、迁移与回滚

SQLite 增加 ai_credentials、ai_secrets、ai_clients、ai_jobs、ai_requests、audit_events 六张表，启动时幂等建表；原表、配置、历史均不重建。secret 只存 SHA-256 摘要，随机密钥至少 256 位；客户端注册只支持不含秘密的公共客户端。审计记录身份、操作、目标、结果、UTC 时间（页面显示北京时间），不记录请求正文、密码、密钥或令牌。生产关闭 Uvicorn access log，应用日志不打印令牌；只保留安全的业务审计。MCP 写入身份由共享业务服务记录。

Jenkins 在切换前安装并验证独立 virtualenv、运行 Ruff/pytest、语法检查、在线 SQLite backup 和受保护的配置/账号快照，等待已有运行任务结束后切换并重启。预计切换影响数秒到数十秒；若任务十分钟仍未结束则放弃切换。原有共享 app.json 永不覆盖。TLS 环境变量与证书预先准备，切换后验证实际内网 HTTPS、业务就绪、OAuth resource 和 S256 元数据。

备份位置 `shared/backups/YYYY/MM/DD/<release>/`，访问权限 0700/0600，含敏感配置不得下载到公开位置。回滚优先切回 previous release 并重启；新表保留，不影响旧代码，不恢复数据库即可保留发布后历史。部署失败自动切回旧版本；AI HTTPS 入口在旧版本不可用属于预期回退。

如果明确要撤销数据库新增表：先停止服务并完整备份，切回旧代码后按 ai_requests、ai_jobs、ai_secrets、ai_clients、ai_credentials、audit_events 顺序 DROP TABLE；此举会永久丢失 AI 凭据/任务/审计，须另行确认，默认不执行。仅在确认需要数据级回退时，用在线备份恢复 SQLite、保留新库副本，恢复会丢失备份后的业务历史。
