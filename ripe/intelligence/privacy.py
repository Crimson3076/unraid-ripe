"""Data minimisation for profiles, model inputs and model-call audit logs."""
from __future__ import annotations
import ipaddress
import json
import os
import re
from typing import Any
from urllib.parse import urlparse
from ripe.models import Profile

_REDACTED = '[redacted]'
# Credential words remain sensitive inside camelCase/suffixed fields (for
# example passwordHash or tokenValue). A bare "key" needs a word boundary so
# relevant configuration such as keyboard settings is not mistaken for a key.
_SECRET_KEY = re.compile(r'password|passwd|token|secret|credential|authorization|cookie|(?:^|[_.\-\s])(?:pwd|key)(?:$|[_.\-\s])', re.I)
_ASSIGNMENT = re.compile(r'(?i)\b([\w.\-]*(?:password|passwd|token|secret|credential|api[_-]?key|private[_-]?key|authorization)[\w.\-]*)["\']?\s*[:=]\s*["\']?[^\s,;"\'}]+')
_BEARER = re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+')
_URL = re.compile(r'\b[A-Za-z][A-Za-z0-9+.\-]{1,30}://[^\s<>"\']+', re.I)
_URI_CREDENTIALS = re.compile(r'(?<![\w])[A-Za-z0-9_.\-]+:[^\s@/<>"\']+@[^\s/<>"\']+')
_IP = re.compile(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?(?![\w.])')
_IPV6 = re.compile(r'(?<![\w])(?:[a-fA-F0-9]{0,4}:){2,}[a-fA-F0-9:]{0,4}(?![\w])')
# Public semantic version names are allowed; dotted DNS names and paths are not.
_HOSTNAME = re.compile(r'(?<![\w.])(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,62})\.)+(?:com|net|org|io|dev|app|local|lan|home|internal|cloud|me|xyz|info|uk|de|edu|gov|test|example)\b', re.I)
_PATH = re.compile(r'(?<![\w])(?:[A-Za-z]:[\\/][^\s<>"\']+|/(?:[\w.\-]+/)*[\w.\-]+|~/[^\s<>"\']+)')
_ENV_ASSIGN = re.compile(r'\b[A-Z][A-Z0-9_]{2,}\s*=\s*[^\s,;]+')
_ENV_NAME = re.compile(r'\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b')
_CAMEL_SECRET = re.compile(r'(?:api|access|private|public|encryption|signing|client)[_\-]?key', re.I)
_OPAQUE_SECRET = re.compile(r'(?<![\w])[A-Za-z0-9_+/=-]{40,}(?![\w])')
_PEM = re.compile(r'-----BEGIN [A-Z ]*(?:PRIVATE KEY|CERTIFICATE)-----.*?-----END [A-Z ]+-----', re.S)
_LONG_CREDENTIAL = re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{12,}|AIza[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b')


def _secret_values() -> list[str]:
    # Presence is never logged. Values are used only as a redaction denylist.
    return [value for name, value in os.environ.items()
            if re.search(r'(?:TOKEN|PASSWORD|PASSWD|SECRET|API_KEY|PRIVATE_KEY|GEMINI_KEY)', name, re.I)
            and len(value) >= 6]


def redact_text(value: str, *, private: bool = False, secrets: tuple[str, ...] = ()) -> str:
    value = str(value)
    for secret in (*_secret_values(), *secrets):
        if secret and len(secret) >= 6:
            value = value.replace(secret, _REDACTED)
    value = _PEM.sub(_REDACTED, value)
    value = _LONG_CREDENTIAL.sub(_REDACTED, value)
    value = _OPAQUE_SECRET.sub(lambda m: _REDACTED if len(m.group()) >= 48 or (any(c.isdigit() for c in m.group()) and any(c.isalpha() for c in m.group())) else m.group(), value)
    value = _ASSIGNMENT.sub(lambda m: m.group(1) + '=' + _REDACTED, value)
    value = _BEARER.sub('Bearer ' + _REDACTED, value)
    value = _URI_CREDENTIALS.sub(_REDACTED, value)
    if private:
        value = _URL.sub('[private-address]', value)
        value = _IP.sub('[private-address]', value)
        value = _IPV6.sub('[private-address]', value)
        value = _HOSTNAME.sub('[private-address]', value)
        value = _PATH.sub('[private-path]', value)
        value = _ENV_ASSIGN.sub('[private-setting]', value)
        value = _ENV_NAME.sub('[private-setting]', value)
    return value


def sanitize_value(value: Any, *, private: bool = False) -> Any:
    if isinstance(value, dict):
        return {redact_text(str(k), private=private): sanitize_value(v, private=private)
                for k, v in value.items() if not _SECRET_KEY.search(str(k)) and not _CAMEL_SECRET.search(str(k))}
    if isinstance(value, (list, tuple)):
        return [sanitize_value(v, private=private) for v in value]
    if isinstance(value, str):
        return redact_text(value, private=private)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(str(value), private=private)


def sanitize_profile(profile: Profile) -> Profile:
    """Keep relevant nonsecret settings; do not retain private paths or addresses."""
    return Profile.model_validate(sanitize_value(profile.model_dump(), private=True))


def profile_text(profile: Profile) -> str:
    return json.dumps(sanitize_profile(profile).model_dump(), sort_keys=True, ensure_ascii=False)


def public_payload(app_name: str, version: str, notes: str) -> dict[str, str]:
    """The only public-classification fields; never serialise an AppRecord here."""
    return {"app": redact_text(app_name, private=True),
            "version": redact_text(version, private=True),
            "public_release_notes": redact_text(notes, private=True)}


def local_model_url(value: str) -> str:
    """Prevent private profile analysis being sent to a public model endpoint."""
    parsed = urlparse(value)
    if parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Local model URL must be a local HTTP(S) endpoint without credentials')
    host = parsed.hostname or ''
    try:
        address = ipaddress.ip_address(host)
        allowed = address.is_loopback or any(address in network for network in
            (ipaddress.ip_network('10.0.0.0/8'), ipaddress.ip_network('172.16.0.0/12'), ipaddress.ip_network('192.168.0.0/16')) if address.version == network.version)
    except ValueError:
        allowed = bool(host and ('.' not in host or host.endswith(('.local', '.lan'))))
    if not allowed:
        raise ValueError('Profile analysis requires a local Ollama endpoint')
    return value.rstrip('/')
