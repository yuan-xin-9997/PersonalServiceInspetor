from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def test_health_checks_and_authentication(settings_factory):
    app = create_app(settings_factory(), start_scheduler=False)
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        ready = client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json()["database"] is True
        assert client.get("/api/summary").status_code == 401
        response = client.post("/login", data={"username": "admin", "password": "test-password"}, follow_redirects=False)
        assert response.status_code == 303
        assert client.get("/api/summary").status_code == 200
        assert client.get("/api/config").status_code == 200


def test_manual_run_requires_admin(settings_factory):
    app = create_app(settings_factory(), start_scheduler=False)
    with TestClient(app) as client:
        client.post("/login", data={"username": "viewer", "password": "view-password"})
        assert client.post("/api/run").status_code == 401
