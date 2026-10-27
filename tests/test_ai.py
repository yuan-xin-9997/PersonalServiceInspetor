from __future__ import annotations

import base64
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.ai_service import SCOPES, digest, now
from app.database import AICredential, AIJob, AISecret, AuditEvent, UserPermission
from app.main import create_app
from app.schemas import CheckResult


def login(client, username="admin", password="test-password"):
    client.post("/login", data={"username": username, "password": password})
    return client.get("/api/ai/info").json().get("csrf")


def create(client, csrf, scopes=None, **extra):
    return client.post(
        "/api/ai/credentials",
        headers={"X-CSRF-Token": csrf},
        json={
            "name": "test",
            "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "scopes": scopes or ["status:read"],
            **extra,
        },
    )


def test_credential_security_lifecycle_and_live_permissions(settings_factory):
    app = create_app(settings_factory(), start_scheduler=False)
    with TestClient(app) as c:
        for authorization in [None, "Bearer", "Bearer ", "Bearer wrong"]:
            headers = {"Authorization": authorization} if authorization else {}
            assert c.get("/api/v1/summary", headers=headers).status_code == 401
            assert c.post("/mcp", headers=headers, json={}).status_code == 401
        csrf = login(c)
        assert c.post("/api/ai/credentials", json={}).status_code == 403
        r = create(c, csrf)
        assert r.status_code == 201
        assert r.headers["cache-control"] == "no-store"
        key, cid = r.json()["secret"], r.json()["id"]
        h = {"Authorization": "Bearer " + key}
        assert c.get("/api/v1/summary", headers=h).json()["configured_total"] == 1
        assert c.get("/api/v1/checks", headers=h).status_code == 403
        assert c.post("/mcp", headers=h, json={}).status_code == 401
        listing = c.get("/api/ai/credentials").json()
        assert key not in str(listing) and "secret" not in str(listing)
        token = c.post("/api/v1/token", headers=h).json()["access_token"]
        assert (
            c.get("/api/v1/summary", headers={"Authorization": "Bearer " + token}).status_code
            == 200
        )
        assert (
            c.delete("/api/ai/credentials/" + cid, headers={"X-CSRF-Token": csrf}).status_code
            == 200
        )
        for value in [key, token]:
            assert (
                c.get("/api/v1/summary", headers={"Authorization": "Bearer " + value}).status_code
                == 401
            )
        with app.state.runtime.database.session() as s:
            assert s.get(AICredential, cid).digest == digest(key)
            assert key not in str([v.__dict__ for v in s.query(AuditEvent).all()])
        expired = create(c, csrf).json()
        with app.state.runtime.database.session() as s:
            s.get(AICredential, expired["id"]).expires_at = now() - 1
        assert (
            c.get(
                "/api/v1/summary", headers={"Authorization": "Bearer " + expired["secret"]}
            ).status_code
            == 401
        )

        with app.state.runtime.database.session() as s:
            s.get(UserPermission, "viewer").pages = ["ai", "dashboard"]
        csrf_viewer = login(c, "viewer", "view-password")
        assert create(c, csrf_viewer, ["checks:write"]).status_code == 403
        assert create(c, csrf_viewer, ["status:read"], username="admin").status_code == 422
        assert (
            c.delete(
                "/api/ai/credentials/" + cid, headers={"X-CSRF-Token": csrf_viewer}
            ).status_code
            == 404
        )
        viewer = create(c, csrf_viewer).json()
        vh = {"Authorization": "Bearer " + viewer["secret"]}
        assert c.get("/api/v1/summary", headers=vh).status_code == 200
        with app.state.runtime.database.session() as s:
            s.get(UserPermission, "viewer").pages = ["ai"]
        assert c.get("/api/v1/summary", headers=vh).status_code == 403
        app.state.runtime.users.password_file.write_text(
            "admin:test-password:admin\n", encoding="utf-8"
        )
        assert c.get("/api/v1/summary", headers=vh).status_code == 401
        assert c.get("/api/ai/credentials").status_code == 401


