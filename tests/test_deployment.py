from __future__ import annotations

import json
import runpy
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_systemd_runs_release_virtualenv_without_docker():
    unit = (ROOT / "deploy" / "personal-system-inspector.service").read_text(encoding="utf-8")
    assert "current/.venv/bin/uvicorn" in unit
    assert "current/version.env" in unit
    assert "User=inspector" in unit
    assert "Restart=on-failure" in unit
    assert "docker" not in unit.lower()
    assert not (ROOT / "Dockerfile").exists()
    assert not (ROOT / "docker-compose.yml").exists()


def test_deployment_keeps_current_plus_two_historical_releases():
    script = (ROOT / "deploy" / "jenkins-deploy.sh").read_text(encoding="utf-8")
    assert "docker" not in script.lower()
    assert "HISTORY_RELEASES_TO_KEEP=2" in script
    assert '== "$current_release"' in script
    assert "historical_kept -gt $HISTORY_RELEASES_TO_KEEP" in script


def test_jenkins_uses_python_virtualenv_for_quality_gates():
    pipeline = (ROOT / "src" / "JenkinsConfig" / "Jenkinsfile").read_text(encoding="utf-8")
    assert "python3.12 -m venv .test-venv" in pipeline
    assert ".test-venv/bin/pytest -q" in pipeline
    assert "docker build" not in pipeline
    assert "docker run" not in pipeline


def test_online_backup_preserves_original_data_and_configuration(tmp_path):
    shared = tmp_path / "shared"
    (shared / "config").mkdir(parents=True)
    (shared / "data").mkdir()
    (shared / "config/app.json").write_text(json.dumps({"storage": {"database_url": "sqlite:///data/app.sqlite3"}}), encoding="utf-8")
    (shared / "data/password.txt").write_text("synthetic-test-account", encoding="utf-8")
    with sqlite3.connect(shared / "data/app.sqlite3") as db:
        db.execute("CREATE TABLE sample(value TEXT)")
        db.execute("INSERT INTO sample VALUES ('original')")
    target = runpy.run_path(str(ROOT / "deploy/backup.py"))["backup"](shared, "test-release")
    with sqlite3.connect(shared / "data/app.sqlite3") as db:
        db.execute("UPDATE sample SET value='later'")
    with sqlite3.connect(target / "database.sqlite3") as db:
        assert db.execute("SELECT value FROM sample").fetchone()[0] == "original"
    assert (target / "config/app.json").read_bytes() == (shared / "config/app.json").read_bytes()
    assert (target / "data/password.txt").read_bytes() == (shared / "data/password.txt").read_bytes()
