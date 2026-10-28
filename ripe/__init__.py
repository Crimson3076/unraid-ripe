import os
import re


def version_from(raw: str | None) -> str:
    """Version shown in the UI. The image build stamps RIPE_VERSION from git: a release tag ("v0.3.1"), or
    "v0.3.0-2-g1ba3d83" for a main build two commits after a release (shown as "0.3.0+1ba3d83"). Without it: "dev"."""
    raw = (raw or "").strip()
    if not raw:
        return "dev"
    if raw[0] in "vV":
        raw = raw[1:]
    m = re.fullmatch(r"(.+)-\d+-g([0-9a-f]{7,40})", raw)
    return f"{m.group(1)}+{m.group(2)[:7]}" if m else raw


__version__ = version_from(os.environ.get("RIPE_VERSION"))
