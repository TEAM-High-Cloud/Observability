"""Bedrock Claude Haiku 4.5 client for alert analysis.

Single-shot LLM call: assembles the alert + Loki/VM context + matched
runbook into one prompt, returns the model's text reply (or None on
failure so the caller can fall back).

Prompt caching is intentionally not enabled in this baseline so the
boilerplate stays minimal; revisit once the call path is proven.
"""

from __future__ import annotations

import asyncio
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

AGENT_ENABLED = os.getenv("AGENT_ENABLED", "true").lower() in ("1", "true", "yes")
AGENT_MAX_ITERATIONS = int(os.getenv("AGENT_MAX_ITERATIONS", "5"))

SYSTEM_PROMPT = textwrap.dedent(
    """\
    당신은 HighCloud 옵저버빌리티 스택(k3s + VictoriaMetrics + Loki + OTel)에서
    발생한 알람을 트리아지하는 SRE 어시스턴트입니다. 답변은 Slack 스레드 reply로
    게시됩니다.

    **반드시 한국어로 답변**하세요. 명령어/쿼리는 그대로 영문 코드 블록을
    유지하되, 설명과 요약 텍스트는 모두 한글로 작성합니다.

    # 두 가지 모드 — 절대 혼동하지 마세요

    당신은 매 turn 마다 둘 중 **하나**만 합니다:

    **(A) Tool call 발행** — 가설을 좁히기 위해 추가 정보가 필요할 때.
        - `query_metrics` / `query_logs` / `list_recent_alerts` 중 적합한 것을 호출.
        - 텍스트 응답은 발행하지 않습니다 (Bedrock 가 자동으로 다음 turn 에 결과
          전달).
        - **절대 금지**: `query_metrics(promql=...)` 같은 도구 이름을 텍스트로
          답변에 적는 것. 도구를 *호출* 하든지, 아니면 텍스트만 작성하든지 둘 중
          하나입니다.

    **(B) 최종 답변 작성** — 결론을 낼 정보가 충분할 때.
        - 도구 호출 없이 아래 출력 형식대로 텍스트만 작성.
        - 운영자가 직접 손으로 실행할 진짜 명령어만 적습니다 (`kubectl ...`,
          순수 LogQL/PromQL 식). agent 도구 이름은 텍스트에 등장 X.

    # 모드 선택 기준

    - 1차 컨텍스트 (Firing alert + Recent logs + Recent metric samples + Runbook)
      만으로 원인이 명확 → 즉시 (B).
    - 가설 한 가지를 검증해야 결론 가능 → (A) 한 번 호출 → 결과 받으면 (B).
    - 한 turn 에 한 가지 가설만. 한꺼번에 여러 도구 호출 X.
    - 같은 `(도구, 인자)` 조합을 두 번 이상 호출하지 마세요.
    - PromQL/LogQL 은 가능한 한 좁은 selector 사용. 와일드카드 남발 금지.

    # 운영자에게 제시하는 명령어 규칙 (B 모드)

    - **읽기 전용(read-only)** 만 제안.
    - 허용: `kubectl get|describe|logs` (read-only 플래그만), 순수 Loki LogQL,
      순수 VictoriaMetrics PromQL/MetricsQL.
    - **절대 금지**: 상태를 변경하는 명령어 (`kubectl delete` / `rollout` /
      `scale` / `patch` / `drain`, `helm upgrade` / `uninstall`, `systemctl
      restart`, `shutdown`, `reboot`, `kill`, `killall` 등).
    - 확신이 없으면 추측하지 말고 "확실하지 않다"고 명시.
    - 라벨 형식: VictoriaMetrics = 점 표기 (`k8s.cluster.name`),
      Loki = 언더스코어 (`k8s_cluster_name`).

    # 최종 답변 출력 형식 (B 모드 — 정확히)

    1번째 줄: 가장 유력한 원인을 한 줄로 요약 (한국어).
    2번째 줄 이후: 운영자가 실행할 읽기 전용 조사 명령어를 2~4개의 bullet 로.
    각 bullet 은 한국어 설명 + 영문 코드 블록 조합으로 자기 완결적이어야 합니다.
    """
)


# Tool schemas exposed to the model via Bedrock Converse toolConfig.
TOOL_SCHEMAS: list[dict] = [
    {
        "toolSpec": {
            "name": "query_metrics",
            "description": (
                "Run a PromQL/MetricsQL range query against VictoriaMetrics and "
                "return the resulting samples. Use when you need to inspect a "
                "metric that is not already in the 1차 컨텍스트 (e.g. correlating "
                "another resource on the same node). Labels use DOT form on VM "
                "(k8s.cluster.name)."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "promql": {"type": "string", "description": "PromQL/MetricsQL expression"},
                        "lookback_minutes": {"type": "integer", "default": 15, "minimum": 1, "maximum": 360},
                    },
                    "required": ["promql"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "query_logs",
            "description": (
                "Run a LogQL range query against Loki and return the matching log "
                "lines (newest first). Use when you need to inspect logs beyond "
                "what is in the 1차 컨텍스트 (different namespace, different time "
                "window, different filter). Labels use UNDERSCORE form on Loki "
                "(k8s_cluster_name)."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "logql": {"type": "string", "description": "LogQL expression"},
                        "lookback_minutes": {"type": "integer", "default": 15, "minimum": 1, "maximum": 360},
                        "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 500},
                    },
                    "required": ["logql"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "list_recent_alerts",
            "description": (
                "List currently-active Alertmanager alerts that started within "
                "the past `window_minutes`. Use to distinguish a single-shot "
                "alert from a cascade. The triggering alert itself is excluded."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "window_minutes": {"type": "integer", "default": 30, "minimum": 1, "maximum": 360},
                    },
                    "required": [],
                }
            },
        }
    },
]