def test_ai_validation_idempotency_concurrency_and_restart(settings_factory):
    settings = settings_factory()
    app = create_app(settings, start_scheduler=False)
    with TestClient(app) as c:
        csrf = login(c)
        cred = create(c, csrf, list(SCOPES)).json()
        h = {"Authorization": "Bearer " + cred["secret"], "Idempotency-Key": "same-toggle"}
        assert c.get("/api/v1/checks?limit=0", headers=h).status_code == 422
        assert c.put("/api/v1/checks/health", headers=h, json={"enabled": True}).status_code == 422
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(
                pool.map(
                    lambda _: c.put(
                        "/api/v1/checks/health", headers=h, json={"enabled": True, "confirm": True}
                    ),
                    range(8),
                )
            )
        assert all(r.status_code == 200 for r in responses)
        assert len({r.text for r in responses}) == 1
        assert (
            c.put(
                "/api/v1/checks/health", headers=h, json={"enabled": False, "confirm": True}
            ).status_code
            == 409
        )
        assert (
            c.put(
                "/api/v1/checks/missing",
                headers={**h, "Idempotency-Key": "missing"},
                json={"enabled": True, "confirm": True},
            ).status_code
            == 404
        )

        async def fake_run(trigger):
            return {"status": "SUCCESS", "counts": {"OK": 1}}

        app.state.runtime.engine.run_all = fake_run
        with ThreadPoolExecutor(max_workers=4) as pool:
            runs = list(
                pool.map(
                    lambda _: c.post(
                        "/api/v1/tasks", headers={**h, "Idempotency-Key": "same-run"}, json={}
                    ),
                    range(8),
                )
            )
        assert all(r.status_code == 202 for r in runs)
        assert len({r.json()["id"] for r in runs}) == 1
        job_id = runs[0].json()["id"]
        assert c.get("/api/v1/tasks/" + job_id, headers=h).json()["status"] == "SUCCESS"
        assert c.get("/api/v1/tasks?limit=1", headers=h).json()["total"] == 1

        async def fail(trigger):
            raise RuntimeError("secret upstream error must never be returned")

        app.state.runtime.engine.run_all = fail
        failed = c.post(
            "/api/v1/tasks", headers={**h, "Idempotency-Key": "failed"}, json={}
        ).json()["id"]
        status = c.get("/api/v1/tasks/" + failed, headers=h).json()
        assert status["status"] == "FAILED" and "secret upstream" not in str(status)
        with app.state.runtime.database.session() as s:
            s.add(
                AIJob(
                    id="interrupted",
                    username="admin",
                    credential_id=cred["id"],
                    status="RUNNING",
                    created_at=now(),
                )
            )
    app2 = create_app(settings, start_scheduler=False)
    with TestClient(app2) as c:
        assert c.get("/api/v1/tasks/interrupted", headers=h).json()["status"] == "FAILED"
        assert c.get("/api/v1/tasks/" + job_id, headers=h).json()["status"] == "SUCCESS"
        assert (
            c.post("/api/v1/tasks", headers={**h, "Idempotency-Key": "same-run"}, json={}).json()[
                "id"
            ]
            == job_id
        )


def test_rate_limit_and_origin(settings_factory):
    settings = settings_factory()
    settings.security["ai_rate_limit_per_minute"] = 2
    app = create_app(settings, start_scheduler=False)
    with TestClient(app) as c:
        assert c.get("/api/v1/summary").status_code == 401
        assert c.get("/api/v1/summary").status_code == 401
        assert c.get("/api/v1/summary").status_code == 429
        assert (
            c.post(
                "/login",
                headers={"Origin": "https://evil.test"},
                data={"username": "admin", "password": "test-password"},
            ).status_code
            == 403
        )


