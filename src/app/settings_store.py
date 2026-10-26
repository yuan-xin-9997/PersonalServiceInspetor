from __future__ import annotations

import copy
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .config import Settings
from .database import CheckSetting, Database, InspectionSetting

SCHEDULE_KEY = "schedule"
LIMITS_KEY = "limits"

SCHEDULE_MODES = {"interval", "cron"}

LIMIT_RULES: dict[str, tuple[float, float, type]] = {
    "timeout_seconds": (1, 120, float),
    "failure_threshold": (1, 10, int),
    "warning_response_ms": (100, 60_000, int),
    "tls_warning_days": (1, 365, int),
    "tls_critical_days": (1, 365, int),
    "disk_warning_percent": (1, 100, int),
    "disk_critical_percent": (1, 100, int),
    "inode_warning_percent": (1, 100, int),
    "max_log_bytes": (1024, 52_428_800, int),
}

SCHEDULE_FIELDS = {
    "enabled",
    "mode",
    "interval_minutes",
    "times",
    "window_start",
    "window_end",
    "daily_report_enabled",
    "daily_report_time",
    "run_on_startup",
}


def parse_time(value: Any, field: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text, "%H:%M")
    except ValueError as exc:
        raise ValueError(f"{field} 必须是 HH:MM 格式") from exc
    return parsed.strftime("%H:%M")


def time_parts(value: str) -> tuple[int, int]:
    hour, minute = value.split(":", 1)
    return int(hour), int(minute)


def _number(value: Any, field: str, cast: type, minimum: float, maximum: float) -> int | float:
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是数字")
    try:
        parsed = cast(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是数字") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{field} 必须在 {minimum:g} 到 {maximum:g} 之间")
    return parsed


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} 必须是布尔值")
    return value


