from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


def _env(name: str | None, default: Any = None) -> Any:
    return os.getenv(name, default) if name else default


@dataclass(slots=True)
class Settings:
    app: dict[str, Any]
    storage: dict[str, Any]
    scheduler: dict[str, Any]
    security: dict[str, Any]
    logging: dict[str, Any]
    defaults: dict[str, Any]
    checks: list[dict[str, Any]]
    app_config_path: Path
    checks_config_path: Path

    @property
    def database_url(self) -> str:
        return str(self.storage["database_url"])

    @property
    def timezone(self) -> str:
        return str(self.app.get("timezone", "Asia/Shanghai"))

    @property
    def version(self) -> str:
        return os.getenv("APP_VERSION", str(self.app.get("version", "dev")))

    def public_view(self) -> dict[str, Any]:
        """Return safe, non-secret configuration for the admin page."""
        return {
            "app": self.app,
            "storage": {"database_url": self.storage.get("database_url", "").split("?")[0]},
            "scheduler": self.scheduler,
            "defaults": self.defaults,
            "checks": [
                {
                    key: value
                    for key, value in check.items()
                    if not key.endswith("password") and not key.endswith("token")
                }
                for check in self.checks
            ],
        }


def load_settings(
    app_path: str | Path | None = None,
    checks_path: str | Path | None = None,
) -> Settings:
    app_file = Path(app_path or os.getenv("APP_CONFIG_PATH", "config/app.json"))
    checks_file = Path(checks_path or os.getenv("CHECKS_CONFIG_PATH", "config/checks.yaml"))
    with app_file.open(encoding="utf-8") as handle:
        app_data = json.load(handle)
    with checks_file.open(encoding="utf-8") as handle:
        checks_data = yaml.safe_load(handle) or {}

    checks = checks_data.get("checks", [])
    if not isinstance(checks, list):
        raise ValueError("checks.yaml: checks 必须是列表")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for source in checks:
        if not isinstance(source, dict):
            raise ValueError("checks.yaml: 每个检查项必须是对象")
        item = deepcopy(source)
        for required in ("id", "name", "category", "type"):
            if not item.get(required):
                raise ValueError(f"检查项缺少字段: {required}")
        if item["id"] in seen:
            raise ValueError(f"重复检查项 id: {item['id']}")
        seen.add(item["id"])
        normalized.append(item)

    return Settings(
        app=app_data.get("app", {}),
        storage=app_data.get("storage", {}),
        scheduler=app_data.get("scheduler", {}),
        security=app_data.get("security", {}),
        logging=app_data.get("logging", {}),
        defaults=checks_data.get("defaults", {}),
        checks=normalized,
        app_config_path=app_file,
        checks_config_path=checks_file,
    )


def env_value(item: dict[str, Any], key: str, default: Any = None) -> Any:
    """Resolve a check property from either a literal or an environment variable name."""
    env_name = item.get(f"{key}_env")
    if env_name:
        return _env(str(env_name), default)
    return item.get(key, default)
