from __future__ import annotations

import json

import pytest
import yaml

from app.config import load_settings


def test_config_parses_and_exposes_safe_view(settings_factory):
    settings = settings_factory()
    assert settings.timezone == "Asia/Shanghai"
    assert settings.checks[0]["id"] == "health"
    assert settings.public_view()["checks"][0]["url"].startswith("https://")


def test_config_rejects_duplicate_ids(tmp_path):
    app = {"app": {}, "storage": {"database_url": "sqlite://"}}
    checks = {"checks": [{"id": "x", "name": "a", "category": "service", "type": "tcp"}, {"id": "x", "name": "b", "category": "service", "type": "tcp"}]}
    (tmp_path / "app.json").write_text(json.dumps(app), encoding="utf-8")
    (tmp_path / "checks.yaml").write_text(yaml.safe_dump(checks), encoding="utf-8")
    with pytest.raises(ValueError, match="重复检查项"):
        load_settings(tmp_path / "app.json", tmp_path / "checks.yaml")
