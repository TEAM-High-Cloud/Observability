"""Loki LogQL fetcher for alert context.

Loki normalizes OTel attribute keys to underscores (k8s_cluster_name, etc.),
while VictoriaMetrics keeps the original dot form. We translate any dotted
label on the alert (k8s.cluster.name, k8s.namespace.name, ...) into the
underscore form when building the LogQL stream selector.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Iterable

import httpx

log = logging.getLogger("ai-analyzer.loki")

LOKI_URL = os.getenv("LOKI_URL", "")
WINDOW_MINUTES = int(os.getenv("CONTEXT_WINDOW_MINUTES", "15"))
MAX_LINES = int(os.getenv("LOKI_MAX_LINES", "200"))
HTTP_TIMEOUT = float(os.getenv("LOKI_HTTP_TIMEOUT", "8"))

# Labels worth carrying from Alertmanager into the LogQL stream selector.
# Any other dotted key is normalized to underscore form on the fly.
_PASSTHROUGH_LABELS: tuple[str, ...] = (
    "k8s_cluster_name",
    "k8s_namespace_name",
    "k8s_pod_name",
    "k8s_node_name",
)


def _dot_to_underscore(key: str) -> str:
    return key.replace(".", "_").replace("-", "_")


def _build_selector(labels: dict[str, str]) -> str:
    parts: list[str] = []
    for k, v in labels.items():
        norm = _dot_to_underscore(k)
        if norm in _PASSTHROUGH_LABELS and v:
            parts.append(f'{norm}="{v}"')
        elif norm == "cluster" and v:
            parts.append(f'k8s_cluster_name="{v}"')
    if not parts:
        # Fall back to the broadest filter so the LLM still gets something.
        parts.append('k8s_cluster_name=~".+"')
    return "{" + ",".join(parts) + "}"


def _query_from_alert(alert: dict) -> str:
    annotations = alert.get("annotations", {}) or {}
    query = annotations.get("logql_context_query")
    if query:
        return query
    return _build_selector(alert.get("labels", {}) or {})


def _format_lines(values: Iterable[list[str]]) -> list[str]:
    """Loki query_range returns [[ts_ns, line], ...] under each stream."""
    out: list[str] = []
    for ts_ns, line in values:
        try:
            ts = datetime.fromtimestamp(int(ts_ns) / 1e9, tz=timezone.utc)
            out.append(f'{ts.isoformat(timespec="seconds")} {line}')
        except (ValueError, TypeError):
            out.append(line)
    return out


async def fetch(alert: dict, when: datetime | None = None) -> list[str]:
    """Return up to MAX_LINES log lines around the alert.

    Soft-fails: on any error returns an empty list so the analyzer can still
    proceed with whatever other context it has.
    """
    if not LOKI_URL:
        log.warning("LOKI_URL not set; skipping Loki context")
        return []

    when = when or datetime.now(timezone.utc)
    start = when - timedelta(minutes=WINDOW_MINUTES)
    end = when + timedelta(minutes=WINDOW_MINUTES)

    query = _query_from_alert(alert)

    params = {
        "query": query,
        "start": int(start.timestamp() * 1e9),
        "end": int(end.timestamp() * 1e9),
        "limit": MAX_LINES,
        "direction": "backward",
    }

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.get(f"{LOKI_URL}/loki/api/v1/query_range", params=params)
            r.raise_for_status()
            data = r.json().get("data", {})
            streams = data.get("result", []) or []
    except Exception as e:
        log.warning("loki query failed: %s", e)
        return []

    lines: list[str] = []
    for stream in streams:
        lines.extend(_format_lines(stream.get("values", [])))
    return lines[:MAX_LINES]
