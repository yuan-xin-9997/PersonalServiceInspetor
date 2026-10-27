from __future__ import annotations

import hmac
import os
import secrets
from datetime import datetime
from html import escape
from typing import Annotated, Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from .ai_oauth import InspectorOAuth
from .ai_service import SCOPES, AIService, Principal

Offset = Annotated[int, Query(ge=0, le=1000000)]
Limit = Annotated[int, Query(ge=1, le=100)]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CredentialCreate(StrictModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"\S", examples=["巡检助手"])
    expires_at: datetime = Field(examples=["2026-12-31T23:59:59+08:00"])
    scopes: list[str] = Field(min_length=1, max_length=6, examples=[["status:read"]])


class Toggle(StrictModel):
    enabled: bool = Field(strict=True)
    confirm: bool = Field(
        default=False, strict=True, description="确认影响：停用会关闭该项当前异常；历史保留"
    )


class RunRequest(StrictModel):
    pass


class JobAccepted(BaseModel):
    id: str
    status_url: str


class Page[T](BaseModel):
    items: list[T]
    total: int
    offset: int
    limit: int


class CheckView(BaseModel):
    check_id: str
    name: str
    category: str
    type: str
    enabled: bool
    status: str
    consecutive_failures: int
    message: str
    latency_ms: float | None
    last_checked_at: str | None
    last_success_at: str | None


class SummaryView(BaseModel):
    overall_status: str
    counts: dict[str, int]
    total: int
    configured_total: int
    disabled: int
    categories: list[dict[str, Any]]
    last_inspection: dict[str, Any] | None
    last_daily_report: dict[str, Any] | None
    open_incidents: list[dict[str, Any]]
    recovered_incidents: list[dict[str, Any]]
    states: list[CheckView]


class TokenView(BaseModel):
    access_token: str
    token_type: str
    expires_in: int
    scope: str


class CredentialView(BaseModel):
    id: str
    name: str
    prefix: str
    scopes: list[str]
    created_at: str
    expires_at: str
    status: str


class CredentialIssued(CredentialView):
    secret: str = Field(description="仅本次创建返回，服务端只保存摘要；不要写入浏览器存储或日志")


class JobResult(BaseModel):
    id: str
    status: str = Field(description="QUEUED / RUNNING / SUCCESS / SKIPPED / FAILED")
    created_at: str
    finished_at: str | None
    result: dict[str, Any] | None
    error: str | None
    progress: int | None = Field(description="完成时 100；巡检引擎不提供中间百分比时为 null")


