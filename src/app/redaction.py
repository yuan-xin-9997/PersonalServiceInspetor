from __future__ import annotations

import re
from typing import Any

REDACTION_RULES = [
    re.compile(r"(?i)(authorization\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?i)((?:password|passwd|pwd|token|api[_-]?key|cookie)\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(postgres(?:ql)?|mysql)://[^\s@]+@"),
    re.compile(r"(?i)(set-cookie\s*:\s*)([^\r\n]+)"),
]


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "***" if _is_sensitive_key(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if not isinstance(value, str):
        return value
    text = value
    for rule in REDACTION_RULES:
        if rule.pattern.startswith("(?i)(postgres"):
            text = rule.sub(lambda match: f"{match.group(1)}://***@", text)
        else:
            text = rule.sub(lambda match: f"{match.group(1)}***", text)
    return text


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    return any(term in lowered for term in ("password", "passwd", "token", "secret", "api_key", "cookie", "authorization"))
