# Ripe — notes for coding agents

Ripe is a read-only update tracker (FastAPI + SQLite + Jinja templates) for Unraid, Docker and Home Assistant. Stage 1 must never pull images, run commands, or modify the host.

## Develop and test

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --no-deps -r requirements-dev.lock
.venv/bin/python -m pytest -q
```

Python 3.12+. Keep the Docker GET allowlist in `ripe/sources/collector.py` read-only. Never log or display secrets.

## Rules for the automated GitHub agent

When fixing an issue:
1. Reproduce first: write a failing test that shows the bug, then fix it, then run the full test suite.
2. Keep the change minimal and focused on the issue. No refactors, renames, dependency bumps or formatting churn.
3. Never edit `.github/`, Dockerfiles/compose files, dependency/lock files, migrations, auth or secret handling.
   If the fix needs that, stop and explain in a comment instead.
4. Open a pull request with `gh pr create`, title `fix: <short description>`, and a body that starts with
   `Fixes #<issue number>` followed by: root cause, what changed, and how it was tested.
5. If the issue is unclear or not reproducible, comment on the issue asking for exactly what's missing and stop.
