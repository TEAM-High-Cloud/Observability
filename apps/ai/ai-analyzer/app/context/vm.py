"""VictoriaMetrics PromQL fetcher for alert context.

VM accepts the dotted MetricsQL label syntax via quoted labels, so the
alert's expression is forwarded as-is. We only re-window the query around
the alert time to grab the recent trend.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger("ai-analyzer.vm")

VM_URL = os.getenv("VM_URL", "")
WINDOW_MINUTES = int(os.getenv("CONTEXT_WINDOW_MINUTES", "15"))
STEP_SECONDS = int(os.getenv("VM_STEP_SECONDS", "60"))
HTTP_TIMEOUT = float(os.getenv("VM_HTTP_TIMEOUT", "8"))


def _expr_from_alert(alert: dict) -> str | None:
    """Best-effort recovery of the original alert expression.

    Alertmanager doesn't always pass the raw expression. When it's missing we
    fall back to a label-based instant query so the LLM still sees something.
    """
    annotations = alert.get("annotations", {}) or {}
    if "expr" in annotations:
        return annotations["expr"]

    labels = alert.get("labels", {}) or {}
    if cluster := labels.get("cluster") or labels.get("k8s.cluster.name"):
        return (
            "100 * (1 - sum by (\"k8s.cluster.name\") "
            f"(rate({{__name__=\"system.cpu.time\",state=\"idle\","
            f"\"k8s.cluster.name\"=\"{cluster}\"}}[5m]))"
            " / sum by (\"k8s.cluster.name\") "
            f"(rate({{__name__=\"system.cpu.time\","
            f"\"k8s.cluster.name\"=\"{cluster}\"}}[5m])))"
        )
    return None


async def fetch(alert: dict, when: datetime | None = None) -> list[dict]:
    """Return PromQL samples around the alert as [{ts, value}, ...].

    Soft-fails: returns [] on any error.
    """
    if not VM_URL:
        log.warning("VM_URL not set; skipping VM context")
        return []

    expr = _expr_from_alert(alert)
    if not expr:
        return []

    when = when or datetime.now(timezone.utc)
    start = when - timedelta(minutes=WINDOW_MINUTES)
    end = when + timedelta(minutes=WINDOW_MINUTES)

    params = {
        "query": expr,
        "start": int(start.timestamp()),
        "end": int(end.timestamp()),
        "step": STEP_SECONDS,
    }

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.get(f"{VM_URL}/api/v1/query_range", params=params)
            r.raise_for_status()
            data = r.json().get("data", {})
            result = data.get("result", []) or []
    except Exception as e:
        log.warning("vm query failed: %s", e)
        return []

    samples: list[dict] = []
    for series in result:
        for ts, val in series.get("values", []):
            samples.append({"ts": ts, "value": val})
    return samples
