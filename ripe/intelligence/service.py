"""Versioned, cached-by-caller classification and local applicability pipeline."""
from __future__ import annotations
import hashlib
import json
import re
import time
from collections.abc import Callable
from importlib.resources import files
from typing import Any

import httpx
from pydantic import ValidationError
from ripe.models import AppRecord, Release, Settings
from .evidence import (Applicability, Classification, conservative_applicability,
                       keyword_warnings, verify_applicability, verify_classification)
from .privacy import (local_model_url, profile_text, public_payload, redact_text,
                      sanitize_profile, sanitize_value)

PROMPT_VERSION = 'classification-v1'
APPLICABILITY_PROMPT_VERSION = 'applicability-v1'
CHUNK_SIZE = 12_000
MAX_NOTES = 500_000
LOCAL_NUM_PREDICT = 8192
CTX_MARGIN = 1024
CTX_STEP = 4096
MIN_NUM_CTX = 8192
MAX_NUM_CTX = 32768
LogCall = Callable[[dict[str, Any]], None]

class LLMUnavailable(RuntimeError):
    pass


def _prompt(name: str) -> str:
    return files('ripe').joinpath('prompts', name + '.txt').read_text(encoding='utf-8')


def prompt_digest(name: str = APPLICABILITY_PROMPT_VERSION) -> str:
    """Content digest so editing a prompt invalidates cached model output."""
    if name not in {PROMPT_VERSION, APPLICABILITY_PROMPT_VERSION}:
        raise ValueError('Unknown prompt version')
    return hashlib.sha256(_prompt(name).encode()).hexdigest()


def classification_key(release: Release, app: AppRecord, settings: Settings) -> str:
    """Only public classification inputs; never invalidate this on a profile edit."""
    values = [app.upstream_repo or app.name, release.version, release.notes, PROMPT_VERSION,
              _prompt(PROMPT_VERSION), settings.ollama_url,
              settings.classification_provider, settings.local_model, settings.gemini_model,
              str(settings.second_opinion), str(release.id.startswith(('github:', 'unraid:')))]
    return hashlib.sha256('\0'.join(values).encode()).hexdigest()


