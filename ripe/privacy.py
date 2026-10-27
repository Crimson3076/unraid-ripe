"""Last-line secret redaction before storing or rendering diagnostic data."""
from __future__ import annotations
import re
import threading
from collections import deque
from typing import Any

SENSITIVE_KEY = re.compile(r'(password|passwd|secret|token|credential|api[_-]?key|authorization|cookie)', re.I)
TOKEN = re.compile(r'(?i)(?:bearer\s+\S+|(?:password|token|api[_-]?key|secret)\s*[:=]\s*[^\s,;]+)')
URL_AUTH = re.compile(r'(https?://)[^\s/@]+:[^\s/@]+@', re.I)
_runtime_secrets = deque(maxlen=32)
_secret_lock = threading.RLock()


def register_secrets(values):
    """Retain a bounded denylist of active/replaced in-app credentials in memory."""
    with _secret_lock:
        for value in values:
            if isinstance(value, str) and len(value) >= 6 and value not in _runtime_secrets:
                _runtime_secrets.append(value)


def registered_secrets() -> tuple[str, ...]:
    with _secret_lock:
        return tuple(_runtime_secrets)


def redact(value: Any, secrets: list[str] | None = None) -> Any:
    if isinstance(value, dict):
        return {str(k): '[redacted]' if SENSITIVE_KEY.search(str(k)) else redact(v, secrets) for k,v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in (*registered_secrets(), *(secrets or [])):
            value = value.replace(secret, '[redacted]')
        return URL_AUTH.sub(r'\1[redacted]@', TOKEN.sub('[redacted]', value))
    return value
