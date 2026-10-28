"""One "Since your version" summary over every pending release (map -> reduce), evidence-checked."""
from __future__ import annotations
import hashlib
import json
from typing import Any

from ripe.models import AppRecord, Release, Settings
from ripe.versions import compare_versions
from .evidence import RollupModel, has_evidence
from .privacy import redact_text
from .service import (CHUNK_SIZE, MAX_NOTES, LLMUnavailable, LogCall, _prompt, _structured, chunks)

ROLLUP_PROMPT = 'rollup-v1'
REDUCE_PROMPT = 'rollup-reduce-v1'
GROUPS = ('action', 'security', 'features', 'fixes', 'under_the_hood')
LABELS = {'action': 'What you need to do', 'security': 'Security', 'features': 'New features',
          'fixes': 'Fixes', 'under_the_hood': 'Under the hood'}
Groups = dict[str, list[dict]]


def manifest_text(diff: list[dict] | None) -> str:
    """Changed dependency-manifest lines as plain text; both the model input and the evidence source."""
    lines = []
    for change in diff or []:
        lines += [f"{change['file']}: removed {line}" for line in change.get('removed', [])]
        lines += [f"{change['file']}: added {line}" for line in change.get('added', [])]
    return '\n'.join(lines)[:CHUNK_SIZE // 2]


def image_items(diff: list[dict] | None) -> list[dict]:
    """Deterministic facts: the registry-reported image config differences, no model involved."""
    items = []
    for change in diff or []:
        before, after = change.get('installed'), change.get('latest')
        if before and after:
            text = f"{change['name']} changes from {before} to {after} in the container image."
        elif after:
            text = f"{change['name']} {after} is newly set in the container image."
        else:
            text = f"{change['name']} (was {before}) is no longer set in the container image."
        items.append({'text': text, 'source': 'image', 'release': '',
                      'evidence': f"{change['key']}: {before or '(unset)'} → {after or '(unset)'}"})
    return items


def rollup_key(app: AppRecord, installed: str | None, latest: str | None, pending: list[Release],
               image_diff: list[dict] | None, manifest: list[dict] | None, settings: Settings) -> str:
    """Per (app, installed, latest) plus the inputs those versions imply and the model setup."""
    values = [app.id, str(installed), str(latest), ROLLUP_PROMPT, _prompt(ROLLUP_PROMPT), _prompt(REDUCE_PROMPT),
              json.dumps([[r.version, hashlib.sha256(r.notes.encode()).hexdigest()] for r in pending]),
              json.dumps(image_diff or [], sort_keys=True), json.dumps(manifest or [], sort_keys=True),
              settings.ollama_url, settings.classification_provider, settings.local_model,
              settings.gemini_model, str(settings.second_opinion)]
    return hashlib.sha256('\0'.join(values).encode()).hexdigest()


def _norm(text: str) -> str:
    return ' '.join(text.casefold().split())


def verify_rollup(model: RollupModel, notes: dict[str, str], manifest: str) -> tuple[Groups, list[str]]:
    """Keep an item only when its quote really occurs in the release (or manifest diff) it cites."""
    result: Groups = {}
    dropped: list[str] = []
    for group in GROUPS:
        kept: list[dict] = []
        seen: set[str] = set()
        for index, item in enumerate(getattr(model, group)):
            source = item.model_dump()
            if source['source'] == 'release':
                version = next((v for v in notes if v == source['release'] or compare_versions(v, source['release']) == 0), None)
                ok = version is not None and has_evidence(source['evidence'], notes[version])
                source['release'] = version or source['release']
            else:
                ok = has_evidence(source['evidence'], manifest)
                source['release'] = ''
            text = ' '.join(source['text'].split())[:300]
            if not ok or not text or _norm(text) in seen:
                dropped.append(f'{group}[{index}]')
                continue
            seen.add(_norm(text))
            kept.append({**source, 'text': text})
        result[group] = kept
    return result, dropped


def _union(parts: list[Groups]) -> Groups:
    merged: Groups = {group: [] for group in GROUPS}
    for group in GROUPS:
        seen: set[str] = set()
        for part in parts:
            for item in part.get(group, []):
                if _norm(item['text']) not in seen:
                    seen.add(_norm(item['text']))
                    merged[group].append(item)
    return merged


def plan_chunks(releases: list[Release], manifest: str) -> list[dict]:
    """Greedy map inputs of at most about CHUNK_SIZE characters of notes; dependency changes ride the first."""
    parts: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    size = 0
    for release in releases:
        for piece in chunks(release.notes):
            if current and size + len(piece) > CHUNK_SIZE:
                parts.append(current)
                current, size = [], 0
            current.append((release.version, piece))
            size += len(piece)
    if current:
        parts.append(current)
    plan = [{'releases': part, 'manifest': ''} for part in parts] or [{'releases': [], 'manifest': ''}]
    plan[0]['manifest'] = manifest
    return plan


def headline(groups: Groups, installed: str | None, missing: int) -> str:
    total = sum(len(items) for items in groups.values())
    for group in GROUPS:
        if groups.get(group):
            more = f' (+{total - 1} more)' if total > 1 else ''
            return f"{LABELS[group]}: {groups[group][0]['text']}{more}"
    if missing:
        return f"Nothing notable found since {installed}; {missing} {'release has' if missing == 1 else 'releases have'} no notes."
    return f'Nothing notable found since {installed}.'


async def build_rollup(app: AppRecord, installed: str | None, latest: str | None, pending: list[Release],
                       settings: Settings, *, image_diff: list[dict] | None = None,
                       manifest_diff: list[dict] | None = None, gemini_key: str | None = None,
                       model_unavailable: str | None = None, log_call: LogCall = lambda _: None) -> dict:
    """status 'ok' is cacheable; 'unavailable' keeps the deterministic image facts and is retried."""
    with_notes = [r for r in pending if r.notes.strip() and len(r.notes) <= MAX_NOTES]
    no_notes = [r.version for r in pending if r not in with_notes]
    facts = image_items(image_diff)
    manifest = manifest_text(manifest_diff)
    base = {'installed': installed, 'latest': latest, 'releases': [r.version for r in pending],
            'no_notes': no_notes, 'prompt_version': ROLLUP_PROMPT}
    ai: Groups = {group: [] for group in GROUPS}
    provider_failures: list[str] = []
    provenance: list[dict[str, str]] = []
    if (with_notes or manifest) and model_unavailable:
        groups = {'under_the_hood': facts} if facts else {}
        return {**base, 'status': 'unavailable', 'error': model_unavailable, 'groups': groups,
                'headline': headline(groups, installed, len(no_notes)) if groups else ''}
    if with_notes or manifest:
        # Same rule as classification: only public notes may reach a cloud model.
        public = all(r.id.startswith(('github:', 'unraid:')) for r in with_notes)
        primary = settings.classification_provider if public else 'local'
        requested = [primary]
        if settings.second_opinion and public:
            requested.append('gemini' if primary == 'local' else 'local')
        pseudo = Release(id='rollup:' + app.id, app_id=app.id, version=latest or '')
        full_notes = {r.version: r.notes for r in with_notes}
        plan = plan_chunks(with_notes, manifest)

        async def run_provider(provider: str) -> Groups:
            model = settings.local_model if provider == 'local' else settings.gemini_model
            head = {'app': redact_text(app.name, private=True), 'installed_version': redact_text(str(installed), private=True),
                    'latest_version': redact_text(str(latest), private=True)}
            parts = []
            for index, chunk in enumerate(plan):
                notes: dict[str, str] = {}
                for version, piece in chunk['releases']:
                    notes[version] = notes.get(version, '') + piece + '\n'
                payload = {**head, 'releases': [{'version': redact_text(v, private=True), 'notes': redact_text(t, private=True)} for v, t in notes.items()]}
                if chunk['manifest']:
                    payload['dependency_changes'] = redact_text(chunk['manifest'], private=True)
                part, _ = await _structured(provider, model, ROLLUP_PROMPT, payload, RollupModel, settings, gemini_key,
                                            log_call, pseudo, index,
                                            lambda parsed, n=notes, m=chunk['manifest']: verify_rollup(parsed, n, m))
                parts.append(part)
            merged = _union(parts)
            if len(parts) > 1 and any(merged.values()):
                try:
                    reduced, _ = await _structured(provider, model, REDUCE_PROMPT, {**head, 'candidate_items': {
                        g: [{k: redact_text(str(v), private=True) for k, v in item.items()} for item in items]
                        for g, items in merged.items()}}, RollupModel, settings, gemini_key, log_call, pseudo, len(parts),
                        lambda parsed: verify_rollup(parsed, full_notes, manifest))
                    if any(reduced.values()):
                        merged = reduced
                except LLMUnavailable as exc:
                    provider_failures.append(f'{provider} merge: {redact_text(str(exc), secrets=(gemini_key or "",))}')
            provenance.append({'provider': provider, 'model': model})
            return merged

        results: list[Groups] = []
        for provider in requested:
            if any(p['provider'] == provider for p in provenance):
                continue
            try:
                results.append(await run_provider(provider))
            except LLMUnavailable as exc:
                provider_failures.append(f'{provider}: {redact_text(str(exc), secrets=(gemini_key or "",))}')
                # Gemini failures fall back to the local model, as in classification.
                if provider == 'gemini' and not any(p['provider'] == 'local' for p in provenance):
                    try:
                        results.append(await run_provider('local'))
                    except LLMUnavailable as local_exc:
                        provider_failures.append('local: ' + redact_text(str(local_exc)))
        if not results:
            groups = {'under_the_hood': facts} if facts else {}
            return {**base, 'status': 'unavailable', 'error': '; '.join(provider_failures) or 'Model unavailable',
                    'groups': groups, 'headline': headline(groups, installed, len(no_notes)) if groups else ''}
        ai = _union(results)
    ai['under_the_hood'] = _union([{'under_the_hood': facts}, {'under_the_hood': ai['under_the_hood']}])['under_the_hood']
    groups = {group: ai[group] for group in GROUPS if ai[group]}
    result: dict[str, Any] = {**base, 'status': 'ok', 'groups': groups, 'headline': headline(groups, installed, len(no_notes)),
                              'provenance': provenance, 'provider_errors': provider_failures}
    if provenance:
        result.update(provider=provenance[0]['provider'], model=provenance[0]['model'])
    return result