def test_oauth_pkce_refresh_resource_binding_and_revoke(settings_factory):
    app = create_app(settings_factory(), start_scheduler=False)
    with TestClient(app, base_url="https://testserver") as c:
        metadata = c.get("/.well-known/oauth-protected-resource/mcp").json()
        assert metadata["resource"] == "https://testserver/mcp"
        assert c.get("/.well-known/oauth-authorization-server").json()[
            "code_challenge_methods_supported"
        ] == ["S256"]
        registered = c.post(
            "/register",
            json={
                "redirect_uris": ["http://127.0.0.1:9876/callback"],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "scope": "status:read",
            },
        )
        assert registered.status_code == 201, registered.text
        client_id = registered.json()["client_id"]
        verifier = "a" * 48
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "http://127.0.0.1:9876/callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "status:read",
            "resource": "https://testserver/mcp",
            "state": "test-state",
        }
        assert (
            c.get(
                "/authorize",
                params={**params, "resource": "https://evil.test/mcp"},
                follow_redirects=False,
            ).status_code
            == 302
        )
        r = c.get("/authorize", params=params, follow_redirects=False)
        pending_url = r.headers["location"]
        assert c.get(pending_url, follow_redirects=False).status_code == 303
        login(c)
        consent = c.get(pending_url)
        assert consent.status_code == 200
        import re

        csrf = re.search('name="csrf" value="([^"]+)"', consent.text)[1]
        approved = c.post(
            pending_url,
            data={"csrf": csrf, "scopes": "status:read", "days": "1"},
            follow_redirects=False,
        )
        assert approved.status_code == 303, approved.text
        code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]
        data = {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": params["redirect_uri"],
            "resource": params["resource"],
        }
        assert c.post("/token", data={**data, "code_verifier": "bad"}).status_code == 400
        granted = c.post("/token", data=data)
        assert granted.status_code == 200, granted.text
        assert c.post("/token", data=data).status_code == 400
        refresh = granted.json()["refresh_token"]
        renewal = c.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh,
                "resource": params["resource"],
            },
        )
        assert renewal.status_code == 200, renewal.text
        assert (
            c.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "refresh_token": refresh,
                },
            ).status_code
            == 400
        )
        assert (
            c.post(
                "/revoke", data={"client_id": client_id, "token": renewal.json()["access_token"]}
            ).status_code
            == 200
        )
        assert (
            c.get(
                "/api/v1/summary",
                headers={"Authorization": "Bearer " + renewal.json()["access_token"]},
            ).status_code
            == 401
        )


async def test_real_sdk_initialize_discover_read_and_safe_write(settings_factory):
    app = create_app(settings_factory(), start_scheduler=False)
    async with app.router.lifespan_context(app):
        service = app.state.runtime.ai
        cred = service.create_credential("admin", "SDK", list(SCOPES), now() + 3600)
        p = service.authenticate(cred["secret"])
        token = service.issue_tokens(p, "sdk-test", "https://testserver/mcp")["access_token"]
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://testserver",
                headers={"Authorization": "Bearer " + token},
            ) as http,
            streamable_http_client("https://testserver/mcp", http_client=http) as streams,
            ClientSession(streams[0], streams[1]) as client,
        ):
            init = await client.initialize()
            assert init.server_info.name == "个人系统巡检中心"
            tools = await client.list_tools()
            assert len(tools.tools) == 9
            read = await client.call_tool("get_summary", {})
            assert not read.is_error
            write = await client.call_tool(
                "set_check_enabled",
                {
                    "check_id": "health",
                    "enabled": True,
                    "confirm": True,
                    "idempotency_key": "sdk-safe-write",
                },
            )
            assert not write.is_error
            bad = await client.call_tool("list_checks", {"limit": 0})
            assert bad.is_error
            service.revoke("admin", cred["id"])
            assert (await http.get("/api/v1/summary")).status_code == 401


