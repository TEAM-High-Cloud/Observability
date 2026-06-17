# HighFilesystem

## What it means
A filesystem on one of the OpenStack control nodes exceeded 85% used. Disk
full will start failing writes (logs, journald, VM images) shortly.

## Common causes
- Log rotation not running (syslog/journal piling up)
- Orphan qcow2 / snapshot files on /var/lib/nova
- Container image / build cache buildup
- A single oversized core dump

## Read-only checks
- syslog for fs/disk warnings:
  - `{k8s_cluster_name="<cluster>", log_file_name="syslog"} |~ "(?i)no space|disk full|read-only"`
- Per-mount usage trend (VM):
  - `{__name__="system.filesystem.usage", "k8s.cluster.name"="<cluster>", state="used"}` by `mountpoint`
- Largest dirs on that mount (read-only host check):
  - `du -xh --max-depth=2 <mountpoint> 2>/dev/null | sort -h | tail -20`
