from app.redaction import redact


def test_redacts_secrets_and_connection_strings():
    value = redact("Authorization: Bearer-abc password=hunter2 postgresql://user:pass@db.local/app")
    assert "hunter2" not in value
    assert "Bearer-abc" not in value
    assert "user:pass" not in value
    assert redact({"api_key": "secret", "safe": "ok"}) == {"api_key": "***", "safe": "ok"}
