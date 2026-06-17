import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from fastapi import BackgroundTasks, FastAPI, Request

from app import bedrock, runbooks, slack
from app.context import loki, vm

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":%(message)r}',
)
log = logging.getLogger("ai-analyzer")

app = FastAPI(title="ai-analyzer", version="0.2.0")


def _parse_when(alert: dict) -> datetime:
    raw = alert.get("startsAt") or alert.get("endsAt")
    if not raw:
        return datetime.now(timezone.utc)
    try:
        # Alertmanager uses RFC3339 with optional fractional seconds + 'Z'.
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


async def _analyze_alert(alert: dict, group_key: str | None) -> None:
    """Background task: gather context, call Bedrock, log the result.

    Soft-fails throughout — the goal is "best-effort enrichment", never
    interrupting the alert pipeline.
    """
    labels = alert.get("labels", {}) or {}
    alertname = labels.get("alertname", "")
    when = _parse_when(alert)

    loki_lines, vm_samples = await asyncio.gather(
        loki.fetch(alert, when),
        vm.fetch(alert, when),
    )
    runbook = runbooks.find(alertname)

    # boto3 is sync — offload to a thread so we don't block the loop.
    summary = await asyncio.get_event_loop().run_in_executor(
        None, bedrock.analyze, alert, loki_lines, vm_samples, runbook
    )

    result = {
        "alertname": alertname,
        "status": alert.get("status"),
        "groupKey": group_key,
        "labels": labels,
        "loki_lines": len(loki_lines),
        "vm_samples": len(vm_samples),
        "runbook_matched": bool(runbook) and not runbook.startswith("# Default runbook"),
        "summary": summary,
        "fallback": summary is None,
    }
    log.info("analysis: %s", json.dumps(result, ensure_ascii=False))

    # Best-effort thread reply. Alertmanager carries alert.fingerprint
    # which our Slack template embeds, so conversations.history can match it.
    posted = await slack.post_thread_reply(result, alert.get("fingerprint"))
    log.info("slack posted: %s", posted)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks):
    payload = await request.json()
    alerts = payload.get("alerts", []) or []
    group_key = payload.get("groupKey")
    log.info(
        "received %d alert(s); status=%s, groupKey=%s",
        len(alerts),
        payload.get("status"),
        group_key,
    )
    for alert in alerts:
        background.add_task(_analyze_alert, alert, group_key)
    return {"received": len(alerts)}
