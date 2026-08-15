from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .config import Settings
from .engine import InspectionEngine
from .mailer import Mailer

logger = logging.getLogger(__name__)


class InspectionScheduler:
    def __init__(self, settings: Settings, engine: InspectionEngine, mailer: Mailer):
        self.settings = settings
        self.engine = engine
        self.mailer = mailer
        self.scheduler = AsyncIOScheduler(timezone=settings.timezone)

    def start(self) -> None:
        if not self.settings.scheduler.get("enabled", True):
            logger.info("scheduler disabled")
            return
        interval = int(self.settings.scheduler.get("inspection_interval_minutes", 15))
        self.scheduler.add_job(
            self.engine.run_all,
            IntervalTrigger(minutes=interval),
            kwargs={"trigger": "scheduled"},
            id="inspection",
            name="周期统一巡检",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        self.scheduler.add_job(
            self.mailer.send_daily,
            CronTrigger(
                hour=int(self.settings.scheduler.get("daily_report_hour", 8)),
                minute=int(self.settings.scheduler.get("daily_report_minute", 0)),
                timezone=self.settings.timezone,
            ),
            id="daily-report",
            name="每日 HTML 巡检邮件",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def jobs(self) -> list[dict[str, str | None]]:
        return [
            {
                "id": job.id,
                "name": job.name,
                "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
                "trigger": str(job.trigger),
            }
            for job in self.scheduler.get_jobs()
        ]
