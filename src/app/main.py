from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from .checkers import CheckRunner
from .config import Settings, load_settings
from .database import Database, UserPermission
from .engine import InspectionEngine
from .mailer import Mailer
from .scheduler import InspectionScheduler
from .security import UserStore, app_secret

logger = logging.getLogger(__name__)
PACKAGE_DIRECTORY = Path(__file__).parent
templates = Jinja2Templates(directory=PACKAGE_DIRECTORY / "templates")


class Runtime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.database = Database(settings.database_url)
        self.runner = CheckRunner(settings.defaults, settings.timezone)
        self.mailer = Mailer(settings, self.database)
        self.engine = InspectionEngine(settings, self.database, self.runner, self.mailer)
        self.mailer.bind_engine(self.engine)
        self.scheduler = InspectionScheduler(settings, self.engine, self.mailer)
        self.users = UserStore(settings.security.get("password_file", "./data/password.txt"), self.database, settings.timezone)
        self.startup_task: asyncio.Task | None = None

    async def start(self) -> None:
        self.database.create_all()
        self.users.sync_permissions()
        self.scheduler.start()
        if self.settings.scheduler.get("run_on_startup", True):
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
        if start_scheduler:
            runtime.scheduler.start()
            if config.scheduler.get("run_on_startup", True):
                runtime.startup_task = asyncio.create_task(runtime.engine.run_all(trigger="startup"))
        yield
        await runtime.stop()

    application = FastAPI(
        title="个人系统统一巡检中心",
        version=config.version,
        description="主机、服务、Docker、数据库、日志与 Jenkins 的统一只读巡检 API",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    application.state.runtime = runtime
    application.add_middleware(
        SessionMiddleware,
        secret_key=app_secret(),
        max_age=int(config.security.get("session_max_age_seconds", 43200)),
        same_site="strict",
        https_only=bool(config.security.get("session_cookie_secure", False)),
    )
    application.mount("/static", StaticFiles(directory=PACKAGE_DIRECTORY / "static"), name="static")

    def current_user(request: Request) -> dict[str, str]:
        user = request.session.get("user")
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
        return user

    def admin_user(user: dict[str, str] = Depends(current_user)) -> dict[str, str]:
        if user.get("role") != "admin":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
        return user

    def run_authorized(request: Request) -> dict[str, str]:
        user = request.session.get("user")
        if user and user.get("role") == "admin":
            return user
        expected = os.getenv("RUN_API_TOKEN", "")
        authorization = request.headers.get("authorization", "")
        if expected and authorization == f"Bearer {expected}":
            return {"username": "api-token", "role": "admin"}
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
        request.session.clear()
        request.session["user"] = {"username": user.username, "role": user.role}
        return RedirectResponse("/", status_code=303)

    @application.post("/logout")
    async def logout(request: Request) -> RedirectResponse:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @application.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        user = request.session.get("user")
        if not user:
            return RedirectResponse("/login", status_code=303)
        permission = runtime.users.permission(user["username"])
        pages = permission.pages if permission else []
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {"user": user, "version": config.version, "pages": pages, "app_name": config.app.get("name")},
        )

    @application.get("/api/summary")
    async def api_summary(_: dict[str, str] = Depends(current_user)) -> dict[str, Any]:
        return runtime.engine.summary()

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

    @application.get("/api/tasks")
    async def api_tasks(_: dict[str, str] = Depends(current_user)) -> dict[str, Any]:
        return {"scheduled": runtime.scheduler.jobs(), "history": runtime.engine.tasks()}

    @application.get("/api/config")
    async def api_config(_: dict[str, str] = Depends(admin_user)) -> dict[str, Any]:
        return config.public_view()

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
    async def docs_page(_: dict[str, str] = Depends(current_user)) -> HTMLResponse:
        return HTMLResponse("<h1>API</h1><p>健康检查公开；其余接口需要登录。详见 README。</p>")

    return application


app = create_app()
