"""Evidence-checked local and optional public-cloud intelligence."""
from .privacy import sanitize_profile, sanitize_value, redact_text, public_payload, local_model_url
from .service import classify_release, analyze_applicability, classification_key, prompt_digest
from .evidence import has_evidence, keyword_warnings
from .rollup import build_rollup, rollup_key, ROLLBACK_PROMPT

__all__ = ['sanitize_profile', 'sanitize_value', 'redact_text', 'public_payload', 'local_model_url',
           'classify_release', 'analyze_applicability', 'classification_key', 'prompt_digest', 'has_evidence', 'keyword_warnings',
           'build_rollup', 'rollup_key', 'ROLLBACK_PROMPT']
