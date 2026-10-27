# AGENTS.md

Guidance for coding agents and reviewers. Coding agents also follow CLAUDE.md.

## Code Review Rules

### Secrets and auth
- Flag any change that could expose a token, password or API key: in logs, API responses, rendered HTML, error messages or LLM prompts. Saved credentials must never be echoed back, not even masked or partially.
- Flag changes that weaken authentication, CSRF/same-origin checks or permission checks.

### Tests
- Flag behaviour changes that have no test, and tests that no longer exercise the code they claim to cover.

### Scope
- Flag changes to CI workflows, Dockerfiles, dependency/lock files or migrations unless the linked issue asks for them.