def chunks(text: str) -> list[str]:
    if len(text) <= CHUNK_SIZE:
        return [text]
    output = []
    while text:
        if len(text) <= CHUNK_SIZE:
            output.append(text)
            break
        split = text.rfind('\n', CHUNK_SIZE // 2, CHUNK_SIZE)
        if split < 0:
            split = text.rfind(' ', CHUNK_SIZE // 2, CHUNK_SIZE)
        if split < 0:
            split = CHUNK_SIZE
        output.append(text[:split])
        text = text[split:].lstrip()
    return output


def _audit(log_call: LogCall, **entry: Any) -> None:
    # Log prompt identity and sizes, never raw inputs or headers/keys.
    safe = sanitize_value(entry, private=True)
    log_call(safe)


def local_num_ctx(prompt: str, user: str) -> int:
    """Context window for one local request: prompt + output + margin, in 4k steps."""
    # Conservative estimate (3 chars/token) since JSON and code tokenize densely.
    needed = (len(prompt) + len(user)) // 3 + 1 + LOCAL_NUM_PREDICT + CTX_MARGIN
    rounded = -(-needed // CTX_STEP) * CTX_STEP
    return min(max(rounded, MIN_NUM_CTX), MAX_NUM_CTX)


async def _call_model(provider: str, model: str, prompt: str, payload: dict,
                      settings: Settings, key: str | None) -> str:
    user = json.dumps(payload, ensure_ascii=False)
    if provider == 'local':
        url = local_model_url(settings.ollama_url)
        async with httpx.AsyncClient(timeout=180, trust_env=False, follow_redirects=False) as client:
            response = await client.post(url + '/api/chat', json={
                'model': model, 'stream': False, 'think': False, 'format': 'json',
                'messages': [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': user}],
                'options': {'temperature': 0, 'num_ctx': local_num_ctx(prompt, user),
                            'num_predict': LOCAL_NUM_PREDICT},
            })
        if response.status_code != 200:
            raise LLMUnavailable(f'Local model HTTP {response.status_code}')
        try:
            content = response.json()['message']['content']
        except (KeyError, ValueError, TypeError):
            raise LLMUnavailable('Local model response shape invalid') from None
    elif provider == 'gemini':
        if not key:
            raise LLMUnavailable('Gemini key is not configured')
        if not re.fullmatch(r'[A-Za-z0-9._-]{1,100}', model):
            raise LLMUnavailable('Gemini model name invalid')
        # key is in a header, never URL/query or logging; this request contains
        # ONLY public_payload() fields (or that schema plus a retry instruction).
        async with httpx.AsyncClient(timeout=90, trust_env=True, follow_redirects=False) as client:
            response = await client.post(
                f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
                headers={'x-goog-api-key': key},
                json={'systemInstruction': {'parts': [{'text': prompt}]},
                      'contents': [{'role': 'user', 'parts': [{'text': user}]}],
                      'generationConfig': {'temperature': 0, 'responseMimeType': 'application/json',
                                           'maxOutputTokens': 8192}},
            )
        if response.status_code != 200:
            raise LLMUnavailable(f'Gemini HTTP {response.status_code}')
        try:
            content = ''.join(part.get('text', '') for part in response.json()['candidates'][0]['content']['parts'])
        except (KeyError, IndexError, ValueError, TypeError):
            raise LLMUnavailable('Gemini response shape invalid') from None
    else:
        raise LLMUnavailable('Unknown model provider')
    if not isinstance(content, str) or len(content) > 160_000:
        raise LLMUnavailable('Model output is empty, invalid or too large')
    return content


async def _structured(provider: str, model: str, prompt_name: str, payload: dict,
                      schema: type, settings: Settings, key: str | None,
                      log_call: LogCall, release: Release, chunk_index: int,
                      verifier: Callable) -> tuple[Any, list[str]]:
    prompt = _prompt(prompt_name)
    for attempt in range(2):
        started = time.monotonic()
        output = ''
        entry = {'model': model, 'provider': provider, 'prompt_version': prompt_name,
                 'release_id': release.id, 'input_size': len(prompt) + len(json.dumps(payload)),
                 'retry': attempt, 'chunk_index': chunk_index}
        try:
            output = await _call_model(provider, model, prompt, payload, settings, key)
            parsed = schema.model_validate_json(output)
            # Secret material is removed before it can be stored, even if a model
            # independently emits a token-looking string that was not in its input.
            parsed = schema.model_validate(sanitize_value(parsed.model_dump()))
            result, dropped = verifier(parsed)
        except (ValidationError, json.JSONDecodeError) as exc:
            _audit(log_call, **entry, output=redact_text(output, secrets=(key or '',)),
                   duration_ms=round((time.monotonic() - started) * 1000),
                   evidence_passed=False, outcome='invalid_json', error=type(exc).__name__)
            if attempt == 0:
                prompt += '\nYour previous response failed strict JSON/schema validation. Return ONLY the complete requested JSON shape, with no extra fields.'
                continue
            raise LLMUnavailable('Model returned invalid structured JSON twice') from None
        except (httpx.HTTPError, LLMUnavailable, ValueError) as exc:
            _audit(log_call, **entry, output='',
                   duration_ms=round((time.monotonic() - started) * 1000),
                   evidence_passed=False, outcome='unavailable', error=type(exc).__name__)
            raise LLMUnavailable(str(exc) if isinstance(exc, LLMUnavailable) else type(exc).__name__) from None
        _audit(log_call, **entry, output=redact_text(output, secrets=(key or '',)),
               duration_ms=round((time.monotonic() - started) * 1000),
               evidence_passed=not dropped, evidence_dropped=dropped, outcome='ok')
        return result, dropped
    raise LLMUnavailable('Structured output unavailable')


def _merge_classifications(parts: list[dict], notes: str) -> dict:
    result = {'status': 'ok', 'summary': '', 'summary_evidence': '', 'tags': [],
              'tag_evidence': {}, 'security': [], 'breaking': [], 'migration': [],
              'big_features': [], 'keyword_hits_addressed': [], 'confidence': 'high'}
    confidence = {'low': 0, 'medium': 1, 'high': 2}
    for part in parts:
        if not result['summary'] and part.get('summary'):
            result['summary'], result['summary_evidence'] = part['summary'], part['summary_evidence']
        for kind in ('security', 'breaking', 'migration', 'big_features', 'keyword_hits_addressed'):
            existing = {json.dumps(item, sort_keys=True) for item in result[kind]}
            for item in part.get(kind, []):
                serialised = json.dumps(item, sort_keys=True)
                if serialised not in existing:
                    result[kind].append(item)
                    existing.add(serialised)
        for tag in part.get('tags', []):
            if tag not in result['tags']:
                result['tags'].append(tag)
                result['tag_evidence'][tag] = part['tag_evidence'][tag]
        if confidence.get(part.get('confidence'), 0) < confidence[result['confidence']]:
            result['confidence'] = part.get('confidence', 'low')
    result['keyword_warnings'] = keyword_warnings(notes, result['keyword_hits_addressed'])
    return result


def _provenance(result: dict, provider: str, model: str) -> dict:
    return {'provider': provider, 'model': model, 'tags': result.get('tags', []),
            'severities': sorted(item['severity'] for item in result.get('security', []))}


def _unknown(notes: str, reason: str) -> dict:
    return {'status': 'unknown', 'summary': '', 'summary_evidence': '', 'tags': [],
            'tag_evidence': {}, 'security': [], 'breaking': [], 'migration': [], 'big_features': [],
            'keyword_hits_addressed': [], 'keyword_warnings': keyword_warnings(notes, []),
            'confidence': 'low', 'error': reason, 'prompt_version': PROMPT_VERSION, 'provenance': []}


async def classify_release(release: Release, app: AppRecord, settings: Settings, *,
                           gemini_key: str | None = None, log_call: LogCall = lambda _: None) -> Release:
    """Classify notes; caller persists each key once for reuse across app views."""
    cache_key = classification_key(release, app, settings)
    if (release.classification is not None and release.classification.get('status') == 'ok'
            and release.classification_key == cache_key):
        return release
    if not release.notes.strip() or len(release.notes) > MAX_NOTES:
        reason = 'Release notes unavailable' if not release.notes.strip() else 'Release notes exceed safe processing limit'
        return release.model_copy(update={'classification': _unknown(release.notes, reason), 'classification_key': cache_key})
    # HA-provided notes can be authored locally. A URL attribute from HA is not
    # provenance: only records created by our public upstream fetchers qualify.
    public_notes = release.id.startswith(('github:', 'unraid:'))
    primary = settings.classification_provider if public_notes else 'local'
    requested = [primary]
    if settings.second_opinion and public_notes:
        requested.append('gemini' if primary == 'local' else 'local')
    results: list[tuple[str, str, dict]] = []
    provider_failures: list[str] = []
    if not public_notes and (settings.classification_provider == 'gemini' or settings.second_opinion):
        provider_failures.append('gemini: local-source notes are restricted to the local model')

    async def run_provider(provider: str) -> tuple[str, dict]:
        model = settings.local_model if provider == 'local' else settings.gemini_model
        parts = []
        for index, chunk in enumerate(chunks(release.notes)):
            # This allowlist is used for BOTH providers so public classification
            # can never accidentally consume a private AppRecord/profile.
            payload = public_payload((app.upstream_repo or app.name).rsplit('/', 1)[-1], release.version, chunk)
            part, _ = await _structured(provider, model, PROMPT_VERSION, payload,
                                        Classification, settings, gemini_key, log_call, release, index,
                                        lambda parsed: verify_classification(parsed, chunk))
            parts.append(part)
        return model, _merge_classifications(parts, release.notes)

    for provider in requested:
        if any(existing[0] == provider for existing in results):
            continue
        try:
            model, result = await run_provider(provider)
            results.append((provider, model, result))
        except LLMUnavailable as exc:
            provider_failures.append(f'{provider}: {redact_text(str(exc), secrets=(gemini_key or "",))}')
            # On Gemini rate limits/errors, fall back to local, independently of
            # whether the user asked for a second opinion.
            if provider == 'gemini' and not any(existing[0] == 'local' for existing in results):
                try:
                    model, result = await run_provider('local')
                    results.append(('local', model, result))
                except LLMUnavailable as local_exc:
                    provider_failures.append('local: ' + redact_text(str(local_exc)))
    if not results:
        unknown = _unknown(release.notes, '; '.join(provider_failures) or 'Model unavailable')
        return release.model_copy(update={'classification': unknown, 'classification_key': cache_key,
                                          'model_disagreement': False})
    provider, model, result = results[0]
    provenance = [_provenance(item, p, m) for p, m, item in results]
    disagreement = len({(tuple(sorted(p['tags'])), tuple(p['severities'])) for p in provenance}) > 1
    # Preserve supported risk items from either model on disagreement. Otherwise a
    # second model's security finding could be flagged but never shown/analyzed.
    if disagreement:
        result = _merge_classifications([item for _, _, item in results], release.notes)
    result.update(provider=provider, model=model, prompt_version=PROMPT_VERSION,
                  provenance=provenance, provider_errors=provider_failures,
                  chunk_count=len(chunks(release.notes)))
    return release.model_copy(update={'classification': result, 'classification_key': cache_key,
                                      'model_disagreement': disagreement})


async def analyze_applicability(release: Release, app: AppRecord, settings: Settings, *,
                                log_call: LogCall = lambda _: None) -> Release:
    classification = release.classification or {}
    fallback = conservative_applicability(classification)
    if not fallback:
        return release.model_copy(update={'applicability': []})
    text = profile_text(sanitize_profile(app.profile))
    items = {kind: classification.get(kind, []) for kind in ('security', 'breaking', 'migration')}
    payload = {'app': redact_text(app.name, private=True), 'version': release.version,
               'release_items': items, 'profile_json': text}
    try:
        result, _ = await _structured('local', settings.local_model, APPLICABILITY_PROMPT_VERSION,
                                     payload, Applicability, settings, None, log_call, release, 0,
                                     lambda parsed: verify_applicability(parsed, classification, release.notes, text))
    except LLMUnavailable:
        result = fallback
    return release.model_copy(update={'applicability': result})
