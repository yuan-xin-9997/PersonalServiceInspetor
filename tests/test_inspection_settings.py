from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.database import Database, Incident
from app.engine import InspectionEngine
from app.main import create_app
from app.scheduler import within_window
from app.schemas import CheckResult
from app.settings_store import SettingsStore


def two_checks() -> list[dict]:
    return [
        {"id": "health", "name": "Health", "category": "service", "type": "fake"},
        {"id": "host", "name": "Host", "category": "host", "type": "fake"},
    ]


class OneResultRunner:
    def __init__(self, status: str = "OK"):
        self.status = status
        self.calls: list[str] = []

    async def run(self, item):
        self.calls.append(item["id"])
        return CheckResult(
            item["id"],
            item["name"],
            item["category"],
            item["type"],
            self.status,
            f"result={self.status}",
            datetime.now(ZoneInfo("Asia/Shanghai")),
            5.0,
        )


def test_settings_store_toggles_and_validates(settings_factory):
    settings = settings_factory(two_checks())
    database = Database(settings.database_url)
    database.create_all()
    store = SettingsStore(database, settings)

    assert [item["enabled"] for item in store.configured_checks()] == [True, True]
    store.set_check_enabled("host", False)
    assert store.enabled_map() == {"host": False}
    assert [item["id"] for item in store.enabled_checks()] == ["health"]
    with pytest.raises(KeyError):
        store.set_check_enabled("missing", False)

    schedule = store.update_schedule(
        {
            "mode": "cron",
            "times": ["18:00", "08:00", "08:00"],
            "daily_report_time": "09:30",
            "interval_minutes": 20,
        }
    )
    assert schedule["times"] == ["08:00", "18:00"]
    assert store.schedule()["daily_report_time"] == "09:30"
    with pytest.raises(ValueError, match="巡检模式"):
        store.update_schedule({"mode": "weekly"})
    with pytest.raises(ValueError, match="巡检时间点"):
        store.update_schedule({"times": ["25:00"]})

    limits = store.update_limits({"failure_threshold": 5, "timeout_seconds": 4.5})
    assert limits["failure_threshold"] == 5
    assert limits["timeout_seconds"] == 4.5
    assert store.effective_defaults()["failure_threshold"] == 5
    with pytest.raises(ValueError, match="TLS"):
        store.update_limits({"tls_warning_days": 5})


@pytest.mark.asyncio
async def test_engine_skips_disabled_checks_and_reports_categories(settings_factory):
    settings = settings_factory(two_checks())
    database = Database(settings.database_url)
    database.create_all()
    store = SettingsStore(database, settings)
    store.set_check_enabled("host", False)
    runner = OneResultRunner()
    engine = InspectionEngine(settings, database, runner=runner, store=store)

    result = await engine.run_all()
    assert runner.calls == ["health"]
    assert result["counts"]["OK"] == 1

    summary = engine.summary()
    assert summary["total"] == 1
    assert summary["configured_total"] == 2
    assert summary["disabled"] == 1
    assert summary["overall_status"] == "OK"
    assert {item["check_id"]: item["enabled"] for item in summary["states"]} == {"health": True, "host": False}
    assert {item["id"]: item["total"] for item in summary["categories"]} == {"host": 0, "service": 1}


def test_disabling_check_closes_open_incidents(settings_factory):
    settings = settings_factory(two_checks())
    database = Database(settings.database_url)
    database.create_all()
    store = SettingsStore(database, settings)
    engine = InspectionEngine(settings, database, store=store)
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    with database.session() as session:
        incident = Incident(
            check_id="host",
            name="Host",
            category="host",
            severity="CRITICAL",
            status="OPEN",
            first_seen_at=now,
            opened_at=now,
            last_seen_at=now,
            last_error="failed",
        )
        session.add(incident)
        session.flush()
        incident_id = incident.id

    assert engine.close_open_incidents("host") == 1
    incidents = engine.incidents()
    assert incidents[0]["id"] == incident_id
    assert incidents[0]["status"] == "CLOSED"


def test_scheduler_window_supports_cross_midnight():
    assert within_window("08:00", "18:00", time(9, 0))
    assert not within_window("08:00", "18:00", time(19, 0))
    assert within_window("22:00", "06:00", time(23, 0))
    assert within_window("22:00", "06:00", time(5, 0))
    assert not within_window("22:00", "06:00", time(12, 0))


def test_inspection_api_updates_checks_schedule_and_permissions(settings_factory):
    app = create_app(settings_factory(two_checks()), start_scheduler=False)
    with TestClient(app) as client:
        assert client.get("/api/inspection").status_code == 401
        client.post("/login", data={"username": "admin", "password": "test-password"})

        payload = client.get("/api/inspection").json()
        assert payload["schedule"]["mode"] == "interval"
        assert len(payload["checks"]) == 2

        assert client.put("/api/inspection/checks/health", json={"enabled": False}).status_code == 200
        assert client.get("/api/summary").json()["disabled"] == 1

        response = client.put(
            "/api/inspection/schedule",
            json={"mode": "cron", "times": ["08:00", "18:00"], "daily_report_time": "09:30"},
        )
        assert response.status_code == 200
        assert response.json()["schedule"]["times"] == ["08:00", "18:00"]
        assert client.get("/api/inspection").json()["schedule"]["daily_report_time"] == "09:30"

        response = client.put("/api/inspection/limits", json={"failure_threshold": 4})
        assert response.status_code == 200
        assert response.json()["limits"]["failure_threshold"] == 4
        assert client.put("/api/inspection/limits", json={"failure_threshold": 0}).status_code == 422

        client.post("/login", data={"username": "viewer", "password": "view-password"})
        assert client.get("/api/inspection").status_code == 403
        with app.state.runtime.database.session() as session:
            from app.database import UserPermission
            session.get(UserPermission, "viewer").pages = ["dashboard", "inspection"]
        assert client.get("/api/inspection").status_code == 200
        assert client.put("/api/inspection/checks/health", json={"enabled": True}).status_code == 403