class SettingsStore:
    """Database-backed runtime settings that survive code deployments.

    The versioned YAML/JSON files stay the source of defaults.  Anything an
    administrator changes in the web UI is written to SQLite instead, so a new
    release never silently re-enables a disabled check or resets the schedule.
    """

    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings

    # ------------------------------------------------------------------ storage
    def _load(self, key: str) -> dict[str, Any]:
        with self.database.session() as session:
            row = session.get(InspectionSetting, key)
            return copy.deepcopy(row.value) if row and isinstance(row.value, dict) else {}

    def _save(self, key: str, value: dict[str, Any]) -> None:
        now = datetime.now(ZoneInfo(self.settings.timezone))
        with self.database.session() as session:
            row = session.get(InspectionSetting, key)
            if row is None:
                session.add(InspectionSetting(key=key, value=value, updated_at=now))
            else:
                row.value = value
                row.updated_at = now

    # ----------------------------------------------------------------- schedule
    def default_schedule(self) -> dict[str, Any]:
        source = self.settings.scheduler
        hour = int(source.get("daily_report_hour", 9))
        minute = int(source.get("daily_report_minute", 0))
        default_times = source.get("inspection_times") or ["09:00"]
        return {
            "enabled": bool(source.get("enabled", True)),
            "mode": str(source.get("mode", "interval")),
            "interval_minutes": int(source.get("inspection_interval_minutes", 15)),
            "times": [parse_time(value, "inspection_times") for value in default_times],
            "window_start": parse_time(source.get("inspection_window_start", "00:00"), "inspection_window_start"),
            "window_end": parse_time(source.get("inspection_window_end", "23:59"), "inspection_window_end"),
            "daily_report_enabled": bool(source.get("daily_report_enabled", True)),
            "daily_report_time": f"{hour:02d}:{minute:02d}",
            "run_on_startup": bool(source.get("run_on_startup", True)),
        }

    def schedule(self) -> dict[str, Any]:
        value = self.default_schedule()
        value.update(self._load(SCHEDULE_KEY))
        return self._validate_schedule(value)

    def update_schedule(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("调度配置必须是对象")
        unknown = set(payload) - SCHEDULE_FIELDS
        if unknown:
            raise ValueError(f"不支持的调度字段: {', '.join(sorted(unknown))}")
        merged = self.schedule()
        merged.update(payload)
        value = self._validate_schedule(merged)
        self._save(SCHEDULE_KEY, value)
        return value

    def _validate_schedule(self, value: dict[str, Any]) -> dict[str, Any]:
        mode = str(value.get("mode", "interval")).lower()
        if mode not in SCHEDULE_MODES:
            raise ValueError("巡检模式只能是 interval 或 cron")
        times = value.get("times", [])
        if isinstance(times, str):
            times = [item.strip() for item in times.split(",") if item.strip()]
        if not isinstance(times, list) or not times:
            raise ValueError("定时巡检模式至少需要一个时间点")
        normalized_times = sorted({parse_time(item, "巡检时间点") for item in times})
        if len(normalized_times) > 24:
            raise ValueError("巡检时间点不能超过 24 个")
        return {
            "enabled": _boolean(value.get("enabled", True), "enabled"),
            "mode": mode,
            "interval_minutes": int(_number(value.get("interval_minutes", 15), "interval_minutes", int, 1, 1440)),
            "times": normalized_times,
            "window_start": parse_time(value.get("window_start", "00:00"), "window_start"),
            "window_end": parse_time(value.get("window_end", "23:59"), "window_end"),
            "daily_report_enabled": _boolean(value.get("daily_report_enabled", True), "daily_report_enabled"),
            "daily_report_time": parse_time(value.get("daily_report_time", "09:00"), "daily_report_time"),
            "run_on_startup": _boolean(value.get("run_on_startup", True), "run_on_startup"),
        }

    # ------------------------------------------------------------------- limits
    def default_limits(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, (_minimum, _maximum, cast) in LIMIT_RULES.items():
            fallback = {"timeout_seconds": 8, "failure_threshold": 3, "warning_response_ms": 3000, "tls_warning_days": 30, "tls_critical_days": 7, "disk_warning_percent": 80, "disk_critical_percent": 90, "inode_warning_percent": 80, "max_log_bytes": 5_242_880}[key]
            result[key] = cast(self.settings.defaults.get(key, fallback))
        return result

    def limits(self) -> dict[str, Any]:
        value = self.default_limits()
        value.update(self._load(LIMITS_KEY))
        return self._validate_limits(value)

    def update_limits(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("巡检参数必须是对象")
        unknown = set(payload) - set(LIMIT_RULES)
        if unknown:
            raise ValueError(f"不支持的巡检参数: {', '.join(sorted(unknown))}")
        merged = self.limits()
        merged.update(payload)
        value = self._validate_limits(merged)
        self._save(LIMITS_KEY, value)
        return value

    def _validate_limits(self, value: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, (minimum, maximum, cast) in LIMIT_RULES.items():
            result[key] = _number(value.get(key, self.default_limits()[key]), key, cast, minimum, maximum)
            result[key] = int(result[key]) if cast is int else float(result[key])
        if result["tls_critical_days"] >= result["tls_warning_days"]:
            raise ValueError("TLS 严重阈值必须小于预警阈值")
        if result["disk_critical_percent"] <= result["disk_warning_percent"]:
            raise ValueError("磁盘严重阈值必须大于预警阈值")
        return result

    def effective_defaults(self) -> dict[str, Any]:
        return {**self.settings.defaults, **self.limits()}

    # -------------------------------------------------------------- check toggles
    def enabled_map(self) -> dict[str, bool]:
        with self.database.session() as session:
            rows = list(session.scalars(select(CheckSetting)))
        return {row.check_id: bool(row.enabled) for row in rows}

    def configured_checks(self) -> list[dict[str, Any]]:
        mapping = self.enabled_map()
        return [
            {**copy.deepcopy(item), "enabled": mapping.get(item["id"], True)}
            for item in self.settings.checks
        ]

    def enabled_checks(self) -> list[dict[str, Any]]:
        return [item for item in self.configured_checks() if item["enabled"]]

    def set_check_enabled(self, check_id: str, enabled: bool) -> None:
        if check_id not in {item["id"] for item in self.settings.checks}:
            raise KeyError(check_id)
        now = datetime.now(ZoneInfo(self.settings.timezone))
        with self.database.session() as session:
            row = session.get(CheckSetting, check_id)
            if row is None:
                session.add(CheckSetting(check_id=check_id, enabled=enabled, updated_at=now))
            else:
                row.enabled = enabled
                row.updated_at = now

    def set_checks_enabled(self, check_ids: list[str], enabled: bool) -> None:
        known = {item["id"] for item in self.settings.checks}
        unknown = [item for item in check_ids if item not in known]
        if unknown:
            raise KeyError(", ".join(unknown))
        for check_id in check_ids:
            self.set_check_enabled(check_id, enabled)
