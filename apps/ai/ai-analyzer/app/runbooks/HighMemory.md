# HighMemory

## What it means
Memory used ratio exceeded 90% for 5 minutes on one of the OpenStack control
nodes. OOM-killer becomes likely soon if the pressure does not subside.

## Common causes
- VM guest RAM oversubscription
- Memory leak in a service (mysqld/rabbitmq commonly drift up)
- Caches not reclaimed (page cache pinned by an IO-heavy workload)

## Read-only checks
- syslog for OOM signals:
  - `{k8s_cluster_name="<cluster>", log_file_name="syslog"} |~ "(?i)oom-killer|killed process|out of memory"`
- Memory state breakdown:
  - `{__name__="system.memory.usage", "k8s.cluster.name"="<cluster>"}` by `state`
- Top memory users (OTel process metrics, if enabled):
  - `topk(5, {__name__="process.memory.physical_usage", "k8s.cluster.name"="<cluster>"})`