def _summarize_tool_metrics(samples: list[dict], cap: int = 30) -> str:
    if not samples:
        return "(no samples)"
    head = samples[:cap]
    out = []
    for s in head:
        metric = s.get("metric") or {}
        label_str = ",".join(f"{k}={v}" for k, v in list(metric.items())[:4])
        out.append(f"{s.get('ts')}: {s.get('value')}  {{{label_str}}}")
    if len(samples) > cap:
        out.append(f"… [{len(samples) - cap} more samples truncated]")
    return "\n".join(out)


def _summarize_tool_logs(lines: list[str], cap: int = 40) -> str:
    if not lines:
        return "(no lines)"
    head = lines[:cap]
    rendered = "\n".join(head)
    if len(lines) > cap:
        rendered += f"\n… [{len(lines) - cap} more lines truncated]"
    return rendered


def _summarize_alerts(alerts: list[dict]) -> str:
    if not alerts:
        return "(no other active alerts)"
    return json.dumps(alerts[:25], indent=2, ensure_ascii=False)


async def _dispatch_tool(name: str, args: dict, fingerprint: str | None) -> str:
    """Run one tool call and return a model-facing text result."""
    # Local import keeps bedrock module decoupled at import time and avoids a
    # cycle if the context modules grow further.
    from app.context import alertmanager, loki, vm

    if name == "query_metrics":
        samples = await vm.fetch_promql(
            promql=args.get("promql", ""),
            lookback_minutes=int(args.get("lookback_minutes", 15)),
        )
        return _summarize_tool_metrics(samples)
    if name == "query_logs":
        lines = await loki.fetch_logql(
            logql=args.get("logql", ""),
            lookback_minutes=int(args.get("lookback_minutes", 15)),
            limit=int(args.get("limit", 100)),
        )
        return _summarize_tool_logs(lines)
    if name == "list_recent_alerts":
        alerts = await alertmanager.list_active(
            window_minutes=int(args.get("window_minutes", 30)),
            exclude_fingerprint=fingerprint,
        )
        return _summarize_alerts(alerts)
    return f"(unknown tool: {name})"

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


async def analyze_agentic(
    alert: dict,
    loki_lines: list[str],
    vm_samples: list[dict],
    runbook: str,
    fingerprint: str | None = None,
) -> tuple[str | None, int]:
    """Tool-using analyze loop. Returns (text, iterations).

    Each iteration the model can either emit a final answer (end_turn) or
    request a tool call (tool_use); we run the tool, append the result, and
    let the model continue. Guard rails:
      - max iterations = AGENT_MAX_ITERATIONS
      - duplicate (tool, args) detection breaks the loop early
    Falls back to None on any Bedrock failure so the caller can degrade.
    """
    client = _client_lazy()
    user_prompt = _build_user(alert, loki_lines, vm_samples, runbook)
    messages: list[dict] = [
        {"role": "user", "content": [{"text": user_prompt}]},
    ]
    seen_calls: set[str] = set()
    final_text: str | None = None
    iterations = 0

    for it in range(1, AGENT_MAX_ITERATIONS + 1):
        iterations = it
        try:
            resp = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: client.converse(
                    modelId=MODEL_ID,
                    system=[{"text": SYSTEM_PROMPT}],
                    messages=messages,
                    toolConfig={"tools": TOOL_SCHEMAS},
                    inferenceConfig={"maxTokens": MAX_TOKENS, "temperature": TEMPERATURE},
                ),
            )
        except Exception as e:
            log.warning("bedrock agentic failed (iter=%d): %s", it, e)
            return None, iterations

        usage = resp.get("usage", {})
        stop_reason = resp.get("stopReason")
        log.info(
            "agent iter=%d stop=%s input=%s output=%s",
            it, stop_reason, usage.get("inputTokens"), usage.get("outputTokens"),
        )

        message = resp["output"]["message"]
        content = message.get("content", []) or []
        messages.append({"role": "assistant", "content": content})

        if stop_reason == "end_turn" or stop_reason == "max_tokens":
            for block in content:
                if "text" in block:
                    final_text = (final_text or "") + block["text"]
            break

        if stop_reason != "tool_use":
            for block in content:
                if "text" in block:
                    final_text = (final_text or "") + block["text"]
            break

        tool_uses = [b["toolUse"] for b in content if "toolUse" in b]
        if not tool_uses:
            for block in content:
                if "text" in block:
                    final_text = (final_text or "") + block["text"]
            break

        tool_results: list[dict] = []
        for tu in tool_uses:
            name = tu.get("name", "")
            args = tu.get("input", {}) or {}
            use_id = tu.get("toolUseId", "")
            sig = f"{name}|{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
            if sig in seen_calls:
                log.info("agent duplicate tool call detected: %s", sig[:160])
                tool_results.append({
                    "toolResult": {
                        "toolUseId": use_id,
                        "content": [{"text": "(duplicate tool call ignored — please conclude with current evidence.)"}],
                        "status": "error",
                    }
                })
                continue
            seen_calls.add(sig)
            try:
                text = await _dispatch_tool(name, args, fingerprint)
            except Exception as e:
                log.warning("agent tool '%s' failed: %s", name, e)
                text = f"(tool error: {e})"
            tool_results.append({
                "toolResult": {
                    "toolUseId": use_id,
                    "content": [{"text": text}],
                }
            })

        messages.append({"role": "user", "content": tool_results})

    return final_text, iterations
