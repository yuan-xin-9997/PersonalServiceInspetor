from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager, contextmanager
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import APIKeyCookie
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from .ai_api import install_ai
from .ai_service import digest
from .checkers import CheckRunner
from .config import Settings, load_settings
from .database import Database, UserPermission
from .engine import InspectionEngine
from .mailer import Mailer
from .scheduler import InspectionScheduler
from .security import UserStore, app_secret
from .settings_store import SettingsStore

logger = logging.getLogger(__name__)
PACKAGE_DIRECTORY = Path(__file__).parent
templates = Jinja2Templates(directory=PACKAGE_DIRECTORY / "templates")

PAGES_CATALOG = [
    {"id": "dashboard", "label": "仪表盘", "group": ""},
    {"id": "inspection", "label": "巡检管理", "group": ""},
    {"id": "hosts", "label": "主机", "group": "巡检管理"},
    {"id": "services", "label": "服务和接口", "group": "巡检管理"},
    {"id": "docker", "label": "Docker", "group": "巡检管理"},
    {"id": "databases", "label": "数据库", "group": "巡检管理"},
    {"id": "logs", "label": "日志", "group": "巡检管理"},
    {"id": "jenkins", "label": "Jenkins 和定时任务", "group": "巡检管理"},
    {"id": "history", "label": "历史记录", "group": ""},
    {"id": "tasks", "label": "任务中心", "group": ""},
    {"id": "config", "label": "系统配置", "group": ""},
    {"id": "ai", "label": "AI 接入", "group": "系统配置"},
    {"id": "users", "label": "权限管理", "group": ""},
]


class Runtime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.database = Database(settings.database_url)
        self.store = SettingsStore(self.database, settings)
        self.runner = CheckRunner(settings.defaults, settings.timezone)
        self.mailer = Mailer(settings, self.database)
        self.engine = InspectionEngine(settings, self.database, self.runner, self.mailer, store=self.store)
        self.mailer.bind_engine(self.engine)
        self.scheduler = InspectionScheduler(settings, self.engine, self.mailer, store=self.store)
        self.users = UserStore(settings.security.get("password_file", "./data/password.txt"), self.database, settings.timezone)
        self.startup_task: asyncio.Task | None = None

    def apply_runtime_settings(self) -> None:
        self.runner.defaults = self.store.effective_defaults()

    async def start(self) -> None:
        self.database.create_all()
        self.users.sync_permissions()
        self.apply_runtime_settings()
        self.scheduler.start()
        if self.store.schedule().get("run_on_startup", True):
            self.startup_task = asyncio.create_task(self.engine.run_all(trigger="startup"))

    async def stop(self) -> None:
        self.scheduler.shutdown()
        if self.startup_task and not self.startup_task.done():
            self.startup_task.cancel()


def _configure_logging(settings: Settings) -> None:
    log_path = Path(settings.logging.get("path", "./logs/app.log"))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = TimedRotatingFileHandler(
        log_path,
        when="midnight",
        backupCount=int(settings.logging.get("backup_count", 14)),
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(value, TimedRotatingFileHandler) for value in root.handlers):
        root.addHandler(handler)


