# ai-analyzer

Alertmanager → AI Analyzer → Slack thread reply 구조의 알람 enricher.
Bedrock(Claude Haiku 4.5)로 원인 요약 + 읽기 전용 조사 명령어를 생성해
Alertmanager가 발사한 Slack 메시지의 **스레드**에 reply한다.

Tracking issue: [#47](https://github.com/TEAM-High-Cloud/Observability/issues/47)

## 아키텍처

```
                  ┌─► Slack channel (즉시, ~1s)
Alertmanager ─────┤   (route continue: true)
                  └─► ai-analyzer ─► Loki/VM 컨텍스트
                                  └─► Bedrock Haiku 4.5
                                  └─► Slack thread reply (~5-15s)
```

- 알람 latency는 Alertmanager → Slack 직발사로 유지
- AI 분석은 동일 알람을 병렬로 받아 스레드 reply로 첨부
- ai-analyzer 다운 시에도 알람은 정상 (SPOF 없음)

## 상태

PR3 (Slack 연결) — 분석 결과를 Slack 스레드 reply로 운영자에게 전달.
Alertmanager는 즉시 발사한 알람 메시지에 fingerprint footer를 박고,
ai-analyzer가 conversations.history로 그 부모 메시지를 찾아 스레드에 reply.

- [x] FastAPI: `/healthz`, `/webhook`
- [x] in-repo Helm chart + ArgoCD Application
- [x] ECR 빌드 + 자동 image tag bump PR
- [x] Loki/VM 컨텍스트 수집 (PR2)
- [x] 런북 markdown KB (PR2)
- [x] Bedrock 호출 (PR2)
- [x] Slack Block Kit thread reply (PR3)
- [x] Alertmanager route 분기 + fingerprint footer (PR3)
- [ ] Bedrock prompt caching (별 작업)
- [ ] Bedrock 비용 모니터링 대시보드 (별 후속 — CloudWatch datasource 셋업 포함)
- [ ] k3s pod에서 Tailscale 호스트네임 해결 (#63)
- [ ] GLOBAL Haiku 4.5로 복귀 (Marketplace 구독 풀린 후)

## 디렉토리

```
ai-analyzer/
├── application.yaml          # ArgoCD App (destination: metric / ns: ai-analyzer)
├── Dockerfile
├── requirements.txt          # fastapi, uvicorn, httpx, boto3
├── app/
│   ├── main.py               # /webhook이 BackgroundTasks로 분석 분기
│   ├── bedrock.py            # boto3 Converse + 안전 system prompt
│   ├── context/
│   │   ├── loki.py           # LogQL ±15m (underscore 정규화)
│   │   └── vm.py             # PromQL ±15m (dot 유지)
│   └── runbooks/             # alertname → markdown (4개 + _default)
└── chart/                    # in-repo Helm chart
```

## 분석 흐름 (PR3 기준)

```
                            ┌─► slack-default receiver ─► Slack #alerts (즉시)
Alertmanager (continue:true)┤   메시지 본문 끝에 `fingerprint=<value>` footer
                            └─► ai-analyzer receiver ─► POST /webhook
                                                          ↓
                                                       main.py
                                                          ├── 즉시 200 OK
                                                          └── BackgroundTasks
                                                               ├── loki.fetch ─┐
                                                               ├── vm.fetch  ──┴─ asyncio.gather
                                                               ├── runbooks.find
                                                               ├── bedrock.analyze (boto3 → executor)
                                                               └── slack.post_thread_reply
                                                                     ├── conversations.history로
                                                                     │   fingerprint 매칭 → parent ts
                                                                     └── chat.postMessage (thread_ts)
```

- 모든 단계 soft-fail: Loki/VM/Bedrock/Slack 실패해도 분석 자체는 끊지 않음.
- Slack 토큰 없으면 thread reply 단계만 스킵, stdout JSON 로그는 그대로.
- 부모 메시지 매칭 실패하면 채널에 새 메시지로 fallback (스레드 X).

## 환경 변수 (chart values.env)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `LOKI_URL` | `http://observ-log-server.tail07adfd.ts.net:31100` | cross-cluster Tailscale FQDN — short name은 coredns search 순서에 막힘 |
| `VM_URL` | `http://metric-server-victoria-metrics-single-server.victoria-metrics:8428` | in-cluster |
| `CONTEXT_WINDOW_MINUTES` | `15` | ±N분 컨텍스트 |
| `LOKI_MAX_LINES` | `200` | 모델에 넘기는 로그 라인 한도 |
| `BEDROCK_REGION` | `ap-northeast-2` | |
| `BEDROCK_MODEL_ID` | `apac.anthropic.claude-sonnet-4-20250514-v1:0` | APAC inference profile (GLOBAL Haiku 4.5는 Marketplace 구독 이슈로 임시 우회) |
| `BEDROCK_MAX_TOKENS` | `1024` | |
| `BEDROCK_TEMPERATURE` | `0.2` | 낮게 — 정확성 우선 |

## 런북 추가 방법

`app/runbooks/<AlertName>.md` 파일 생성. 매칭은 정확한 alertname → 파일명.
없으면 `_default.md`로 fallback. 코드 수정 없이 markdown만 추가하면 됨.

## Slack 셋업 (PR3 머지 후 1회)

기존 `alert-observability` App을 그대로 augment하는 경로:

1. `api.slack.com/apps` → 기존 App 선택 (예: `alert-observability`)
2. **OAuth & Permissions** → Bot Token Scopes:
   - `chat:write`
   - `channels:history` (public 채널) 또는 `groups:history` (private)
   - `chat:write.customize` — AI 분석 메시지의 sender 이름/아이콘 override용
3. **Install to Workspace** → **Bot User OAuth Token** (`xoxb-…`) 복사
4. 대상 채널(예: `#alerts`)에 `/invite @obs-ai-analyzer`
5. K8s Secret 생성:
   ```sh
   kubectl -n ai-analyzer create secret generic ai-analyzer-slack \
     --from-literal=token=xoxb-...
   ```
6. `apps/ai/ai-analyzer/chart/values.yaml`에 채널 ID + slack.enabled 켜기:
   ```yaml
   env:
     SLACK_CHANNEL: "C01234567"   # Slack UI → 채널 우클릭 → "채널 세부 정보 보기"
   slack:
     enabled: true
   ```
   (1줄짜리 PR 또는 직접 main 커밋)

Slack 채널 ID 못 찾으면 콘솔 또는:
```sh
curl -H "Authorization: Bearer xoxb-..." \
  https://slack.com/api/conversations.list?types=public_channel,private_channel
```

## 배포 흐름 (GitOps)

```
코드 변경 push → GH Actions
   ├─ docker build → ECR push (tag = git sha short)
   └─ values.yaml의 image.tag를 그 sha로 수정한 PR 자동 생성 (peter-evans/create-pull-request)
        ↓
      [사람 리뷰 후 머지]
        ↓
      ArgoCD가 새 tag 발견 → pod 재배포
```

인프라(yaml-only) 변경은 빌드 워크플로우 스킵 — ArgoCD가 곧장 반영.

## 수동 셋업 (PR1 머지 전후)

ArgoCD가 sync 시도하면 image pull이 실패한다. 아래 셋업이 끝나야 정상 기동.

### 1. ECR repository

```sh
aws ecr create-repository \
  --repository-name obs-ai-analyzer \
  --region ap-northeast-2 \
  --image-scanning-configuration scanOnPush=true
```

ARN: `arn:aws:ecr:ap-northeast-2:167781471242:repository/obs-ai-analyzer`

### 2. GitHub OIDC IAM Role

GH Actions → ECR push 용. trust policy로 이 리포만 허용.

Role name 예: `obs-gha-ai-analyzer-push`

Trust policy (token.actions.githubusercontent.com OIDC provider 필요):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Federated": "arn:aws:iam::167781471242:oidc-provider/token.actions.githubusercontent.com" },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
      },
      "StringLike": {
        "token.actions.githubusercontent.com:sub": "repo:TEAM-High-Cloud/Observability:ref:refs/heads/main"
      }
    }
  }]
}
```

Permissions: `AmazonEC2ContainerRegistryPowerUser` 또는 동등 minimum.

Role ARN을 GH repo Settings → Secrets and variables → Actions → `AWS_GHA_ROLE_ARN`로 등록.

### 3. EC2 instance profile (metric-server 노드)

K8s pod이 노드 metadata로 Bedrock 호출 (PR2부터 사용).

기존 instance profile에 inline policy 추가:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ecr:GetAuthorizationToken",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchCheckLayerAvailability"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [ "bedrock:InvokeModel" ],
      "Resource": "arn:aws:bedrock:ap-northeast-2::foundation-model/anthropic.claude-haiku-4-5-*"
    }
  ]
}
```