def install_ai(application, runtime, current_user):
    service = AIService(runtime)
    runtime.ai = service
    base = (os.getenv("AI_PUBLIC_BASE_URL") or str(
            runtime.settings.app.get(
                "ai_public_base_url", runtime.settings.app.get("public_base_url", "")
            )
        )).rstrip("/")
    if not base:
        raise RuntimeError("app.public_base_url is required for OAuth resource binding")
    oauth = InspectorOAuth(service, base)
    runtime.ai_base_url = base
    bearer = HTTPBearer(
        auto_error=False,
        scheme_name="MachineBearer",
        description="独立 API Key；或由 /api/v1/token 自动换取的 15 分钟 Bearer 令牌。MCP 只接受短期令牌。",
    )

    def machine(
        request: Request, auth: HTTPAuthorizationCredentials | None = Depends(bearer)
    ) -> Principal:
        p = service.authenticate(auth.credentials if auth else "")
        request.state.actor = p.username + ":" + p.credential_id
        return p

    def browser(request: Request, user=Depends(current_user), csrf_header: Annotated[str | None, Header(alias="X-CSRF-Token")] = None):
        if user["role"] != "admin" and "ai" not in user["pages"]:
            raise HTTPException(403, "需要 AI 接入页面权限")
        if request.method != "GET":
            expected = request.session.get("csrf", "")
            if not expected or not hmac.compare_digest(
                expected, csrf_header or ""
            ):
                raise HTTPException(403, "CSRF 校验失败，请刷新页面")
        return user

    router = APIRouter(
        prefix="/api/v1",
        tags=["AI 业务 API"],
        responses={
            code: {
                "description": message,
                "content": {
                    "application/json": {"example": {"error": {"code": code, "message": message}}}
                },
            }
            for code, message in [
                (401, "凭据缺失或无效"),
                (403, "权限不足"),
                (404, "对象不存在"),
                (409, "幂等请求冲突"),
                (422, "参数校验失败"),
                (429, "请求过于频繁"),
                (500, "服务执行失败"),
            ]
        },
    )

    @router.get("/summary", summary="总体巡检状态", response_model=SummaryView)
    def summary(p=Depends(machine)):
        return service.summary(p)

    @router.get("/checks", summary="分页查询巡检项、开关和最新结果", response_model=Page[CheckView])
    def checks(offset: Offset = 0, limit: Limit = 50, p=Depends(machine)):
        return service.checks(p, offset, limit)

    @router.get("/incidents", summary="分页查询异常事件", response_model=dict[str, Any])
    def incidents(offset: Offset = 0, limit: Limit = 50, p=Depends(machine)):
        return service.incidents(p, offset, limit)

    @router.get("/history", summary="查询每日巡检趋势（1–90 天）", response_model=dict[str, Any])
    def history(days: int = Query(30, ge=1, le=90), p=Depends(machine)):
        return service.history(p, days)

    @router.get("/tasks", summary="分页查询自己的 AI 巡检任务", response_model=Page[JobResult])
    def tasks(offset: Offset = 0, limit: Limit = 50, p=Depends(machine)):
        return service.tasks(p, offset, limit)

    @router.get("/tasks/{job_id}", response_model=JobResult, summary="查询任务状态、结果和失败原因")
    def task(job_id: str, p=Depends(machine)):
        return service.task(p, job_id)

    @router.get("/tasks/{job_id}/results", summary="分页查询已执行任务的逐项结果，需 tasks:read 和 checks:read", response_model=dict[str, Any])
    def task_results(job_id: str, offset: Offset = 0, limit: Limit = 50, p=Depends(machine)):
        return service.task_results(p, job_id, offset, limit)

    @router.post(
        "/tasks", status_code=202, response_model=JobAccepted, summary="异步执行配置中的只读巡检"
    )
    async def run(body: RunRequest, idempotency_key: IdempotencyKey, p=Depends(machine)):
        return service.submit(p, idempotency_key)

    @router.put(
        "/checks/{check_id}", summary="明确确认后启用/停用单个巡检项", response_model=dict[str, Any]
    )
    def toggle(check_id: str, body: Toggle, idempotency_key: IdempotencyKey, p=Depends(machine)):
        return service.toggle(p, check_id, body.enabled, body.confirm, idempotency_key)

    @router.post("/token", summary="程序自动以 API Key 换取短期令牌", response_model=TokenView)
    def token(request: Request, p=Depends(machine)):
        # This automation exchange is distinct from OAuth authorization-code /token.
        if not request.headers.get("authorization", "").startswith("Bearer psi_"):
            raise HTTPException(401, "此端点仅接受 API Key，OAuth 客户端请使用 refresh_token")
        return service.issue_tokens(p, "api-key-automation", oauth.resource)

    application.include_router(router)
    manage = APIRouter(prefix="/api/ai", tags=["网页登录凭据管理"])

    @manage.get("/info")
    def info(request: Request, user=Depends(browser)) -> dict:
        csrf = request.session.setdefault("csrf", secrets.token_urlsafe(32))
        allowed = service.allowed_scopes(user["username"])
        return {
            "csrf": csrf,
            "scopes": [
                {"id": key, "description": SCOPES[key][0]} for key in SCOPES if key in allowed
            ],
            "api_url": base + "/api/v1",
            "openapi_url": base + "/openapi.json",
            "docs_url": base + "/docs",
            "mcp_url": oauth.resource,
            "token_url": base + "/api/v1/token",
            "oauth_metadata_url": base + "/.well-known/oauth-authorization-server",
        }

    @manage.get("/credentials", response_model=Page[CredentialView])
    def credentials(offset: Offset = 0, limit: Limit = 20, user=Depends(browser)) -> dict:
        return service.credentials(user["username"], offset, limit)

    @manage.post("/credentials", status_code=201, response_model=CredentialIssued)
    def create(body: CredentialCreate, user=Depends(browser)) -> dict:
        if body.expires_at.tzinfo is None:
            raise HTTPException(422, "expires_at 必须包含时区")
        return service.create_credential(
            user["username"], body.name.strip(), body.scopes, int(body.expires_at.timestamp())
        )

    @manage.delete("/credentials/{credential_id}")
    def revoke(credential_id: str, user=Depends(browser)) -> dict:
        return service.revoke(user["username"], credential_id)

    application.include_router(manage)

    @application.post("/revoke", include_in_schema=False)
    async def revoke_oauth_token(client_id: str = Form(...), token: str = Form(...)):
        # SDK 2.3's revoke form incorrectly requires client_secret even for public clients.
        client = await oauth.get_client(client_id)
        if not client or client.token_endpoint_auth_method != "none":
            raise HTTPException(401, "无效客户端")
        value = await oauth.load_access_token(token) or await oauth.load_refresh_token(
            client, token
        )
        if value and value.client_id == client_id:
            await oauth.revoke_token(value)
        return {}

    @application.get("/ai/consent/{pending}", response_class=HTMLResponse, include_in_schema=False)
    async def consent_page(pending: str, request: Request):
        row = oauth.load(pending, "pending")
        if not row:
            raise HTTPException(400, "授权请求已过期")
        if not request.session.get("user"):
            request.session["oauth_return"] = "/ai/consent/" + pending
            return RedirectResponse("/login", status_code=303)
        user = current_user(request)
        if user["role"] != "admin" and "ai" not in user["pages"]:
            raise HTTPException(403, "需要 AI 接入页面权限")
        request.session["consent"] = pending
        csrf = request.session.setdefault("csrf", secrets.token_urlsafe(32))
        requested = row.payload["params"].get("scopes") or ["status:read"]
        options = "".join(
            f'<label><input type="checkbox" name="scopes" value="{escape(scope)}" {"checked" if scope == "status:read" else ""}>{escape(SCOPES[scope][0])}</label><br>'
            for scope in requested
            if scope in service.allowed_scopes(user["username"])
        )
        return HTMLResponse(f'''<!doctype html><html lang="zh-CN"><meta name="viewport" content="width=device-width,initial-scale=1">
        <link rel="stylesheet" href="/static/app.css"><main class="main"><section class="panel"><h1>授权 AI 客户端</h1>
        <p>当前用户：{escape(user["username"])}；客户端：{escape(row.payload["client_id"])}</p>
        <p>仅选择必要权限。客户端自动获取 15 分钟令牌并续取。可在 AI 接入页面撤销授权。</p>
        <form method="post"><input type="hidden" name="csrf" value="{csrf}">{options}
        <label>授权有效期（天，1–366）<input type="number" name="days" value="30" min="1" max="366"></label>
        <button type="submit" class="primary">确认授权</button> <a href="/">取消并返回</a></form></section></main></html>''')

    @application.post("/ai/consent/{pending}", include_in_schema=False)
    async def consent_submit(
        pending: str,
        request: Request,
        csrf: str = Form(...),
        scopes: list[str] = Form(...),
        days: int = Form(30, ge=1, le=366),
    ):
        user = current_user(request)
        if user["role"] != "admin" and "ai" not in user["pages"]:
            raise HTTPException(403, "需要 AI 接入页面权限")
        if request.session.get("consent") != pending or not hmac.compare_digest(
            request.session.get("csrf", ""), csrf
        ):
            raise HTTPException(403, "CSRF 校验失败")
        request.session.pop("consent", None)
        return RedirectResponse(
            oauth.consent(pending, user["username"], scopes, days), status_code=303
        )

    mcp = MCPServer(
        "个人系统巡检中心",
        version="1.0",
        auth_server_provider=oauth,
        auth=AuthSettings(
            issuer_url=base,
            resource_server_url=oauth.resource,
            validate_token_resource=True,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=list(SCOPES), default_scopes=["status:read"]
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
    )

    def mcp_principal():
        token = get_access_token()
        if not token:
            raise ValueError("Authentication required")
        return service.authenticate(token.token, allow_key=False)

    read = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )

    @mcp.tool(annotations=read)
    async def get_summary() -> dict:
        """查看总体巡检状态与异常计数。需要 status:read。"""
        return service.summary(mcp_principal())

    @mcp.tool(annotations=read)
    async def list_checks(
        offset: Annotated[int, Field(ge=0, le=1000000)] = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict:
        """分页查看巡检项开关及最新检查结果。需要 checks:read。"""
        return service.checks(mcp_principal(), offset, limit)

    @mcp.tool(annotations=read)
    async def list_incidents(
        offset: Annotated[int, Field(ge=0, le=1000000)] = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict:
        """分页查看异常事件及恢复情况。需要 status:read。"""
        return service.incidents(mcp_principal(), offset, limit)

    @mcp.tool(annotations=read)
    async def get_history(days: Annotated[int, Field(ge=1, le=90)] = 30) -> dict:
        """查看每日巡检趋势，最多 90 天。需要 history:read。"""
        return service.history(mcp_principal(), days)

    @mcp.tool(annotations=read)
    async def list_tasks(
        offset: Annotated[int, Field(ge=0, le=1000000)] = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict:
        """分页查询当前用户提交的 AI 任务。需要 tasks:read。"""
        return service.tasks(mcp_principal(), offset, limit)

    @mcp.tool(annotations=read)
    async def get_task(task_id: str) -> dict:
        """按任务 ID 查询状态、结果和失败原因。需要 tasks:read，仅能查询自己的任务。"""
        return service.task(mcp_principal(), task_id)

    @mcp.tool(annotations=read)
    async def get_task_results(task_id: str, offset: Annotated[int, Field(ge=0, le=1000000)] = 0, limit: Annotated[int, Field(ge=1, le=100)] = 50) -> dict:
        """分页获取自己任务的逐项巡检结果，需 tasks:read 和 checks:read。尚未完成的任务可能没有结果。"""
        return service.task_results(mcp_principal(), task_id, offset, limit)

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True
        )
    )
    async def run_inspection(
        idempotency_key: Annotated[str, Field(min_length=1, max_length=128)],
    ) -> dict:
        """异步执行配置中的只读巡检，返回任务 ID。可能发送配置中已有的告警。需 inspection:run、tasks:read；重试必须复用幂等键。"""
        return service.submit(mcp_principal(), idempotency_key)

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
        )
    )
    async def set_check_enabled(
        check_id: str,
        enabled: bool,
        confirm: bool,
        idempotency_key: Annotated[str, Field(min_length=1, max_length=128)],
    ) -> dict:
        """启用或停用一个巡检项。停用会关闭该项未恢复事件、保留历史。仅管理员 checks:write；须用户明确同意影响后传 confirm=true。"""
        return service.toggle(mcp_principal(), check_id, enabled, confirm, idempotency_key)

    hosts = runtime.settings.security.get("allowed_hosts", [])
    hosts = [*hosts, urlparse(str(runtime.settings.app.get("public_base_url", base))).netloc]
    host = urlparse(base).netloc
    origins = runtime.settings.security.get(
        "allowed_origins", [base, runtime.settings.app.get("public_base_url", base)]
    )
    mcp_app = mcp.streamable_http_app(
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(set([host, "testserver", *hosts])),
            allowed_origins=origins,
        ),
    )
    application.mount("/", mcp_app)
    runtime.mcp = mcp
    return service
