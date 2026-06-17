# Default runbook

No alert-specific runbook matched. Use the alert's `labels` and
`annotations` to narrow down which subsystem is affected before
running broader read-only checks.

## Read-only checks
- Pod/Deployment state in the affected namespace:
  - `kubectl -n <ns> get pods,deploy,sts`
- Recent events:
  - `kubectl -n <ns> get events --sort-by=.lastTimestamp | tail -30`
- Logs:
  - `kubectl -n <ns> logs <pod> --tail=200`
- Loki search by alert labels (underscore form):
  - `{k8s_namespace_name="<ns>", k8s_pod_name=~"<prefix>.*"} | __error__=""`
- VM PromQL for related metric (dot form):
  - `{__name__="<metric>", "k8s.cluster.name"="<cluster>"}`