def create_app(settings: Settings | None = None, start_scheduler: bool = True) -> FastAPI:
    config = settings or load_settings()
    Path(config.storage.get("report_directory", "./data/reports")).mkdir(parents=True, exist_ok=True)
    Path(config.security.get("password_file", "./data/password.txt")).parent.mkdir(parents=True, exist_ok=True)
    _configure_logging(config)
    runtime = Runtime(config)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        runtime.database.create_all()
        runtime.users.sync_permissions()
        runtime.apply_runtime_settings()
        runtime.ai.recover()
        if start_scheduler:
            runtime.scheduler.start()
            if runtime.store.schedule().get("run_on_startup", True):
                runtime.startup_task = asyncio.create_task(runtime.engine.run_all(trigger="startup"))
        async with runtime.mcp.session_manager.run():
            tls_server = None
            tls_task = None
            if os.getenv("AI_TLS_CERTFILE"):
                import uvicorn

                class TLSListener(uvicorn.Server):
                    @contextmanager
                    def capture_signals(self):
                        yield

                tls_server = TLSListener(uvicorn.Config(application,
                    host=os.environ["AI_TLS_HOST"], port=int(os.environ["AI_TLS_PORT"]),
                    ssl_certfile=os.environ["AI_TLS_CERTFILE"], ssl_keyfile=os.environ["AI_TLS_KEYFILE"],
                    lifespan="off", access_log=False, proxy_headers=False))
                tls_task = asyncio.create_task(tls_server.serve())
            yield
            if tls_server:
                tls_server.should_exit = True
                await tls_task
        await runtime.ai.stop()
        await runtime.stop()

    application = FastAPI(
        title="个人系统统一巡检中心",
        version=config.version,
        description="主机、服务、Docker、数据库、日志与 Jenkins 巡检。/api/v1 为机器 API，/api/ai 为会话及 CSRF 保护的凭据管理。MCP 使用 OAuth 短期令牌。",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    application.state.runtime = runtime
    application.mount("/static", StaticFiles(directory=PACKAGE_DIRECTORY / "static"), name="static")

    def current_user(request: Request, session_cookie: str | None = Depends(APIKeyCookie(name="session", auto_error=False))) -> dict[str, str]:
        user = request.session.get("user")
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
        identity = runtime.users.identity(user["username"])
        if not identity:
            raise HTTPException(401, "用户已停用或删除")
        page = {"/api/incidents": "dashboard", "/api/checks": "inspection",
                "/api/history": "history", "/api/inspection": "inspection", "/api/tasks": "tasks"}.get(request.url.path)
        if page and identity["role"] != "admin" and page not in identity["pages"]:
            raise HTTPException(403, "无页面访问权限")
        return identity

    def admin_user(user: dict[str, str] = Depends(current_user)) -> dict[str, str]:
        if user.get("role") != "admin":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
        return user

    def run_authorized(request: Request) -> dict[str, str]:
        user = request.session.get("user")
        if user:
            user = runtime.users.identity(user["username"])
        if user and user.get("role") == "admin":
            return user
        expected = os.getenv("RUN_API_TOKEN", "")
        authorization = request.headers.get("authorization", "")
        if expected and authorization == f"Bearer {expected}":
            owner = runtime.users.identity(os.getenv("RUN_API_USERNAME", ""))
            if owner and owner["role"] == "admin":
                return owner
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="需要管理员会话或 Bearer Token")

    @application.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok", "version": config.version}

    @application.get("/health/ready")
    async def health_ready() -> JSONResponse:
        database_ready = runtime.database.ready()
        code = 200 if database_ready else 503
        return JSONResponse({"status": "ok" if database_ready else "not_ready", "database": database_ready, "version": config.version}, status_code=code)

    @application.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, error: str | None = None) -> HTMLResponse:
        return templates.TemplateResponse(request, "login.html", {"error": error, "app_name": config.app.get("name")})

    @application.post("/login")
    async def login(request: Request, username: str = Form(...), password: str = Form(...)) -> RedirectResponse:
        user = runtime.users.authenticate(username, password)
        if not user:
            return RedirectResponse("/login?error=用户名或密码错误", status_code=303)
        oauth_return = request.session.get("oauth_return", "/")
        request.session.clear()
        request.session["user"] = {"username": user.username, "role": user.role}
        request.session["csrf"] = secrets.token_urlsafe(32)
        return RedirectResponse(oauth_return, status_code=303)

    @application.post("/logout")
    async def logout(request: Request) -> RedirectResponse:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @application.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        user = request.session.get("user")
        if not user:
            return RedirectResponse("/login", status_code=303)
        user = current_user(request)
        pages = user["pages"]
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "user": user,
                "version": config.version,
                "pages": pages,
                "pages_catalog": PAGES_CATALOG,
                "app_name": config.app.get("name"),
            },
        )

    @application.get("/api/summary")
    async def api_summary(user: dict[str, str] = Depends(current_user)) -> dict[str, Any]:
        if user["role"] == "admin" or set(user["pages"]) & {"dashboard", "inspection"}:
            return runtime.engine.summary()
        mapping = {"hosts": "host", "services": "service", "docker": "docker", "databases": "database", "logs": "log", "jenkins": "jenkins"}
        categories = {value for page, value in mapping.items() if page in user["pages"]}
        if not categories:
            raise HTTPException(403, "无巡检数据访问权限")
        return runtime.engine.summary(categories=categories)

    @application.get("/api/checks")
    async def api_checks(category: str | None = None, limit: int = 500, _: dict[str, str] = Depends(current_user)) -> list[dict[str, Any]]:
        return runtime.engine.checks(category=category, limit=max(1, min(limit, 2000)))

    @application.get("/api/incidents")
    async def api_incidents(incident_status: str | None = None, _: dict[str, str] = Depends(current_user)) -> list[dict[str, Any]]:
        return runtime.engine.incidents(status=incident_status)

    @application.get("/api/history")
    async def api_history(days: int = 30, _: dict[str, str] = Depends(current_user)) -> list[dict[str, Any]]:
        return runtime.engine.history(days)

    @application.post("/api/run")
    async def api_run(_: dict[str, str] = Depends(run_authorized)) -> dict[str, Any]:
        return await runtime.engine.run_all(trigger="api")

    @application.get("/api/inspection")
    async def api_inspection(_: dict[str, str] = Depends(current_user)) -> dict[str, Any]:
        summary = runtime.engine.summary()
        return {
            "schedule": runtime.store.schedule(),
            "limits": runtime.store.limits(),
            "categories": summary["categories"],
            "checks": summary["states"],
            "jobs": runtime.scheduler.jobs(),
        }

    @application.put("/api/inspection/checks")
    async def api_update_checks(request: Request, _: dict[str, str] = Depends(admin_user)) -> dict[str, Any]:
        body = await request.json()
        check_ids = body.get("ids")
        enabled = body.get("enabled")
        if (
            not isinstance(check_ids, list)
            or not check_ids
            or not all(isinstance(value, str) for value in check_ids)
        ):
            raise HTTPException(status_code=422, detail="ids 必须是非空字符串列表")
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=422, detail="enabled 必须是布尔值")
        try:
            runtime.store.set_checks_enabled(check_ids, enabled)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"巡检项不存在: {exc}") from exc
        closed = 0
        if not enabled:
            for check_id in check_ids:
                closed += runtime.engine.close_open_incidents(check_id)
        return {"ids": check_ids, "enabled": enabled, "closed_incidents": closed}

    @application.put("/api/inspection/checks/{check_id}")
    async def api_update_check(check_id: str, request: Request, _: dict[str, str] = Depends(admin_user)) -> dict[str, Any]:
        body = await request.json()
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=422, detail="enabled 必须是布尔值")
        try:
            runtime.store.set_check_enabled(check_id, enabled)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="巡检项不存在") from exc
        closed = 0 if enabled else runtime.engine.close_open_incidents(check_id)
        return {"check_id": check_id, "enabled": enabled, "closed_incidents": closed}

    @application.put("/api/inspection/schedule")
    async def api_update_schedule(request: Request, _: dict[str, str] = Depends(admin_user)) -> dict[str, Any]:
        try:
            schedule = runtime.store.update_schedule(await request.json())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        runtime.scheduler.reload()
        return {"schedule": schedule, "jobs": runtime.scheduler.jobs()}

    @application.put("/api/inspection/limits")
    async def api_update_limits(request: Request, _: dict[str, str] = Depends(admin_user)) -> dict[str, Any]:
        try:
            limits = runtime.store.update_limits(await request.json())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        runtime.apply_runtime_settings()
        return {"limits": limits}

    @application.get("/api/tasks")
    async def api_tasks(_: dict[str, str] = Depends(current_user)) -> dict[str, Any]:
        return {"scheduled": runtime.scheduler.jobs(), "history": runtime.engine.tasks()}

    @application.get("/api/config")
    async def api_config(_: dict[str, str] = Depends(admin_user)) -> dict[str, Any]:
        value = config.public_view()
        value["runtime"] = {
            "schedule": runtime.store.schedule(),
            "limits": runtime.store.limits(),
        }
        return value

    @application.get("/api/users")
    async def api_users(_: dict[str, str] = Depends(admin_user)) -> list[dict[str, Any]]:
        runtime.users.sync_permissions()
        with runtime.database.session() as session:
            values = list(session.scalars(select(UserPermission).order_by(UserPermission.username)))
            return [{"username": value.username, "role": value.role, "pages": value.pages} for value in values]

    @application.put("/api/users/{username}/permissions")
    async def update_permissions(username: str, request: Request, _: dict[str, str] = Depends(admin_user)) -> dict[str, Any]:
        body = await request.json()
        pages = body.get("pages")
        if not isinstance(pages, list) or not all(isinstance(value, str) for value in pages):
            raise HTTPException(status_code=422, detail="pages 必须是字符串列表")
        with runtime.database.session() as session:
            permission = session.get(UserPermission, username)
            if not permission:
                raise HTTPException(status_code=404, detail="用户不存在")
            if permission.role == "admin":
                raise HTTPException(status_code=400, detail="管理员固定拥有全部页面")
            permission.pages = pages
        return {"username": username, "pages": pages}

    @application.get("/api/email/preview", response_class=HTMLResponse)
    async def email_preview(_: dict[str, str] = Depends(admin_user)) -> HTMLResponse:
        _, html = runtime.mailer.render_daily()
        return HTMLResponse(html)

    @application.post("/api/email/test")
    async def email_test(_: dict[str, str] = Depends(admin_user)) -> dict[str, Any]:
        sent = await runtime.mailer.send_daily(test=True)
        return {"sent": sent, "mode": "smtp" if sent else "preview"}

    @application.get("/docs", response_class=HTMLResponse)
    async def docs_page() -> HTMLResponse:
        return get_swagger_ui_html(openapi_url="/openapi.json", title="巡检中心 API v1")

    install_ai(application, runtime, current_user)
    buckets: dict[str, deque] = defaultdict(deque)

    @application.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return JSONResponse({"detail": exc.detail, "error": {"code": exc.status_code, "message": exc.detail}}, status_code=exc.status_code, headers=exc.headers)

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Pydantic's input field can contain credentials; do not echo it.
        return JSONResponse({"error": {"code": 422, "message": "参数校验失败", "fields": [{"location": list(e["loc"]), "message": e["msg"]} for e in exc.errors()]}}, status_code=422)

    @application.middleware("http")
    async def security_boundary(request: Request, call_next):
        path = request.url.path
        machine_path = path.startswith(("/api/v1", "/mcp"))
        sensitive = machine_path or path.startswith(("/api/ai", "/ai/consent")) or path in {"/token", "/authorize", "/register", "/revoke"}
        response = None
        if path.startswith("/mcp"):
            raw = request.headers.get("authorization", "")
            if raw.startswith("Bearer "):
                try:
                    principal = runtime.ai.authenticate(raw[7:], allow_key=False)
                    request.state.actor = principal.username + ":" + principal.credential_id
                except HTTPException:
                    pass
        origin = request.headers.get("origin")
        permitted = set(config.security.get("allowed_origins", [config.app.get("public_base_url")])) | {str(request.base_url).rstrip("/")}
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin and origin not in permitted:
            logger.warning("Origin rejected origin=%s expected=%s", origin, sorted(str(value) for value in permitted))
            response = JSONResponse({"error": {"code": 403, "message": "Origin 不允许"}}, status_code=403)
        if sensitive:
            # Credential-scoped quota plus source-IP quota makes invalid/rotating keys bounded too.
            timestamp = time.monotonic()
            raw = request.headers.get("authorization", "")
            keys = ["ip:" + (request.client.host if request.client else "unknown")]
            if raw:
                keys.append("credential:" + digest(raw))
            limit = int(config.security.get("ai_rate_limit_per_minute", 120))
            for key in keys:
                bucket = buckets[key]
                while bucket and bucket[0] < timestamp - 60:
                    bucket.popleft()
                if len(bucket) >= limit:
                    response = JSONResponse({"error": {"code": 429, "message": "请求过于频繁"}}, status_code=429, headers={"Retry-After": "60"})
                bucket.append(timestamp)
            if len(buckets) > 10000:
                for key in list(buckets):
                    if not buckets[key] or buckets[key][-1] < timestamp - 60:
                        del buckets[key]
        if response is None and path == "/token" and request.method == "POST":
            fields = parse_qs((await request.body()).decode("utf-8", errors="replace"))
            base = runtime.ai_base_url
            request.state.actor = "client:" + fields.get("client_id", ["unknown"])[0][:100]
            if fields.get("resource") != [base + "/mcp"]:
                response = JSONResponse({"error": "invalid_target", "error_description": "resource must identify this MCP endpoint"}, status_code=400)
        if response is None:
            try:
                response = await call_next(request)
            except Exception:
                # Do not send or log exception bodies, which can include upstream credentials.
                response = JSONResponse({"error": {"code": 500, "message": "服务执行失败"}}, status_code=500)
        if sensitive:
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        if machine_path and response.status_code == 401:
            base = runtime.ai_base_url
            response.headers["WWW-Authenticate"] = f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource/mcp"'
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            user = request.session.get("user", {}).get("username", "anonymous")
            target = "/ai/consent" if path.startswith("/ai/consent/") else path
            actor = getattr(request.state, "actor", user)
            runtime.ai.audit(actor, request.method, target, str(response.status_code))
        return response

    # Outermost middleware makes the existing session available to all boundaries.
    application.add_middleware(CORSMiddleware,
        allow_origins=list({runtime.ai_base_url, str(config.app.get("public_base_url", "")), *config.security.get("allowed_origins", [])}),
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "MCP-Protocol-Version"],
        expose_headers=["WWW-Authenticate", "MCP-Protocol-Version", "Retry-After"])
    application.add_middleware(SessionMiddleware, secret_key=app_secret(),
        max_age=int(config.security.get("session_max_age_seconds", 43200)), same_site="strict",
        https_only=bool(config.security.get("session_cookie_secure", False)))

    return application


app = create_app()
