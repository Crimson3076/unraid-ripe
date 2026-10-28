"""Strict model schemas plus verbatim citation and completeness checks."""
from __future__ import annotations
import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

Tag = Literal['bugfix', 'security', 'breaking', 'migration', 'big_feature', 'deprecation', 'performance', 'docs_only']

class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

class SecurityItem(Strict):
    description: str
    severity: Literal['low', 'medium', 'high', 'critical']
    requires: str
    evidence: str

class BreakingItem(Strict):
    description: str
    action_needed: str
    evidence: str

class MigrationItem(Strict):
    description: str
    steps: list[str]
    evidence: str

class FeatureItem(Strict):
    description: str
    evidence: str

class KeywordAddress(Strict):
    term: str
    quote: str
    matters: bool
    why: str

class Classification(Strict):
    summary: str
    summary_evidence: str
    tags: list[Tag]
    tag_evidence: dict[str, str]
    security: list[SecurityItem]
    breaking: list[BreakingItem]
    migration: list[MigrationItem]
    big_features: list[FeatureItem]
    keyword_hits_addressed: list[KeywordAddress]
    confidence: Literal['low', 'medium', 'high']

class ApplicabilityItem(Strict):
    kind: Literal['security', 'breaking', 'migration']
    index: int = Field(ge=0)
    status: Literal['applies', 'might_apply', 'does_not_apply']
    reason: str
    evidence: str
    profile_evidence: str

class RollupItem(Strict):
    text: str
    source: Literal['release', 'manifest']
    release: str
    evidence: str

class RollupModel(Strict):
    action: list[RollupItem]
    security: list[RollupItem]
    features: list[RollupItem]
    fixes: list[RollupItem]
    under_the_hood: list[RollupItem]

class Applicability(Strict):
    items: list[ApplicabilityItem]

KEYWORDS = re.compile(r'\bbreaking\b|\bmigration\b|\bmigrat\w*|\bdeprecat\w*|\bremoved\b|\bsecurity\b|\bCVE-[\w-]*|\bvulnerab\w*|\brequired\b|\bmust\b', re.I)


def normalise(text: str) -> str:
    return ' '.join(text.casefold().split())


def has_evidence(quote: str, source: str) -> bool:
    return bool(normalise(quote)) and normalise(quote) in normalise(source)


def keyword_warnings(notes: str, addressed: list[dict]) -> list[dict]:
    """Coverage is per occurrence, not just a keyword name anywhere in a reply."""
    source = normalise(notes)
    ranges: list[tuple[int, int, str]] = []
    for item in addressed:
        quote = normalise(item.get('quote', ''))
        term = normalise(item.get('term', ''))
        if not quote or not term or term not in quote or not has_evidence(quote, source):
            continue
        offset = 0
        while (start := source.find(quote, offset)) >= 0:
            ranges.append((start, start + len(quote), term))
            offset = start + max(1, len(quote))
    warnings = []
    seen = set()
    for hit in KEYWORDS.finditer(source):
        if any(left <= hit.start() and right >= hit.end() and normalise(hit.group()).startswith(term) for left, right, term in ranges):
            continue
        line = source[max(0, hit.start() - 100):min(len(source), hit.end() + 180)]
        key = (hit.group().casefold(), line)
        if key not in seen:
            seen.add(key)
            warnings.append({'term': hit.group(), 'quote': line,
                             'message': f'Mentions {hit.group()}, not addressed by the model, worth a look'})
    return warnings


def verify_classification(model: Classification, notes: str) -> tuple[dict, list[str]]:
    result = model.model_dump()
    dropped: list[str] = []
    if not has_evidence(result['summary_evidence'], notes):
        result['summary'] = ''
        result['summary_evidence'] = ''
        dropped.append('summary')
    for kind in ('security', 'breaking', 'migration', 'big_features'):
        retained = []
        for index, item in enumerate(result[kind]):
            if has_evidence(item['evidence'], notes):
                retained.append(item)
            else:
                dropped.append(f'{kind}[{index}]')
        result[kind] = retained
    valid_tags = {}
    for tag in result['tags']:
        quote = result['tag_evidence'].get(tag, '')
        if has_evidence(quote, notes):
            valid_tags[tag] = quote
        else:
            dropped.append('tag:' + tag)
    # Structured severe tags must cite an actual retained corresponding item.
    for tag, kind in (('security', 'security'), ('breaking', 'breaking'), ('migration', 'migration'), ('big_feature', 'big_features')):
        if tag in valid_tags and not result[kind]:
            del valid_tags[tag]
            dropped.append('tag_without_item:' + tag)
    result['tags'] = list(valid_tags)
    result['tag_evidence'] = valid_tags
    addressed = []
    for index, item in enumerate(result['keyword_hits_addressed']):
        if has_evidence(item['quote'], notes) and normalise(item['term']) in normalise(item['quote']):
            addressed.append({**item, 'evidence': item['quote']})
        else:
            dropped.append(f'keyword_hits_addressed[{index}]')
    result['keyword_hits_addressed'] = addressed
    result['keyword_warnings'] = keyword_warnings(notes, addressed)
    result['status'] = 'ok'
    return result, dropped


def conservative_applicability(classification: dict | None) -> list[dict]:
    return [{'kind': kind, 'index': i, 'status': 'applies', 'reason': '',
             'evidence': '', 'profile_evidence': '', 'verified': False}
            for kind in ('security', 'breaking', 'migration')
            for i, _ in enumerate((classification or {}).get(kind, []))]


def verify_applicability(model: Applicability, classification: dict, notes: str, profile: str) -> tuple[list[dict], list[str]]:
    output = {(item['kind'], item['index']): item for item in conservative_applicability(classification)}
    dropped = []
    seen = set()
    for item in model.items:
        key = (item.kind, item.index)
        if key not in output:
            dropped.append(f'unknown_item:{item.kind}[{item.index}]')
            continue
        if key in seen:
            # Conflicting repeated indices must never silently dismiss a real risk.
            output[key] = next((entry for entry in conservative_applicability(classification)
                                if (entry['kind'], entry['index']) == key), output.get(key))
            dropped.append(f'duplicate:{item.kind}[{item.index}]')
            continue
        seen.add(key)
        original = classification[item.kind][item.index]
        # Both citations must be present and the release quote must substantiate
        # this specific risk, not an unrelated sentence in the release notes.
        if (has_evidence(item.evidence, notes)
            and (has_evidence(item.evidence, original['evidence']) or has_evidence(original['evidence'], item.evidence))
            and has_evidence(item.profile_evidence, profile)):
            output[key] = {**item.model_dump(), 'verified': True}
        else:
            dropped.append(f'evidence:{item.kind}[{item.index}]')
    return list(output.values()), dropped