> IMDSv2 hop limit 2 필수.

### 4. 첫 이미지 배포 (자동화됨)

main 머지 후 자동:

1. `Build ai-analyzer image` 워크플로우 실행 → ECR push
2. 워크플로우가 `chart/values.yaml`의 `image.tag` 갱신 PR 자동 생성
3. PR 리뷰 후 머지 → ArgoCD가 새 tag 보고 pod 재배포

## ECR 토큰 자동 갱신 (k3s 한정)

ECR `GetAuthorizationToken`이 발급하는 토큰은 **12시간 유효**한데,
k3s는 EKS와 달리 노드 instance profile로 ECR pull을 자동 인증하지 않는다.
그래서 image pull 시점(=새 봇 PR 머지, pod 재기동, 노드 재부팅 등)에
유효한 docker-registry Secret이 K8s에 살아 있어야 한다.

이 chart는 **6시간 주기 CronJob**으로 `ecr-pull` Secret을 갱신한다.

- CronJob: `…-ecr-refresh`, 스케줄 `0 */6 * * *`
- 이미지: `amazon/aws-cli` (public, ECR 인증 불필요 — 닭과 달걀 문제 회피)
- 자격증명: 노드 instance profile (IMDSv2 hop=2 필수)
  → `aws ecr get-login-password` → dockerconfigjson 인코딩
