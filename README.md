# Docker backup (always-on container + web GUI)

A self-contained, always-running container that backs up **every running Docker
container** on this machine to disk on a schedule, keeps the last **7** daily
backups, and prunes the oldest. New stacks/containers are picked up
automatically — there is nothing to configure when you add a project. A built-in
**web GUI** lets you run manual backups on specific containers, restore, delete
backups, and edit the schedule.

- **Location (source):** `E:\Repositories\docker-backup\` (Git repo;
  see [Source & repository](#source--repository)).
- **Web GUI:** http://127.0.0.1:8088 (localhost only)
- **Backups written to:** `E:\Docker\backups\<YYYY-MM-DD>\<project>\<container>\`
- **Schedule:** runs once on start, then daily at `03:00` local (editable in the GUI).
- **Retention:** 7 daily folders (editable in the GUI).
- **Auto-start:** `restart: unless-stopped` — relaunches with Docker Desktop.

## Web GUI

Open **http://127.0.0.1:8088** after `docker compose up -d`. Four tabs:

- **Backup** — live list of all containers (any state); back up selected ones,
  one container, or all running containers with a click.
- **Restore** — browse every backup on disk by day/project; restore a project
  **end-to-end with one click** (stops the stack, overwrites its volumes,
  starts the containers again, and re-imports the DB where needed) or delete a
  project/day backup.
- **Schedule** — enable/disable, add multiple daily run times (`HH:MM`), and set
  the retention count. Saved to `E:\Docker\backups\_config\schedule.json`.
- **Logs** — live activity log (`E:\Docker\backups\_logs\activity.log`).

The Python scheduler runs inside the same container; there is no host Task
Scheduler involved.

## What gets backed up (per running container)

| Item | File |
|------|------|
| Full container manifest | `inspect.json` |
| MySQL/MariaDB databases | `all-databases.sql.gz` (or `<db>.sql.gz`) via `mysqldump` |
| PostgreSQL databases | `all-databases.sql.gz` via `pg_dumpall` |
| Every named volume | `volume-<name>.tar.gz` |
| Compose file(s) | copied into the project folder |
| Completion marker | `_BACKUP_OK.txt` |

Databases are dumped logically **and** their volumes archived, so you have two
recovery paths.

## Deploy

```powershell
cd E:\Repositories\docker-backup
docker compose build
docker compose up -d
docker ps --filter name=docker-backup
```

> **Docker Desktop:** the `E:` drive must be shared (Settings → Resources →
> File sharing) so the `E:/Docker/backups` bind mount and the sibling helper
> mounts work. `E:\Docker` is already the configured Docker home on this machine.

## Operate

Most operations are easiest from the **web GUI** (http://127.0.0.1:8088).
Equivalent CLI commands:

```powershell
# Watch the daemon / GUI / scheduler
docker logs -f docker-backup

# Force a backup right now from the CLI (GUI "Back up" buttons do the same)
docker exec docker-backup backup.sh                       # all running
docker exec docker-backup backup.sh brajkovic-local-db-1  # specific container(s)

