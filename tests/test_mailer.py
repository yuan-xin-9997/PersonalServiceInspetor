from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.database import CheckState, Database
from app.engine import InspectionEngine
from app.mailer import Mailer


def test_daily_email_render(settings_factory):
    settings = settings_factory()
    database = Database(settings.database_url)
    database.create_all()
    with database.session() as session:
        session.add(CheckState(check_id="x", name="示例服务", category="service", current_status="OK", consecutive_failures=0, last_message="正常", last_checked_at=datetime.now(ZoneInfo("Asia/Shanghai"))))
    engine = InspectionEngine(settings, database)
    mailer = Mailer(settings, database, engine)
    subject, html = mailer.render_daily()
    assert "个人服务日报" in subject
    assert "示例服务" in html
    assert "总体结论" in html
