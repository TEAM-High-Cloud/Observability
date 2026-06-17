"""Bedrock Claude Haiku 4.5 client for alert analysis.

Single-shot LLM call: assembles the alert + Loki/VM context + matched
runbook into one prompt, returns the model's text reply (or None on
failure so the caller can fall back).

Prompt caching is intentionally not enabled in this baseline so the
boilerplate stays minimal; revisit once the call path is proven.
"""

from __future__ import annotations

import json
import logging
import os
import textwrap

import boto3

log = logging.getLogger("ai-analyzer.bedrock")

REGION = os.getenv("BEDROCK_REGION", "ap-northeast-2")
MODEL_ID = os.getenv(
    "BEDROCK_MODEL_ID",
    "anthropic.claude-haiku-4-5-20251001-v1:0",
)
MAX_TOKENS = int(os.getenv("BEDROCK_MAX_TOKENS", "1024"))
TEMPERATURE = float(os.getenv("BEDROCK_TEMPERATURE", "0.2"))

SYSTEM_PROMPT = textwrap.dedent(
    """\
    You are an SRE assistant that triages firing alerts on the
    HighCloud observability stack (k3s + VictoriaMetrics + Loki + OTel).
    Your reply will be posted as a Slack thread reply.

    Hard rules:
    - Only suggest READ-ONLY investigation commands.
    - Allowed tools: `kubectl get|describe|logs` (read-only flags only),
      LogQL queries against Loki, PromQL/MetricsQL queries against VM.
    - NEVER suggest commands that modify state: kubectl delete / rollout
      / scale / patch / drain, helm upgrade / uninstall, systemctl
      restart, shutdown, reboot, kill, killall, etc.
    - If you are not confident, say so explicitly instead of guessing.
    - Naming: VictoriaMetrics keeps dotted labels (k8s.cluster.name);
      Loki normalizes them to underscores (k8s_cluster_name). Use the
      right form for the tool you propose.

    Output format (exactly):
    Line 1: One-line summary of the most likely root cause.
    Lines 2..N: 2-4 bullet points of read-only commands the on-call
    should run, each one self-contained.
    """
)

_client = None


def _client_lazy():
    global _client
    if _client is None:
        _client = boto3.client("bedrock-runtime", region_name=REGION)
    return _client


def _summarize_loki(lines: list[str], cap: int = 60) -> str:
    if not lines:
        return "(none)"
    head = lines[:cap]
    return "\n".join(head) + (f"\n… [{len(lines) - cap} more lines truncated]" if len(lines) > cap else "")


def _summarize_vm(samples: list[dict], cap: int = 30) -> str:
    if not samples:
        return "(none)"
    head = samples[:cap]
    rendered = "\n".join(f"{s.get('ts')}: {s.get('value')}" for s in head)
    if len(samples) > cap:
        rendered += f"\n… [{len(samples) - cap} more samples truncated]"
    return rendered


def _build_user(alert: dict, loki_lines: list[str], vm_samples: list[dict], runbook: str) -> str:
    return textwrap.dedent(
        f"""\
        ## Firing alert
        ```json
        {json.dumps(alert, indent=2, ensure_ascii=False)}
        ```

        ## Recent logs (Loki, ±15m, max 60 shown)
        ```
        {_summarize_loki(loki_lines)}
        ```

        ## Recent metric samples (VM, ±15m, max 30 shown)
        ```
        {_summarize_vm(vm_samples)}
        ```

        ## Runbook
        {runbook or "(no runbook matched)"}
        """
    )


def analyze(
    alert: dict,
    loki_lines: list[str],
    vm_samples: list[dict],
    runbook: str,
) -> str | None:
    """Return the model's text reply, or None if the call fails."""
    user_prompt = _build_user(alert, loki_lines, vm_samples, runbook)
    try:
        resp = _client_lazy().converse(
            modelId=MODEL_ID,
            system=[{"text": SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": user_prompt}]}],
            inferenceConfig={"maxTokens": MAX_TOKENS, "temperature": TEMPERATURE},
        )
        out = resp["output"]["message"]["content"][0]["text"]
        usage = resp.get("usage", {})
        log.info(
            "bedrock ok: input_tokens=%s output_tokens=%s",
            usage.get("inputTokens"),
            usage.get("outputTokens"),
        )
        return out
    except Exception as e:
        log.warning("bedrock failed: %s", e)
        return None
