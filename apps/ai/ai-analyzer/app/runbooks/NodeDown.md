# NodeDown

## What it means
OTel hostmetrics from one of the OpenStack control nodes (pc1 or pc2) have
been absent for 2+ minutes. Either the OTel Collector pod on that node is
down, or the host itself is down.

## Common causes
- otelcol DaemonSet pod restarted and is stuck CrashLoopBackOff
- Host network outage (Tailscale tunnel down)
- Host actually rebooted or powered off
- Disk full → kubelet/otel can't write logs

## Read-only checks
- Tailscale reachability:
  - `tailscale ping <node>` from a peer
- OTel pod state on the node:
  - `kubectl -n monitoring get pods -l app.kubernetes.io/name=opentelemetry-collector -o wide`
- Recent collector logs:
  - `kubectl -n monitoring logs ds/otelcol --tail=200`
- Host syslog spike (Loki):
  - `{k8s_cluster_name="<cluster>", log_file_name="syslog"} | __error__=""`
