# HighCpu

## What it means
CPU usage exceeded 80% for 5 minutes on one of the OpenStack control nodes.

## Common causes
- VM workload spike (qemu/libvirt processes)
- Nova/Neutron control plane churn (frequent reconcile)
- Background ansible run or upgrade
- Runaway process or OOM-recovery thrashing

## Read-only checks
- Top processes on the host (Loki):
  - `{k8s_cluster_name="<cluster>", log_file_name=~"syslog|messages"} |~ "(?i)cpu|load"`
- Per-process CPU via OTel process metrics (if enabled):
  - `topk(5, sum by (process.executable.name) (rate({__name__="process.cpu.time"}[5m])))`
- 1m / 5m load average trend:
  - `{__name__="system.cpu.load_average.1m", "k8s.cluster.name"="<cluster>"}`
