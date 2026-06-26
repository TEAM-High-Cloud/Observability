"""Slack thread reply for AI-enriched alert analysis.

Alertmanager posts the raw alert to Slack first (immediate latency budget).
This module finds that parent message via `conversations.history` + a
fingerprint marker the Slack template embeds, then replies in-thread with
the LLM analysis as a Block Kit card.

If no parent is found (lookback window misses, channel quiet, missing
scopes), we degrade gracefully to a new channel message — the analysis
still lands, just without thread attachment.
"""

from __future__ import annotations

import logging
import os
import time

import httpx

log = logging.getLogger("ai-analyzer.slack")

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "")
SLACK_FINGERPRINT_MARKER = os.getenv("SLACK_FINGERPRINT_MARKER", "fingerprint=")
SLACK_HISTORY_LOOKBACK = int(os.getenv("SLACK_HISTORY_LOOKBACK", "100"))
# Stale-parent guard: ignore Alertmanager parent messages older than this
# when matching fingerprints. Without it the same alert re-firing hours later
# would reply into the original (long-stale) thread because the marker is still
# present in channel history.
SLACK_PARENT_MAX_AGE_MIN = int(os.getenv("SLACK_PARENT_MAX_AGE_MIN", "30"))
HTTP_TIMEOUT = float(os.getenv("SLACK_HTTP_TIMEOUT", "8"))

# Per-message sender override so the AI analyzer card is visually distinct
# from the raw alert posted by the same App. Requires `chat:write.customize`
# scope on the Slack App. If unset, the message uses the App's own name/icon.
SLACK_USERNAME = os.getenv("SLACK_USERNAME", "")
SLACK_ICON_URL = os.getenv("SLACK_ICON_URL", "")
SLACK_ICON_EMOJI = os.getenv("SLACK_ICON_EMOJI", "")


def _enabled() -> bool:
    return bool(SLACK_BOT_TOKEN) and bool(SLACK_CHANNEL)


async def _post(method: str, payload: dict) -> dict:
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json; charset=utf-8",
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.post(
                f"https://slack.com/api/{method}", json=payload, headers=headers
            )
            r.raise_for_status()
            data = r.json()
            if not data.get("ok"):
                log.warning("slack %s error: %s", method, data.get("error"))
            return data
    except Exception as e:
        log.warning("slack %s failed: %s", method, e)
        return {"ok": False, "error": str(e)}


async def _get(method: str, params: dict) -> dict:
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.get(
                f"https://slack.com/api/{method}", params=params, headers=headers
            )
            r.raise_for_status()
            data = r.json()
            if not data.get("ok"):
                log.warning("slack %s error: %s", method, data.get("error"))
            return data
    except Exception as e:
        log.warning("slack %s failed: %s", method, e)
        return {"ok": False, "error": str(e)}


async def _find_thread_ts(channel: str, fingerprint: str) -> str | None:
    """Locate the Alertmanager-posted parent message for `fingerprint`.

    Two failure modes the older implementation hit:
    - **Stale match**: a same-fingerprint parent from a previous firing was
      still inside the history slice, so replies kept piling onto an
      hours-old thread instead of the fresh one.
    - **First-match bias**: relied on Slack returning messages newest-first;
      under load that ordering can be perturbed and we'd grab whichever
      match came back first.

    Fix: collect *every* matching parent within the lookback window, drop
    the ones older than `SLACK_PARENT_MAX_AGE_MIN`, and pick the newest by
    ts. Returns None if nothing fresh matches — caller falls back to a new
    channel message.
    """
    if not fingerprint:
        return None
    needle = f"{SLACK_FINGERPRINT_MARKER}{fingerprint}"
    res = await _get(
        "conversations.history", {"channel": channel, "limit": SLACK_HISTORY_LOOKBACK}
    )
    if not res.get("ok"):
        return None

    cutoff_epoch = time.time() - SLACK_PARENT_MAX_AGE_MIN * 60
    candidates: list[tuple[float, str]] = []
    for msg in res.get("messages", []):
        text = msg.get("text", "") or ""
        for att in msg.get("attachments", []) or []:
            text += "\n" + (att.get("text", "") or "")
        # blocks may contain the marker in section/context text fields
        for block in msg.get("blocks", []) or []:
            text += "\n" + str(block)
        if needle not in text:
            continue
        ts_str = msg.get("ts", "")
        try:
            ts_f = float(ts_str)
        except (TypeError, ValueError):
            continue
        if ts_f < cutoff_epoch:
            continue
        candidates.append((ts_f, ts_str))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _format_blocks(result: dict) -> list[dict]:
    alertname = result.get("alertname") or "Unknown"
    labels = result.get("labels", {}) or {}
    severity = labels.get("severity", "")
    cluster = labels.get("cluster") or labels.get("k8s_cluster_name") or ""
    summary = (
        result.get("summary")
        or "_LLM 응답을 받지 못했습니다 (Bedrock unavailable)._"
    )
    loki_n = result.get("loki_lines", 0)
    vm_n = result.get("vm_samples", 0)
    runbook_matched = result.get("runbook_matched", False)
    iters = result.get("agent_iterations", 0)

    header_bits = [f"🤖 AI 분석: {alertname}"]
    if severity:
        header_bits.append(severity)
    if cluster:
        header_bits.append(cluster)
    header = " · ".join(header_bits)

    footer = (
        f"loki_lines={loki_n} · vm_samples={vm_n} · "
        f"runbook={'✓' if runbook_matched else '✗'}"
    )
    if iters:
        footer += f" · agent_iters={iters}"

    # Slack section text caps at 3000 chars; truncate with a marker.
    if len(summary) > 2900:
        summary = summary[:2900] + "\n_…(truncated)_"

    return [
        {"type": "header", "text": {"type": "plain_text", "text": header[:150]}},
        {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]},
    ]


async def post_thread_reply(result: dict, fingerprint: str | None) -> bool:
    """Reply with the analysis under the Alertmanager-posted parent message.

    Returns False (and logs) on any error or when Slack env vars are unset
    — caller treats this as best-effort enrichment.
    """
    if not _enabled():
        log.info("slack disabled (token/channel unset); skipping")
        return False

    blocks = _format_blocks(result)
    payload = {
        "channel": SLACK_CHANNEL,
        "blocks": blocks,
        "text": result.get("summary") or "AI 분석",
    }
    if SLACK_USERNAME:
        payload["username"] = SLACK_USERNAME
    # icon_url takes priority; fall back to icon_emoji if only that is set.
    if SLACK_ICON_URL:
        payload["icon_url"] = SLACK_ICON_URL
    elif SLACK_ICON_EMOJI:
        payload["icon_emoji"] = SLACK_ICON_EMOJI
    if fingerprint:
        ts = await _find_thread_ts(SLACK_CHANNEL, fingerprint)
        if ts:
            payload["thread_ts"] = ts
        else:
            log.info(
                "no matching parent for fingerprint=%s; posting as new message",
                fingerprint,
            )

    res = await _post("chat.postMessage", payload)
    return bool(res.get("ok"))
