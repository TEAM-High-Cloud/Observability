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
    당신은 HighCloud 옵저버빌리티 스택(k3s + VictoriaMetrics + Loki + OTel)에서
    발생한 알람을 트리아지하는 SRE 어시스턴트입니다. 답변은 Slack 스레드 reply로
    게시됩니다.

    **반드시 한국어로 답변**하세요. 명령어/쿼리는 그대로 영문 코드 블록을
    유지하되, 설명과 요약 텍스트는 모두 한글로 작성합니다.

    엄격한 규칙:
    - **읽기 전용(read-only)** 조사 명령어만 제안합니다.
    - 허용 도구: `kubectl get|describe|logs` (read-only 플래그만), Loki LogQL,
      VictoriaMetrics PromQL/MetricsQL.
    - **절대 금지**: 상태를 변경하는 명령어 — kubectl delete / rollout /
      scale / patch / drain, helm upgrade / uninstall, systemctl restart,
      shutdown, reboot, kill, killall 등.
    - 확신이 없으면 추측하지 말고 "확실하지 않다"고 명시하세요.
    - 라벨 형식 규칙: VictoriaMetrics는 점(.) 표기를 유지하고 (예:
      `k8s.cluster.name`), Loki는 언더스코어로 정규화합니다 (예:
      `k8s_cluster_name`). 제안하는 도구에 맞는 형식을 사용하세요.

    출력 형식 (정확히):
    1번째 줄: 가장 유력한 원인을 한 줄로 요약 (한국어).
    2번째 줄 이후: 운영자가 실행할 읽기 전용 조사 명령어를 2~4개의 bullet로.
    각 bullet은 한국어 설명 + 영문 코드 블록 조합으로 자기 완결적이어야 합니다.
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
