from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

VALID_STATUSES = {"OK", "WARNING", "CRITICAL", "UNKNOWN"}


@dataclass(slots=True)
class CheckResult:
    check_id: str
    name: str
    category: str
    check_type: str
    status: str
    message: str
    observed_at: datetime
    latency_ms: float | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in VALID_STATUSES:
            raise ValueError(f"无效状态: {self.status}")

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["observed_at"] = self.observed_at.isoformat()
        return value
