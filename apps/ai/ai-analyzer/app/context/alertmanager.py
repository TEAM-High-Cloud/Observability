"""Alertmanager active alert fetcher for agent context.

Used by the `list_recent_alerts` tool so the model can correlate the firing
alert with other concurrent alerts (cascade vs single-shot).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger("ai-analyzer.am")

AM_URL = os.getenv("AM_URL", "")
HTTP_TIMEOUT = float(os.getenv("AM_HTTP_TIMEOUT", "8"))


async def list_active(window_minutes: int = 30, exclude_fingerprint: str | None = None) -> list[dict]:
    """Return a compact summary of active alerts within `window_minutes`.

    Soft-fails: returns [] on any error so the agent can keep going.
    Each entry: {alertname, severity, cluster, startsAt, status, fingerprint}.
    """
    if not AM_URL:
        log.warning("AM_URL not set; returning empty active-alert list")
        return []

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.get(f"{AM_URL}/api/v2/alerts", params={"active": "true"})
            r.raise_for_status()
            data = r.json() or []
    except Exception as e:
        log.warning("alertmanager fetch failed: %s", e)
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    out: list[dict] = []
    for a in data:
        fp = a.get("fingerprint")
        if exclude_fingerprint and fp == exclude_fingerprint:
            continue
        starts = a.get("startsAt") or ""
        try:
            started = datetime.fromisoformat(starts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if started < cutoff:
            continue
        labels = a.get("labels", {}) or {}
        status = (a.get("status") or {}).get("state", "")
        out.append({
            "alertname": labels.get("alertname", ""),
            "severity": labels.get("severity", ""),
            "cluster": labels.get("cluster") or labels.get("k8s_cluster_name") or "",
            "startsAt": starts,
            "state": status,
            "fingerprint": fp,
        })
    out.sort(key=lambda x: x.get("startsAt", ""), reverse=True)
    return out
