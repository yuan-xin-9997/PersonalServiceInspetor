from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.database import CheckState, Database, Incident
from app.engine import InspectionEngine
from app.mailer import Mailer


def test_daily_email_render(settings_factory):
    settings = settings_factory()
    database = Database(settings.database_url)
    database.create_all()
    with database.session() as session:
        session.add(CheckState(check_id="health", name="示例服务", category="service", current_status="OK", consecutive_failures=0, last_message="正常", last_checked_at=datetime.now(ZoneInfo("Asia/Shanghai"))))
    engine = InspectionEngine(settings, database)
    mailer = Mailer(settings, database, engine)
    subject, html = mailer.render_daily()
    assert "个人服务日报" in subject
    assert "Health" in html
    assert "总体结论" in html


async def test_incident_email_disabled_by_default(settings_factory, monkeypatch):
    settings = settings_factory()
    database = Database(settings.database_url)
    database.create_all()
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    with database.session() as session:
        incident = Incident(
            check_id="x",
            name="示例服务",
            category="service",
            severity="CRITICAL",
            status="OPEN",
            first_seen_at=now,
            opened_at=now,
            last_seen_at=now,
            last_error="失败",
        )
        session.add(incident)
        session.flush()
        incident_id = incident.id

    mailer = Mailer(settings, database)

    def fail_send(*_args, **_kwargs):
        raise AssertionError("incident email should not be sent")

    monkeypatch.setattr(mailer, "_send", fail_send)
    assert await mailer.send_incident(incident_id) is False
    assert await mailer.send_recovery(incident_id) is False
