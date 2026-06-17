"""Runbook lookup: alertname → markdown text.

We keep runbooks as plain markdown files in this directory so reviewers
can edit them without touching code. Lookup is a simple alertname match
with `_default.md` as a fallback.
"""

from __future__ import annotations

from pathlib import Path

_DIR = Path(__file__).parent


def find(alertname: str) -> str:
    safe = "".join(c for c in alertname if c.isalnum() or c in ("-", "_"))
    candidate = _DIR / f"{safe}.md"
    if candidate.exists():
        return candidate.read_text(encoding="utf-8")
    fallback = _DIR / "_default.md"
    if fallback.exists():
        return fallback.read_text(encoding="utf-8")
    return ""
