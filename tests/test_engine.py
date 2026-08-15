from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.database import Database
from app.engine import InspectionEngine
from app.schemas import CheckResult


class SequenceRunner:
    def __init__(self, statuses):
        self.statuses = iter(statuses)

    async def run(self, item):
        status = next(self.statuses)
        return CheckResult(item["id"], item["name"], item["category"], item["type"], status, f"result={status}", datetime.now(ZoneInfo("Asia/Shanghai")), 5.0)


class FakeNotifier:
    def __init__(self):
        self.alerts = []
        self.recoveries = []

    async def send_incident(self, incident_id):
        self.alerts.append(incident_id)
        return True

    async def send_recovery(self, incident_id):
        self.recoveries.append(incident_id)
        return True


@pytest.mark.asyncio
async def test_alert_debounce_dedup_and_recovery(settings_factory):
    settings = settings_factory([{"id": "svc", "name": "服务", "category": "service", "type": "fake", "failure_threshold": 3}])
    database = Database(settings.database_url)
    database.create_all()
    notifier = FakeNotifier()
    engine = InspectionEngine(settings, database, runner=SequenceRunner(["CRITICAL", "CRITICAL", "CRITICAL", "CRITICAL", "OK"]), notifier=notifier)
    await engine.run_all()
    await engine.run_all()
    assert notifier.alerts == []
    await engine.run_all()
    assert len(notifier.alerts) == 1
    await engine.run_all()
    assert len(notifier.alerts) == 1
    await engine.run_all()
    assert notifier.recoveries == notifier.alerts
    incidents = engine.incidents()
    assert incidents[0]["status"] == "RECOVERED"
    assert incidents[0]["duration_seconds"] >= 0


@pytest.mark.asyncio
async def test_warning_is_recorded_without_immediate_or_recovery_email(settings_factory):
    settings = settings_factory([{"id": "svc", "name": "服务", "category": "service", "type": "fake", "failure_threshold": 3}])
    database = Database(settings.database_url)
    database.create_all()
    notifier = FakeNotifier()
    engine = InspectionEngine(settings, database, runner=SequenceRunner(["WARNING", "OK"]), notifier=notifier)
    await engine.run_all()
    assert engine.incidents()[0]["severity"] == "WARNING"
    await engine.run_all()
    assert notifier.alerts == []
    assert notifier.recoveries == []


def test_database_stores_and_reads_states(settings_factory):
    settings = settings_factory()
    database = Database(settings.database_url)
    database.create_all()
    assert database.ready()
    assert InspectionEngine(settings, database).summary()["total"] == 0
