from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from app.checkers import CheckRunner, evaluate_json_assertions, parse_log_window


@pytest.mark.asyncio
@respx.mock
async def test_http_check_success_and_latency():
    route = respx.get("https://example.test/health").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    runner = CheckRunner({"warning_response_ms": 3000}, "Asia/Shanghai")
    result = await runner.run({"id": "h", "name": "health", "category": "service", "type": "http", "url": "https://example.test/health", "expected_status": [200], "json_assertions": [{"path": "status", "equals": "ok"}]})
    assert route.called
    assert result.status == "OK"
    assert result.metrics["http_status"] == 200


@pytest.mark.asyncio
@respx.mock
async def test_http_check_fails_status_and_json_assertion():
    respx.get("http://example.test/bad").mock(return_value=httpx.Response(200, json={"status": "bad"}))
    runner = CheckRunner({}, "Asia/Shanghai")
    result = await runner.run({"id": "bad", "name": "bad", "category": "service", "type": "http", "url": "http://example.test/bad", "expected_status": [200], "json_assertions": [{"path": "status", "equals": "ok"}]})
    assert result.status == "CRITICAL"
    assert "期望" in result.message


@pytest.mark.asyncio
async def test_http_check_ignores_inherited_proxy(monkeypatch):
    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def request(self, *_args, **_kwargs):
            return httpx.Response(200)

        async def aclose(self):
            return None

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    runner = CheckRunner({}, "Asia/Shanghai")
    result = await runner.run(
        {
            "id": "private",
            "name": "private",
            "category": "service",
            "type": "http",
            "url": "http://192.168.0.100/health",
            "expected_status": [200],
        }
    )
    assert result.status == "OK"
    assert captured["trust_env"] is False


def test_json_assertion_supports_nested_fields_and_freshness():
    now = datetime.now(ZoneInfo("UTC")).isoformat()
    failures = evaluate_json_assertions({"data": {"state": "ready", "updated": now}}, [{"path": "data.state", "equals": "ready"}, {"path": "data.updated", "max_age_seconds": 60}])
    assert failures == []
    assert evaluate_json_assertions({}, [{"path": "missing.value", "equals": 1}])


def test_log_window_uses_timestamps_and_counts_patterns():
    zone = ZoneInfo("Asia/Shanghai")
    now = datetime.now(zone)
    old = now - timedelta(days=2)
    text = f"{old:%Y-%m-%d %H:%M:%S} ERROR old\n{now:%Y-%m-%d %H:%M:%S} WARNING timeout\n{now:%Y-%m-%d %H:%M:%S} ERROR connection refused\n"
    result = parse_log_window(text, now - timedelta(hours=24))
    assert result["lines"] == 2
    assert result["counts"]["error"] == 1
    assert result["counts"]["warning"] == 1
    assert result["counts"]["timeout"] == 1
