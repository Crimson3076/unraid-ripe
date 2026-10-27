"""Private in-app Home Assistant credentials; never general settings or profiles."""
from __future__ import annotations
import json
import os
import tempfile
import threading
from pathlib import Path
from urllib.parse import unquote, urlsplit
import httpx
from .config import checked_url
from .models import utcnow
from .privacy import register_secrets


def ha_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
        raise ValueError('Use a valid Home Assistant URL')
    value = value.strip()
    if not value:
        return ''
    try:
        parsed = urlsplit(value)
        parsed.port
        path = unquote(parsed.path)
        if any(c.isspace() for c in value) or '\\' in value or '\\' in path or any(p in ('.', '..') for p in path.split('/')):
            raise ValueError()
        return checked_url(value)
    except ValueError:
        raise ValueError('Use an HTTP(S) Home Assistant URL without credentials or query parameters') from None


def ha_token(value: str) -> str:
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError('Use a valid long-lived access token')
    value = value.strip()
    if value and len(value) < 16:
        raise ValueError('Use a valid long-lived access token')
    if any(not 33 <= ord(c) <= 126 for c in value):
        raise ValueError('Use a token without whitespace or control characters')
    return value


class HomeAssistantConnection:
    def __init__(self, root: Path, url: str = '', token: str | None = None):
        self.path = root / 'secrets' / 'home-assistant.json'
        self.lock = threading.RLock()
        self.last_test: dict | None = None
        self.source = 'environment' if url or token else 'none'
        self.url, self.token = ha_url(url), ha_token(token or '')
        register_secrets([self.token])
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ValueError('Home Assistant credential paths must not be symlinks')
        if self.path.exists():
            try:
                if self.path.stat().st_size > 16384:
                    raise ValueError()
                data = json.loads(self.path.read_text())
                if not isinstance(data, dict) or data.get('schema') != 1:
                    raise ValueError()
                self.url, self.token = ha_url(data['url']), ha_token(data['token'])
                self.source = 'app'
            except (ValueError, TypeError, KeyError, OSError):
                raise ValueError('Private Home Assistant settings could not be read') from None
        register_secrets([self.token])

    def credentials(self) -> tuple[str, str | None]:
        with self.lock:
            return self.url, self.token or None

    def public(self) -> dict:
        with self.lock:
            return {'url': self.url, 'has_token': bool(self.token), 'configured': bool(self.url and self.token),
                    'source': self.source, 'last_test': self.last_test}

    def save(self, data: dict) -> dict:
        if not isinstance(data, dict) or set(data) - {'url', 'token', 'clear_token'}:
            raise ValueError('Unknown Home Assistant connection field')
        with self.lock:
            url = ha_url(data.get('url', self.url))
            token = ha_token(data.get('token', ''))
            clear = data.get('clear_token', False)
            if type(clear) is not bool:
                raise ValueError('Token removal must be true or false')
            if not clear and not token and self.token and url != self.url:
                raise ValueError('Enter a new token or remove the saved token when changing servers')
            token = '' if clear else token or self.token
            if token and not url:
                raise ValueError('Enter the Home Assistant server URL')
            self._write({'schema': 1, 'url': url, 'token': token})
            register_secrets([self.token, token])
            self.url, self.token, self.source, self.last_test = url, token, 'app', None
            return self.public()

    def disconnect(self) -> dict:
        # Persist an empty override: removing a file would reactivate old HA_TOKEN.
        return self.save({'url': '', 'clear_token': True})

    def _write(self, value):
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ValueError('Home Assistant credential paths must not be symlinks')
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        descriptor, name = tempfile.mkstemp(prefix='.ha-', dir=self.path.parent)
        try:
            with os.fdopen(descriptor, 'w') as output:
                json.dump(value, output)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(name, 0o600)
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    async def test(self, *, transport=None) -> dict:
        url, token = self.credentials()
        if not url or not token:
            raise ValueError('Save the Home Assistant URL and long-lived token first')
        ok, message = False, 'Could not connect. Check the Home Assistant URL, certificate and network.'
        try:
            async with httpx.AsyncClient(timeout=8, trust_env=False, follow_redirects=False, transport=transport) as client:
                async with client.stream('GET', url + '/api/', headers={'Authorization': 'Bearer ' + token}) as response:
                    if response.status_code in (401, 403):
                        message = 'Home Assistant rejected the token. Check its validity and permissions.'
                    elif 300 <= response.status_code < 400:
                        message = 'Home Assistant redirected the request. Use its direct URL; the token was not forwarded.'
                    else:
                        response.raise_for_status()
                        raw = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=16384):
                            raw.extend(chunk)
                            if len(raw) > 65536:
                                raise ValueError()
                        result = json.loads(raw)
                        ok = isinstance(result, dict) and result.get('message') == 'API running.'
                        message = 'Home Assistant connection verified.' if ok else 'This address did not return the Home Assistant REST API.'
        except (httpx.HTTPError, ValueError, TypeError):
            pass
        with self.lock:
            if (url, token) != self.credentials():
                raise ValueError('The connection changed during the test; test the saved connection again')
            self.last_test = {'ok': ok, 'message': message, 'checked_at': utcnow().isoformat()}
        return dict(self.last_test)