# List what has been backed up
docker exec docker-backup restore.sh --list
```

## Restore

Restore is **fully automated and overwrites current data**. Click **Restore** on
a project in the GUI (or run the CLI below) and the tool runs all four phases in
order, in the background:

1. **Stop** every container belonging to the project (so volume writes are
   consistent).
2. **Overwrite** each named volume — the existing volume contents are wiped and
   replaced with the backed-up snapshot.
3. **Start** the containers again (database containers first).
4. **Re-import** the SQL dump **only** where no volume archive covered the data;
   when a volume archive exists the database is already restored from it, so the
   redundant SQL import is skipped to avoid conflicts. The tool waits for the DB
   to accept connections before importing.

```powershell
# End-to-end restore of a project (stop -> overwrite volumes -> start -> import)
docker exec docker-backup restore.sh <project> latest
docker exec docker-backup restore.sh <project> 2026-05-26
```

Progress shows in the status badge and the **Logs** tab. Only one backup or
restore runs at a time.

Known project names on this machine:
`brajkovic-local`, `villamakar-local`, `local-wordpress-multirent`, and the
standalone `bank-statement-analyzer` (its external volume
`bank-statement-analyzer-data` is recreated on restore).

### Full disaster recovery (machine rebuilt)

The automated restore overwrites **existing** containers. If the containers no
longer exist (e.g. the machine was rebuilt), recreate the stack first so the
tool has containers to stop/start and DBs to import into:

1. Reinstall Docker Desktop and re-create `E:\Docker\backups` (or restore it
   from off-machine copy — see below).
2. `git clone` the project repo(s) and `docker compose up -d` to create the
   containers, empty volumes and databases.
3. `docker exec docker-backup restore.sh <project> latest` — this stops the
   fresh containers, overwrites their volumes with the backup, starts them
   again, and re-imports the DB where needed.

> **Off-machine copy:** this tool protects against container/volume loss. To
> survive total disk failure, also copy `E:\Docker\backups` to another
> disk/cloud (e.g. a scheduled robocopy or your existing restic job).

## Resilience — what if I lose the backup container?

**The backup container is disposable. It stores nothing of its own** — every
bit of state lives on disk via host bind mounts, not inside the container:

| Item | Where it lives | Survives container loss? |
|------|----------------|--------------------------|
| Your actual backups | `E:\Docker\backups\` | ✅ yes |
| Schedule + retention config | `E:\Docker\backups\_config\schedule.json` | ✅ yes |
| Activity log | `E:\Docker\backups\_logs\activity.log` | ✅ yes |
| The tool's source | `E:\Repositories\docker-backup\` (+ GitHub) | ✅ yes |

**Scenario 1 — container deleted/crashed, image still present.** It auto-recovers
via `restart: unless-stopped` when Docker Desktop starts. If you removed it
manually, just bring it back:

```powershell
cd E:\Repositories\docker-backup
docker compose up -d
```

It re-attaches to the same `E:\Docker\backups` folder, reads the existing
schedule, and continues. **No backups are lost.**

**Scenario 2 — image also gone, or machine rebuilt.** Rebuild from source (it is
all in this repo):

```powershell
git clone <your-private-repo-url> E:\Repositories\docker-backup
cd E:\Repositories\docker-backup
docker compose up -d --build
```

As long as `E:\Docker\backups` still exists (or is restored from an off-machine
copy), the rebuilt container immediately sees all previous backups.

> **The only real risk** is losing the **`E:` disk itself** — both your stacks
> *and* their backups live there. Guard against that with an off-machine copy of
> `E:\Docker\backups` (robocopy to another disk, or a restic/cloud job).

## Source & repository

The tool lives in its own Git repository at `E:\Repositories\docker-backup\`
(private GitHub repo). Only the source files are tracked:

```
app.py          web GUI + scheduler (Python stdlib)
backup.sh       one-shot backup engine
restore.sh      end-to-end overwrite restore engine
Dockerfile      docker:27-cli + python3 image
compose.yaml    service definition (ports, env, bind mounts)
README.md       this document
.gitignore      excludes local/runtime artifacts
```

Backups, logs, and config are **not** in the repo — they live under
`E:\Docker\backups` and are intentionally excluded. To update the tool: edit the
source, commit/push, then `docker compose up -d --build` from the repo folder.

> Shell scripts (`*.sh`) and `app.py` must keep **LF** line endings and UTF-8
> (no BOM) so they run under the container's BusyBox `sh`. A `.gitattributes`
> entry enforces this on checkout.

## Configuration

Schedule times and retention are managed in the **GUI** (persisted in
`E:\Docker\backups\_config\schedule.json`). The `compose.yaml` env vars below
set the initial defaults and runtime behavior; edit them then
`docker compose up -d --build`:

| Var | Default | Meaning |
|-----|---------|---------|
| `BACKUP_HOUR` | `3` | Default daily hour, used until a schedule is saved in the GUI |
| `RETENTION_DAYS` | `7` | Default retention (editable in the GUI) |
| `TZ` | `Europe/Zagreb` | Timezone for the schedule |
| `GUI_PORT` | `8088` | Web GUI port (also map it in `ports:`) |
| `HELPER_IMAGE` | `alpine:3.20` | Image used to tar/untar volumes |
