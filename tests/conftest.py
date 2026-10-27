from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from app.config import load_settings

os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-that-is-definitely-long-enough")


@pytest.fixture
def settings_factory(tmp_path: Path):
    def factory(checks: list[dict] | None = None, **scheduler_overrides):
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)
        password_file = data_dir / "password.txt"
        password_file.write_text("admin:test-password:admin\nviewer:view-password:user\n", encoding="utf-8")
        app_data = {
            "app": {"name": "测试巡检", "version": "test", "timezone": "Asia/Shanghai", "public_base_url": "https://testserver"},
            "storage": {"database_url": f"sqlite:///{(data_dir / 'test.sqlite3').as_posix()}", "report_directory": str(data_dir / "reports")},
            "scheduler": {"enabled": False, "run_on_startup": False, **scheduler_overrides},
            "email": {"send_incident_notifications": False},
            "security": {"password_file": str(password_file), "session_max_age_seconds": 3600},
            "logging": {"path": str(tmp_path / "logs" / "app.log"), "backup_count": 2},
        }
        checks_data = {
            "defaults": {"failure_threshold": 3, "warning_response_ms": 3000, "timeout_seconds": 2},
            "checks": checks
            or [{"id": "health", "name": "Health", "category": "service", "type": "http", "url": "https://example.test/health", "expected_status": [200]}],
        }
        app_path = tmp_path / "app.json"
        checks_path = tmp_path / "checks.yaml"
        app_path.write_text(json.dumps(app_data, ensure_ascii=False), encoding="utf-8")
        checks_path.write_text(yaml.safe_dump(checks_data, allow_unicode=True), encoding="utf-8")
        return load_settings(app_path, checks_path)

    return factory