async def test_recover_queued_job_results_and_owner_isolation(settings_factory):
    settings = settings_factory()
    app = create_app(settings, start_scheduler=False)
    async with app.router.lifespan_context(app):
        service = app.state.runtime.ai
        issued = service.create_credential("admin", "queue", list(SCOPES), now() + 3600)
        with service.db.session() as s:
            s.add(AIJob(id="queued", username="admin", credential_id=issued["id"], status="QUEUED", created_at=now()))
    app = create_app(settings, start_scheduler=False)

    class Runner:
        async def run(self, item):
            return CheckResult(item["id"], item["name"], item["category"], item["type"], "OK", "test", datetime.now(UTC), 1.0)

    app.state.runtime.engine.runner = Runner()
    async with app.router.lifespan_context(app):
        service = app.state.runtime.ai
        await service.workers["queued"]
        p = service.authenticate(issued["secret"])
        result = service.task_results(p, "queued", 0, 1)
        assert result["task_status"] == "SUCCESS" and result["total"] == 1
        assert result["items"][0]["check_id"] == "health"
        with service.db.session() as s:
            s.get(UserPermission, "viewer").pages = ["ai", "tasks"]
        other = service.create_credential("viewer", "other", ["tasks:read"], now() + 3600)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as http:
            assert (await http.get("/api/v1/tasks/queued", headers={"Authorization": "Bearer " + other["secret"]})).status_code == 404


def test_expired_token_admin_downgrade_and_metadata_no_plaintext(settings_factory):
    app = create_app(settings_factory(), start_scheduler=False)
    with TestClient(app) as c:
        csrf = login(c)
        issued = create(c, csrf, list(SCOPES)).json()
        h = {"Authorization": "Bearer " + issued["secret"]}
        grant = c.post("/api/v1/token", headers=h).json()
        token = grant["access_token"]
        with app.state.runtime.database.session() as s:
            s.get(AISecret, digest(token)).expires_at = now() - 1
        assert c.get("/api/v1/summary", headers={"Authorization": "Bearer " + token}).status_code == 401
        app.state.runtime.users.password_file.write_text("admin:test-password:user\nviewer:view-password:user\n", encoding="utf-8")
        assert c.put("/api/v1/checks/health", headers={**h, "Idempotency-Key": "downgraded"}, json={"enabled": True, "confirm": True}).status_code == 403
        assert c.get("/api/config").status_code == 403
        assert c.get("/api/v1/summary").status_code == 401


def test_browser_test_protocol_and_paginated_credentials(settings_factory):
    app = create_app(settings_factory(), start_scheduler=False)
    with TestClient(app, base_url="https://testserver") as c:
        csrf = login(c)
        issued = create(c, csrf).json()
        other = create(c, csrf).json()
        listing = c.get("/api/ai/credentials?offset=1&limit=1").json()
        assert listing["total"] == 2 and len(listing["items"]) == 1
        assert issued["secret"] not in str(listing) and other["secret"] not in str(listing)
        grant = c.post("/api/v1/token", headers={"Authorization": "Bearer " + issued["secret"]}).json()
        headers = {"Authorization": "Bearer " + grant["access_token"], "Accept": "application/json, text/event-stream"}
        def rpc(method, params):
            r = c.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            assert r.status_code == 200
            return r.json()["result"]
        initialized = rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "browser-test", "version": "1"}})
        headers["MCP-Protocol-Version"] = initialized["protocolVersion"]
        assert len(rpc("tools/list", {})["tools"]) == 9
        assert not rpc("tools/call", {"name": "get_summary", "arguments": {}}).get("isError", False)
        assert c.post("/api/v1/token", headers=headers).status_code == 401


def test_production_tls_boundary_preserves_legacy_web(settings_factory, monkeypatch):
    monkeypatch.setenv("AI_REQUIRE_HTTPS", "true")
    app = create_app(settings_factory(), start_scheduler=False)
    with TestClient(app) as c:
        assert c.get("/health/ready").status_code == 200
        csrf = login(c)
        assert c.get("/api/summary").status_code == 200
        assert c.get("/api/ai/info").json()["require_https"] is True
        assert create(c, csrf).status_code == 403
        for path in ["/mcp", "/token", "/revoke", "/register", "/api/v1/token"]:
            assert c.post(path, json={}).status_code == 403
    with TestClient(create_app(app.state.runtime.settings, start_scheduler=False), base_url="https://testserver") as c:
        csrf = login(c)
        assert create(c, csrf).status_code == 201