- K8s API 직접 호출(`curl`)로 같은 ns의 `ecr-pull` Secret을 upsert
- 별 SA + Role (해당 Secret에 대한 get/patch/update/delete + 생성만)
- ai-analyzer SA의 `imagePullSecrets`가 `ecr-pull`을 가리킴

### 부트스트랩

- 첫 도입 시점에는 한 번만 수동 시드 필요 (이후 CronJob이 갱신):

```sh
# metric-server 노드(또는 instance profile credential 가진 위치)에서
TOKEN=$(aws ecr get-login-password --region ap-northeast-2)
kubectl create secret docker-registry ecr-pull \
  --docker-server=167781471242.dkr.ecr.ap-northeast-2.amazonaws.com \
  --docker-username=AWS \
  --docker-password="$TOKEN" \
  -n ai-analyzer
```

- 이미 시드돼 있으면 다음 CronJob 실행 시점에 자동 갱신됨.
- CronJob을 즉시 1회 돌리고 싶으면:

```sh
kubectl -n ai-analyzer create job --from=cronjob/ai-analyzer-ecr-refresh ecr-refresh-now
kubectl -n ai-analyzer logs job/ecr-refresh-now
```

### 동작 확인

```sh
kubectl -n ai-analyzer get cronjob
kubectl -n ai-analyzer get secret ecr-pull -o jsonpath='{.metadata.creationTimestamp}'
kubectl -n ai-analyzer logs -l job-name --tail=20  # 최근 갱신 로그
```

## 로컬 동작

```sh
cd apps/ai/ai-analyzer
docker build -t ai-analyzer:dev .
docker run --rm -p 8000:8000 ai-analyzer:dev

# 다른 터미널
curl localhost:8000/healthz
curl -X POST localhost:8000/webhook -H 'Content-Type: application/json' \
  -d '{"alerts":[{"labels":{"alertname":"Test"}}], "status":"firing", "groupKey":"test"}'
```
