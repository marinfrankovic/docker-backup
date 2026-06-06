# Using the web page

Open **<http://127.0.0.1:8088>**. There are five tabs.

- **Backup** — back up one container, a group, a few you pick, or everything.
  **Expand any container** to choose exactly which volumes and bind mounts to
  include, and tick **Exclude network mounts** to drop its NFS/SMB/CIFS shares
  for that run. Manual backups go to the `_manual` folder.
- **Schedules** — add daily/weekly/monthly jobs. Each has a **scope**: **All**
  (everything), **Selected** (pick what to include), or **All Running** (only
  what's running). In **Selected** scope every container chip has its own
  **▸ mounts** panel for per-container/per-mount choices; **All** / **All
  Running** scopes offer a single **Exclude network mounts** toggle. Each
  schedule keeps the last N runs and has its own **Save** button, plus a
  **▶ Run now** button that saves any pending edits and runs it immediately.
- **Restore** — every backup found on disk, grouped by date. Open one to restore
  a single project or the whole run. See [Restoring](RESTORE.md).
- **Settings** — the backup folder (auto-read from the tool's `/backups` mount),
  an optional subfolder for new runs, and how many manual backups to keep.
- **Logs** — live activity log.

While a backup or restore runs, the header shows a **live progress bar** with a
`done / total` count and the current item (e.g. `14/24 — webapp · volume
data`). A red **⏹ Stop** button cancels the job — it stops the running script
and any helper containers, then returns to idle. The interrupted run is left
incomplete (no `_BACKUP_OK.txt`) and can be deleted from **Restore**.

## What gets backed up

For each container the tool saves:

- a details file (`inspect.json`),
- all data volumes (`volume-*.tar.gz`),
- bind-mounted folders/files (`bind-*.tar.gz`, listed in `binds.tsv`),
- databases (via `mysqldump` / `pg_dumpall`, compressed),
- its compose file.

A `_BACKUP_OK.txt` file marks a finished backup. Databases are stored **both** as
a dump **and** inside the volume copy, so you have two ways to recover. Stopped
containers skip the live dump, but their volumes are still saved.

This means **every container is fully restorable with all its config** — whether
it stores data in a named Docker volume *or* a bind-mounted host folder (for
example AdGuard Home's `conf` folder). This applies to every Docker project, not
any specific app.

### You choose what to include, per container, per run

By default every volume and bind mount of every container is backed up. On the
**Backup** tab (and in a schedule's **Selected** scope) you can expand any
container and tick exactly which volumes and bind mounts to keep — handy for
dropping a big cache or media folder while still saving its database volume.
There are no global skip patterns: nothing is excluded unless you say so.

### Network shares

Tick **Exclude network mounts** on a container (or use the schedule-wide toggle
for All / All Running scopes) to skip any mounts on NFS/SMB/CIFS, so you don't
pull gigabytes off another server. Volume type comes from `docker volume
inspect`; bind type is probed with `stat -f`.

### Always skipped

A few host paths are always skipped because restoring them would break the
container: the Docker socket, and system files like `/proc`, `/sys`, `/dev`,
`/etc/localtime`, `/etc/timezone`, `/etc/hostname`, `/etc/hosts`, and
`/etc/resolv.conf`. Anything inside the backup folder itself is skipped too.

### Where files land

```
<backup-folder>/<bucket>/<YYYY-MM-DD_HHMMSS>/<project>/<container>/
```

`bucket` is the schedule name, or `_manual` for backups you start by hand.
