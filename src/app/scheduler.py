from __future__ import annotations

import logging
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .config import Settings
from .engine import InspectionEngine
from .mailer import Mailer
from .settings_store import SettingsStore, parse_time, time_parts

logger = logging.getLogger(__name__)


def within_window(start: str, end: str, current: time) -> bool:
    """Return whether current time is inside an optional HH:MM window.

    Windows that cross midnight (for example 22:00-06:00) are supported.
    """
    starts = time(*time_parts(parse_time(start, "window_start")))
    ends = time(*time_parts(parse_time(end, "window_end")))
    if starts <= ends:
        return starts <= current <= ends
    return current >= starts or current <= ends


class InspectionScheduler:
    def __init__(
        self,
        settings: Settings,
        engine: InspectionEngine,
        mailer: Mailer,
        store: SettingsStore | None = None,
    ):
        self.settings = settings
        self.engine = engine
        self.mailer = mailer
        self.store = store
        self.scheduler = AsyncIOScheduler(timezone=settings.timezone)

    def start(self) -> None:
        if not self.settings.scheduler.get("enabled", True):
            logger.info("scheduler disabled")
            return
        self._schedule_jobs()
        self.scheduler.start()

    def reload(self) -> None:
        """Apply UI changes without restarting the web process."""
        if not self.scheduler.running:
            return
        for job in self.scheduler.get_jobs():
            self.scheduler.remove_job(job.id)
        self._schedule_jobs()

    def _current_schedule(self) -> dict[str, Any]:
        if self.store is not None:
            return self.store.schedule()
        source = self.settings.scheduler
        hour = int(source.get("daily_report_hour", 9))
        minute = int(source.get("daily_report_minute", 0))
        return {
            "enabled": bool(source.get("enabled", True)),
            "mode": str(source.get("mode", "interval")),
            "interval_minutes": int(source.get("inspection_interval_minutes", 15)),
            "times": [str(value) for value in (source.get("inspection_times") or ["09:00"])],
            "window_start": str(source.get("inspection_window_start", "00:00")),
            "window_end": str(source.get("inspection_window_end", "23:59")),
            "daily_report_enabled": bool(source.get("daily_report_enabled", True)),
            "daily_report_time": f"{hour:02d}:{minute:02d}",
            "run_on_startup": bool(source.get("run_on_startup", True)),
        }

    def _schedule_jobs(self) -> None:
        schedule = self._current_schedule()
        if schedule["enabled"]:
            if schedule["mode"] == "cron":
                for index, value in enumerate(schedule["times"]):
                    hour, minute = time_parts(value)
                    self.scheduler.add_job(
                        self._scheduled_inspection,
                        CronTrigger(hour=hour, minute=minute, timezone=self.settings.timezone),
                        id=f"inspection-{index}",
                        name=f"定时巡检 {value}",
                        max_instances=1,
                        coalesce=True,
                        replace_existing=True,
                    )
            else:
                interval = int(schedule["interval_minutes"])
                self.scheduler.add_job(
                    self._scheduled_inspection,
                    IntervalTrigger(minutes=interval),
                    id="inspection",
                    name=f"周期巡检（每 {interval} 分钟）",
                    max_instances=1,
                    coalesce=True,
                    replace_existing=True,
                )
        if schedule["daily_report_enabled"]:
            hour, minute = time_parts(schedule["daily_report_time"])
            self.scheduler.add_job(
                self.mailer.send_daily,
                CronTrigger(hour=hour, minute=minute, timezone=self.settings.timezone),
                id="daily-report",
                name=f"每日 HTML 巡检邮件 {schedule['daily_report_time']}",
                max_instances=1,
                coalesce=True,
                replace_existing=True,
            )
        logger.info(
            "scheduler configured enabled=%s mode=%s interval=%s times=%s report=%s",
            schedule["enabled"],
            schedule["mode"],
            schedule["interval_minutes"],
            schedule["times"],
            schedule["daily_report_time"],
        )

    async def _scheduled_inspection(self) -> dict[str, Any]:
        schedule = self._current_schedule()
        if not schedule["enabled"]:
            return {"status": "SKIPPED", "message": "巡检计划已停用"}
        current = datetime.now(ZoneInfo(self.settings.timezone)).time()
        if not within_window(schedule["window_start"], schedule["window_end"], current):
            logger.info(
                "scheduled inspection skipped outside window %s-%s",
                schedule["window_start"],
                schedule["window_end"],
            )
            return {"status": "SKIPPED", "message": "不在巡检时间窗口内"}
        return await self.engine.run_all(trigger="scheduled")

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
