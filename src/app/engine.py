from __future__ import annotations

import asyncio
import logging
import uuid
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select

from .checkers import CheckRunner
from .config import Settings
from .database import CheckResultRecord, CheckState, Database, Incident, TaskRun
from .schemas import CheckResult

logger = logging.getLogger(__name__)


class NotificationSink(Protocol):
    async def send_incident(self, incident_id: int) -> bool: ...

    async def send_recovery(self, incident_id: int) -> bool: ...


class InspectionEngine:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        runner: CheckRunner | None = None,
        notifier: NotificationSink | None = None,
    ):
        self.settings = settings
        self.database = database
        self.runner = runner or CheckRunner(settings.defaults, settings.timezone)
        self.notifier = notifier
        self._lock = asyncio.Lock()

    async def run_all(self, trigger: str = "manual") -> dict[str, Any]:
        if self._lock.locked():
            return {"status": "SKIPPED", "message": "巡检任务正在运行"}
        async with self._lock:
            run_id = uuid.uuid4().hex
            started_at = self._now()
            task_db_id = self._start_task(run_id, trigger, started_at)
            logger.info("inspection started run_id=%s trigger=%s", run_id, trigger)
            try:
                results = await asyncio.gather(*(self.runner.run(item) for item in self.settings.checks))
                opened, recovered = self._persist(run_id, results)
                await self._notify(opened, recovered)
                counts = Counter(result.status for result in results)
                self._finish_task(task_db_id, "SUCCESS", f"完成 {len(results)} 项检查", len(results))
                return {
                    "status": "SUCCESS",
                    "run_id": run_id,
                    "started_at": started_at.isoformat(),
                    "finished_at": self._now().isoformat(),
                    "counts": {key: counts.get(key, 0) for key in ("OK", "WARNING", "CRITICAL", "UNKNOWN")},
                    "opened_incidents": opened,
                    "recovered_incidents": recovered,
                }
            except Exception as exc:
                logger.exception("inspection failed run_id=%s", run_id)
                self._finish_task(task_db_id, "FAILED", str(exc), 0)
                raise

    def _persist(self, run_id: str, results: list[CheckResult]) -> tuple[list[int], list[int]]:
        opened: list[int] = []
        recovered: list[int] = []
        threshold_default = int(self.settings.defaults.get("failure_threshold", 3))
        by_id = {item["id"]: item for item in self.settings.checks}
        with self.database.session() as session:
            for result in results:
                self._enrich_historical_metrics(session, result)
                session.add(
                    CheckResultRecord(
                        run_id=run_id,
                        check_id=result.check_id,
                        name=result.name,
                        category=result.category,
                        check_type=result.check_type,
                        status=result.status,
                        message=result.message,
                        latency_ms=result.latency_ms,
                        metrics=result.metrics,
                        observed_at=result.observed_at,
                    )
                )
                state = session.get(CheckState, result.check_id)
                if state is None:
                    state = CheckState(
                        check_id=result.check_id,
                        name=result.name,
                        category=result.category,
                        current_status="UNKNOWN",
                        consecutive_failures=0,
                        last_message="",
                    )
                    session.add(state)
                previous_status = state.current_status
                state.name = result.name
                state.category = result.category
                state.current_status = result.status
                state.last_message = result.message
                state.last_latency_ms = result.latency_ms
                state.last_checked_at = result.observed_at

                if result.status == "OK":
                    state.last_success_at = result.observed_at
                    state.consecutive_failures = 0
                    if state.active_incident_id:
                        incident = session.get(Incident, state.active_incident_id)
                        if incident and incident.status == "OPEN":
                            incident.status = "RECOVERED"
                            incident.recovered_at = result.observed_at
                            incident.last_seen_at = result.observed_at
                            recovered.append(incident.id)
                        state.active_incident_id = None
                    continue

                if result.status == "UNKNOWN":
                    # A missing optional credential must remain visible but must not page.
                    state.consecutive_failures = 0
                    continue

                state.consecutive_failures = 1 if previous_status in {"OK", "UNKNOWN"} else state.consecutive_failures + 1
                item = by_id[result.check_id]
                threshold = int(item.get("failure_threshold", threshold_default))
                if state.active_incident_id:
                    incident = session.get(Incident, state.active_incident_id)
                    if incident:
                        incident.failure_count = state.consecutive_failures
                        incident.last_seen_at = result.observed_at
                        incident.last_error = result.message
                        if result.status == "CRITICAL":
                            incident.severity = "CRITICAL"
                            if state.consecutive_failures >= threshold and not incident.alert_sent_at:
                                opened.append(incident.id)
                    continue

                should_open = result.status == "WARNING" or state.consecutive_failures >= threshold
                # Critical checks are deliberately debounced; warning conditions open immediately.
                if should_open:
                    incident = Incident(
                        check_id=result.check_id,
                        name=result.name,
                        category=result.category,
                        severity=result.status,
                        status="OPEN",
                        first_seen_at=result.observed_at,
                        opened_at=result.observed_at,
                        last_seen_at=result.observed_at,
                        failure_count=state.consecutive_failures,
                        last_error=result.message,
                    )
                    session.add(incident)
                    session.flush()
                    state.active_incident_id = incident.id
                    opened.append(incident.id)
        return opened, recovered

    def _enrich_historical_metrics(self, session: Any, result: CheckResult) -> None:
        if result.category == "database" and result.metrics.get("size_bytes") is not None:
            cutoff = result.observed_at - timedelta(hours=24)
            previous = session.scalar(
                select(CheckResultRecord)
                .where(
                    CheckResultRecord.check_id == result.check_id,
                    CheckResultRecord.observed_at <= cutoff,
                )
                .order_by(desc(CheckResultRecord.observed_at))
                .limit(1)
            )
            if previous and previous.metrics.get("size_bytes") is not None:
                result.metrics["size_growth_24h_bytes"] = int(result.metrics["size_bytes"]) - int(previous.metrics["size_bytes"])

        if result.category == "log":
            cutoff = result.observed_at - timedelta(days=7)
            previous_rows = list(
                session.scalars(
                    select(CheckResultRecord).where(
                        CheckResultRecord.check_id == result.check_id,
                        CheckResultRecord.observed_at >= cutoff,
                    )
                )
            )
            daily: dict[str, list[int]] = {}
            for row in previous_rows:
                counts = row.metrics.get("counts", {})
                error_count = int(counts.get("error", 0)) + int(counts.get("critical", 0)) + int(counts.get("traceback", 0))
                daily.setdefault(self._aware(row.observed_at).date().isoformat(), []).append(error_count)
            daily_values = [sum(values) / len(values) for values in daily.values() if values]
            baseline = sum(daily_values) / len(daily_values) if daily_values else 0.0
            current_counts = result.metrics.get("counts", {})
            current = int(current_counts.get("error", 0)) + int(current_counts.get("critical", 0)) + int(current_counts.get("traceback", 0))
            result.metrics["baseline_7d_error_average"] = round(baseline, 2)
            result.metrics["baseline_delta_percent"] = round((current - baseline) / baseline * 100, 2) if baseline else None
            if current >= max(5, baseline * 2):
                result.status = "WARNING"
                result.message += f"；显著高于 7 天日均 {baseline:.1f}"

    async def _notify(self, opened: list[int], recovered: list[int]) -> None:
        if not self.notifier:
            return
        for incident_id in opened:
            try:
                with self.database.session() as session:
                    incident = session.get(Incident, incident_id)
                    if not incident or incident.severity != "CRITICAL" or incident.alert_sent_at:
                        continue
                sent = await self.notifier.send_incident(incident_id)
                if sent:
                    self._mark_notification(incident_id, "alert")
            except Exception:
                logger.exception("incident notification failed id=%s", incident_id)
        for incident_id in recovered:
            try:
                with self.database.session() as session:
                    incident = session.get(Incident, incident_id)
                    if not incident or incident.severity != "CRITICAL" or not incident.alert_sent_at or incident.recovery_sent_at:
                        continue
                sent = await self.notifier.send_recovery(incident_id)
                if sent:
                    self._mark_notification(incident_id, "recovery")
            except Exception:
                logger.exception("recovery notification failed id=%s", incident_id)

    def _mark_notification(self, incident_id: int, kind: str) -> None:
        with self.database.session() as session:
            incident = session.get(Incident, incident_id)
            if not incident:
                return
            if kind == "alert":
                incident.alert_sent_at = self._now()
            else:
                incident.recovery_sent_at = self._now()

    def summary(self) -> dict[str, Any]:
        with self.database.session() as session:
            states = list(session.scalars(select(CheckState).order_by(CheckState.category, CheckState.name)))
            latest_task = session.scalar(select(TaskRun).where(TaskRun.task_id.like("inspection:%")).order_by(desc(TaskRun.started_at)).limit(1))
            report_task = session.scalar(select(TaskRun).where(TaskRun.task_id.like("daily-report:%")).order_by(desc(TaskRun.started_at)).limit(1))
            open_incidents = list(session.scalars(select(Incident).where(Incident.status == "OPEN").order_by(desc(Incident.opened_at))))
            recently_recovered = list(session.scalars(select(Incident).where(Incident.status == "RECOVERED").order_by(desc(Incident.recovered_at)).limit(20)))
        counts = Counter(state.current_status for state in states)
        overall = "CRITICAL" if counts["CRITICAL"] else "WARNING" if counts["WARNING"] or counts["UNKNOWN"] else "OK"
        return {
            "overall_status": overall,
            "counts": {key: counts.get(key, 0) for key in ("OK", "WARNING", "CRITICAL", "UNKNOWN")},
            "total": len(states),
            "last_inspection": self._task_dict(latest_task),
            "last_daily_report": self._task_dict(report_task),
            "open_incidents": [self._incident_dict(value) for value in open_incidents],
            "recovered_incidents": [self._incident_dict(value) for value in recently_recovered],
            "states": [self._state_dict(value) for value in states],
        }

    def checks(self, category: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        with self.database.session() as session:
            statement = select(CheckResultRecord).order_by(desc(CheckResultRecord.observed_at)).limit(limit)
            if category:
                statement = statement.where(CheckResultRecord.category == category)
            records = list(session.scalars(statement))
        return [
            {
                "id": row.id,
                "run_id": row.run_id,
                "check_id": row.check_id,
                "name": row.name,
                "category": row.category,
                "type": row.check_type,
                "status": row.status,
                "message": row.message,
                "latency_ms": row.latency_ms,
                "metrics": row.metrics,
                "observed_at": self._iso(row.observed_at),
            }
            for row in records
        ]

    def incidents(self, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        with self.database.session() as session:
            statement = select(Incident).order_by(desc(Incident.opened_at)).limit(limit)
            if status:
                statement = statement.where(Incident.status == status)
            values = list(session.scalars(statement))
        return [self._incident_dict(value) for value in values]

    def tasks(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.session() as session:
            values = list(session.scalars(select(TaskRun).order_by(desc(TaskRun.started_at)).limit(limit)))
        return [self._task_dict(value) for value in values]

    def history(self, days: int = 30) -> list[dict[str, Any]]:
        cutoff = self._now() - timedelta(days=max(1, min(days, 90)))
        with self.database.session() as session:
            records = list(session.scalars(select(CheckResultRecord).where(CheckResultRecord.observed_at >= cutoff).order_by(CheckResultRecord.observed_at)))
        buckets: dict[str, Counter[str]] = {}
        for row in records:
            day = self._aware(row.observed_at).date().isoformat()
            buckets.setdefault(day, Counter())[row.status] += 1
        return [{"date": day, **{key: counts[key] for key in ("OK", "WARNING", "CRITICAL", "UNKNOWN")}} for day, counts in sorted(buckets.items())]

    def _start_task(self, run_id: str, trigger: str, started_at: datetime) -> int:
        with self.database.session() as session:
            task = TaskRun(task_id=f"inspection:{run_id}", task_name=f"统一巡检（{trigger}）", status="RUNNING", started_at=started_at)
            session.add(task)
            session.flush()
            return task.id

    def _finish_task(self, task_id: int, status: str, detail: str, count: int) -> None:
        with self.database.session() as session:
            task = session.get(TaskRun, task_id)
            if task:
                task.status = status
                task.finished_at = self._now()
                task.detail = detail
                task.result_count = count

    def _now(self) -> datetime:
        return datetime.now(ZoneInfo(self.settings.timezone))

    def _aware(self, value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=ZoneInfo(self.settings.timezone))

    def _iso(self, value: datetime | None) -> str | None:
        return self._aware(value).isoformat() if value else None

    def _state_dict(self, value: CheckState) -> dict[str, Any]:
        return {
            "check_id": value.check_id,
            "name": value.name,
            "category": value.category,
            "status": value.current_status,
            "consecutive_failures": value.consecutive_failures,
            "message": value.last_message,
            "latency_ms": value.last_latency_ms,
            "last_checked_at": self._iso(value.last_checked_at),
            "last_success_at": self._iso(value.last_success_at),
        }

    def _incident_dict(self, value: Incident) -> dict[str, Any]:
        recovered = self._aware(value.recovered_at) if value.recovered_at else None
        started = self._aware(value.first_seen_at)
        end = recovered or self._now()
        return {
            "id": value.id,
            "check_id": value.check_id,
            "name": value.name,
            "category": value.category,
            "severity": value.severity,
            "status": value.status,
            "first_seen_at": started.isoformat(),
            "opened_at": self._iso(value.opened_at),
            "last_seen_at": self._iso(value.last_seen_at),
            "recovered_at": recovered.isoformat() if recovered else None,
            "duration_seconds": max(0, int((end - started).total_seconds())),
            "failure_count": value.failure_count,
            "last_error": value.last_error,
            "alert_sent_at": self._iso(value.alert_sent_at),
            "recovery_sent_at": self._iso(value.recovery_sent_at),
        }

    def _task_dict(self, value: TaskRun | None) -> dict[str, Any] | None:
        if not value:
            return None
        return {
            "id": value.id,
            "task_id": value.task_id,
            "name": value.task_name,
            "status": value.status,
            "started_at": self._iso(value.started_at),
            "finished_at": self._iso(value.finished_at),
            "detail": value.detail,
            "result_count": value.result_count,
        }
