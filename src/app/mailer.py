from __future__ import annotations

import asyncio
import logging
import os
import smtplib
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import Settings
from .database import Database, Incident, TaskRun

logger = logging.getLogger(__name__)


class Mailer:
    def __init__(self, settings: Settings, database: Database, engine: Any | None = None):
        self.settings = settings
        self.database = database
        self.engine = engine
        self.template_env = Environment(
            loader=FileSystemLoader(Path(__file__).parent / "templates"),
            autoescape=select_autoescape(["html", "xml"]),
        )

    @property
    def configured(self) -> bool:
        return all(os.getenv(name) for name in ("SMTP_HOST", "SMTP_FROM", "SMTP_TO"))

    @property
    def incident_notifications_enabled(self) -> bool:
        return bool(self.settings.email.get("send_incident_notifications", False))

    def bind_engine(self, engine: Any) -> None:
        self.engine = engine

    def render_daily(self) -> tuple[str, str]:
        if self.engine is None:
            raise RuntimeError("Mailer 尚未绑定巡检引擎")
        summary = self.engine.summary()
        checks = self.engine.checks(limit=1000)
        now = datetime.now(ZoneInfo(self.settings.timezone))
        abnormal = summary["counts"]["WARNING"] + summary["counts"]["CRITICAL"] + summary["counts"]["UNKNOWN"]
        subject = f"[系统巡检][{abnormal}项异常] {now:%Y-%m-%d} 个人服务日报"
        html = self.template_env.get_template("email_daily.html").render(summary=summary, checks=checks, now=now, settings=self.settings)
        return subject, html

    async def preview_daily(self) -> Path:
        subject, html = self.render_daily()
        del subject
        directory = Path(self.settings.storage.get("report_directory", "./data/reports"))
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"daily-{datetime.now(ZoneInfo(self.settings.timezone)):%Y%m%d-%H%M%S}.html"
        path.write_text(html, encoding="utf-8")
        return path

    async def send_daily(self, test: bool = False) -> bool:
        subject, html = self.render_daily()
        if test:
            subject = f"[测试]{subject}"
        task_id = self._start_report_task("测试邮件" if test else "每日巡检报告")
        try:
            if not self.configured:
                path = await self.preview_daily()
                self._finish_report_task(task_id, "PREVIEW", f"SMTP 未配置，已生成 {path}")
                return False
            await asyncio.to_thread(self._send, subject, html)
            self._finish_report_task(task_id, "SUCCESS", "邮件发送成功")
            return True
        except Exception as exc:
            self._finish_report_task(task_id, "FAILED", str(exc))
            raise

    async def send_incident(self, incident_id: int) -> bool:
        if not self.incident_notifications_enabled:
            logger.info("incident email disabled by configuration id=%s", incident_id)
            return False
        incident = self._incident(incident_id)
        if not incident or incident.alert_sent_at:
            return False
        subject = f"[系统巡检][{incident.severity}] {incident.name} 故障"
        suggestion = self._suggestion(incident.category)
        html = self.template_env.get_template("email_alert.html").render(incident=incident, suggestion=suggestion, recovery=False)
        if not self.configured:
            logger.warning("SMTP not configured; incident alert preview only id=%s", incident_id)
            return False
        await asyncio.to_thread(self._send, subject, html)
        return True

    async def send_recovery(self, incident_id: int) -> bool:
        if not self.incident_notifications_enabled:
            logger.info("recovery email disabled by configuration id=%s", incident_id)
            return False
        incident = self._incident(incident_id)
        if not incident or incident.recovery_sent_at:
            return False
        subject = f"[系统巡检][已恢复] {incident.name}"
        suggestion = "继续观察后续巡检结果。"
        html = self.template_env.get_template("email_alert.html").render(incident=incident, suggestion=suggestion, recovery=True)
        if not self.configured:
            logger.warning("SMTP not configured; recovery preview only id=%s", incident_id)
            return False
        await asyncio.to_thread(self._send, subject, html)
        return True

    def _send(self, subject: str, html: str) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = os.environ["SMTP_FROM"]
        recipients = [value.strip() for value in os.environ["SMTP_TO"].split(",") if value.strip()]
        message["To"] = ", ".join(recipients)
        message.set_content("请使用支持 HTML 的邮件客户端查看本巡检报告。")
        message.add_alternative(html, subtype="html")
        host = os.environ["SMTP_HOST"]
        port = int(os.getenv("SMTP_PORT", "465"))
        username = os.getenv("SMTP_USERNAME")
        password = os.getenv("SMTP_PASSWORD")
        use_ssl = os.getenv("SMTP_USE_SSL", "true").lower() in {"1", "true", "yes"}
        smtp_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        with smtp_class(host, port, timeout=20) as server:
            if not use_ssl:
                server.starttls()
            if username and password:
                server.login(username, password)
            server.send_message(message, to_addrs=recipients)

    def _incident(self, incident_id: int) -> Incident | None:
        with self.database.session() as session:
            return session.get(Incident, incident_id)

    def _start_report_task(self, name: str) -> int:
        now = datetime.now(ZoneInfo(self.settings.timezone))
        with self.database.session() as session:
            task = TaskRun(task_id=f"daily-report:{now:%Y%m%d%H%M%S}", task_name=name, status="RUNNING", started_at=now)
            session.add(task)
            session.flush()
            return task.id

    def _finish_report_task(self, task_id: int, status: str, detail: str) -> None:
        with self.database.session() as session:
            task = session.get(TaskRun, task_id)
            if task:
                task.status = status
                task.finished_at = datetime.now(ZoneInfo(self.settings.timezone))
                task.detail = detail

    @staticmethod
    def _suggestion(category: str) -> str:
        return {
            "host": "检查主机网络、电源与 SSH 服务；登录后查看系统日志和资源使用率。",
            "service": "先从内网访问健康接口，再检查应用进程、反向代理和最近日志。",
            "database": "检查数据库进程、端口、连接数、磁盘空间及认证配置。",
            "docker": "检查异常容器的 inspect、healthcheck 和最近日志，避免直接重启前丢失现场。",
            "log": "查看高频异常摘要关联的系统日志，确认近期发布或依赖变化。",
            "jenkins": "查看最近失败构建的控制台日志和部署阶段。",
        }.get(category, "检查该系统最近日志、网络和依赖状态。")
