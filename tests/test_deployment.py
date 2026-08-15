from __future__ import annotations

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
