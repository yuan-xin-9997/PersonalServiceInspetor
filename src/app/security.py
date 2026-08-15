from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .database import Database, UserPermission


@dataclass(slots=True)
class User:
    username: str
    password: str
    role: str


class UserStore:
    def __init__(self, password_file: str | Path, database: Database, timezone: str):
        self.password_file = Path(password_file)
        self.database = database
        self.zone = ZoneInfo(timezone)

    def users(self) -> dict[str, User]:
        result: dict[str, User] = {}
        if not self.password_file.exists():
            return result
        for raw_line in self.password_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(":", 2)
            if len(parts) == 3 and parts[2] in {"admin", "user"}:
                result[parts[0]] = User(parts[0], parts[1], parts[2])
        return result

    def sync_permissions(self) -> None:
        now = datetime.now(self.zone)
        with self.database.session() as session:
            for user in self.users().values():
                permission = session.get(UserPermission, user.username)
                if permission is None:
                    pages = ["*"] if user.role == "admin" else ["dashboard"]
                    session.add(UserPermission(username=user.username, role=user.role, pages=pages, updated_at=now))
                elif permission.role != user.role:
                    permission.role = user.role
                    permission.updated_at = now

    def authenticate(self, username: str, password: str) -> User | None:
        user = self.users().get(username)
        if user and hmac.compare_digest(user.password.encode(), password.encode()):
            return user
        return None

    def permission(self, username: str) -> UserPermission | None:
        with self.database.session() as session:
            return session.scalar(select(UserPermission).where(UserPermission.username == username))


def app_secret() -> str:
    value = os.getenv("APP_SECRET_KEY", "")
    if len(value) < 32:
        if os.getenv("PYTEST_CURRENT_TEST"):
            return "test-secret-key-that-is-long-enough-123"
        raise RuntimeError("APP_SECRET_KEY 必须至少 32 个字符")
    return value
