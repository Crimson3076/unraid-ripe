"""Last-line secret redaction before storing or rendering diagnostic data."""
from __future__ import annotations
import re
from typing import Any

SENSITIVE_KEY = re.compile(r'(password|passwd|secret|token|credential|api[_-]?key|authorization|cookie)', re.I)
TOKEN = re.compile(r'(?i)(?:bearer\s+\S+|(?:password|token|api[_-]?key|secret)\s*[:=]\s*[^\s,;]+)')
URL_AUTH = re.compile(r'(https?://)[^\s/@]+:[^\s/@]+@', re.I)


def redact(value: Any, secrets: list[str] | None = None) -> Any:
    if isinstance(value, dict):
        return {str(k): '[redacted]' if SENSITIVE_KEY.search(str(k)) else redact(v, secrets) for k,v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in secrets or []:
            value = value.replace(secret, '[redacted]')
        return URL_AUTH.sub(r'\1[redacted]@', TOKEN.sub('[redacted]', value))
    return value
