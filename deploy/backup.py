"""Online SQLite backup and protected configuration snapshot before a release."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def backup(shared: Path, release: str) -> Path:
    os.umask(0o077)
    target = shared / "backups" / datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y/%m/%d") / release
    target.mkdir(parents=True, exist_ok=False)
    config = shared / "config/app.json"
    if config.exists():
        settings = json.loads(config.read_text(encoding="utf-8"))
        url = settings["storage"]["database_url"]
        if not url.startswith("sqlite:///"):
            raise ValueError("Deployment backup requires SQLite")
        db_path = Path(url.removeprefix("sqlite:///"))
        if not db_path.is_absolute():
            db_path = shared / db_path
        if db_path.exists():
            with sqlite3.connect(db_path) as source, sqlite3.connect(target / "database.sqlite3") as dest:
                source.backup(dest)
    for relative in ["config/app.json", "config/checks.yaml", "data/password.txt", ".env"]:
        source = shared / relative
        if source.exists():
            dest = target / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
    return target


def wait_idle(shared: Path, seconds=600):
    settings = json.loads((shared / "config/app.json").read_text(encoding="utf-8"))
    path = Path(settings["storage"]["database_url"].removeprefix("sqlite:///"))
    if not path.is_absolute():
        path = shared / path
    if not path.exists():
        return
    until = time.monotonic() + seconds
    while True:
        with sqlite3.connect(path) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            active = 0
            for table in ["task_runs", "ai_jobs"]:
                if table in tables:
                    active += db.execute(f"SELECT count(*) FROM {table} WHERE status='RUNNING'").fetchone()[0]
        if not active:
            return
        if time.monotonic() >= until:
            raise TimeoutError("Running tasks did not finish; release not switched")
        time.sleep(5)


if __name__ == "__main__":
    shared = Path(sys.argv[1]).resolve()
    if sys.argv[2] == "wait-idle":
        wait_idle(shared)
    else:
        print("Protected backup:", backup(shared, sys.argv[2]))
