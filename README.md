# Docker backup (always-on container + web GUI)

A self-contained, always-running container that backs up Docker containers on
this machine to disk. You define **multiple schedules** — each with its own
frequency (daily / weekly / monthly), set of containers, and retention — and the
tool keeps the most recent runs per schedule and prunes the rest. New
stacks/containers are picked up automatically. A built-in **web GUI** lets you
run manual backups, manage schedules, browse the backups folder, and restore.

The **backups root folder is the single source of truth.** Everything the tool
produces and everything it can restore lives under that one folder, so after a
rebuild you only need to point the container at the same root — every prior
backup is rediscovered and listed on the Restore page automatically.

- **Location (source):** `E:\Repositories\docker-backup\` (Git repo;
  see [Source & repository](#source--repository)).
- **Web GUI:** http://127.0.0.1:8088 (localhost only)
- **Backups root:** `E:\Docker\backups\` (the host folder mounted at `/backups`)
- **Run layout:** `<root>\<destination?>\<bucket>\<YYYY-MM-DD_HHMMSS>\<project>\<container>\`
  where `bucket` is the schedule id, or `_manual` for ad-hoc backups.
- **Schedules:** any number; each daily/weekly/monthly with its own retention.
- **Auto-start:** `restart: unless-stopped` — relaunches with Docker Desktop.

## Web GUI

Open **http://127.0.0.1:8088** after `docker compose up -d`. Five tabs:

- **Backup** — live list of all containers (any state); back up selected ones,
  one container, a whole stack, or all running containers with a click. Manual
  backups land in the `_manual` bucket.
- **Restore** — two ways to find a backup:
  - *Backup runs found* — the tool scans the root and lists every completed run
    (newest first). Restore a single project or the **whole run** end-to-end,
    or delete a run.
  - *Browse backups folder* — a filesystem browser confined to the root. For
    disaster recovery, navigate manually and restore any folder marked as a
    run, or restore an **individual project** from inside a run.
- **Schedules** — add/remove schedules. Each tile is collapsible and shows a
  summary in its header. Containers are grouped by project (toggle a whole
  project or individual containers). Each has a name, frequency
  (daily/weekly/monthly), time, container selection (all-running or explicit),
  and a "keep last N runs" retention. Each schedule has its **own Save button**;
  unsaved edits are flagged and you are warned before leaving the tab or page.
  Saved to `E:\Docker\backups\_config\schedules.json`.
- **Settings** — shows the backups root, an optional **destination subfolder**
  under the root for new runs, and the manual-backup retention. Saved to
  `E:\Docker\backups\_config\settings.json`.
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
| Completion marker | `_BACKUP_OK.txt` (at the run folder; marks a discoverable run) |

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

# Force a backup right now from the CLI (GUI "Back up" buttons do the same).
# The first argument is the run sub-path (relative to the root).
docker exec docker-backup backup.sh _manual/manual-now                       # all running
docker exec docker-backup backup.sh _manual/manual-now brajkovic-local-db-1  # specific container(s)

# List backup runs found on disk
docker exec docker-backup restore.sh --list
```

## Restore

Restore is **fully automated and overwrites current data**. Pick a run in the
GUI (or run the CLI below) and the tool runs all four phases in order, in the
background, for the chosen project — or for every project in the run:

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
# Restore from a run sub-path (relative to the root). Add a project to limit it.
docker exec docker-backup restore.sh _manual/2026-05-29_141500
docker exec docker-backup restore.sh nightly/2026-05-29_030000 brajkovic-local

# Legacy day folders from the previous version still work
docker exec docker-backup restore.sh 2026-05-26 villamakar-local
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
3. `docker exec docker-backup restore.sh <run> <project>` — this stops the
   fresh containers, overwrites their volumes with the backup, starts them
   again, and re-imports the DB where needed. Use the **Restore** tab to find
   the exact run sub-path, or `restore.sh --list`.

> **Off-machine copy:** this tool protects against container/volume loss. To
> survive total disk failure, also copy `E:\Docker\backups` to another
> disk/cloud (e.g. a scheduled robocopy or your existing restic job).

## Resilience — what if I lose the backup container?

**The backup container is disposable. It stores nothing of its own** — every
bit of state lives on disk via host bind mounts, not inside the container:

| Item | Where it lives | Survives container loss? |
|------|----------------|--------------------------|
| Your actual backups | `E:\Docker\backups\` | ✅ yes |
| Schedules config | `E:\Docker\backups\_config\schedules.json` | ✅ yes |
| Destination/manual settings | `E:\Docker\backups\_config\settings.json` | ✅ yes |
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
schedules, and continues. **No backups are lost.**

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

Schedules, the destination subfolder, and manual retention are managed in the
**GUI** (persisted in `E:\Docker\backups\_config\schedules.json` and
`settings.json`). The `compose.yaml` env vars below set initial defaults and
runtime behavior; edit them then `docker compose up -d --build`:

| Var | Default | Meaning |
|-----|---------|---------|
| `BACKUP_HOUR` | `3` | Default time for the seed schedule (first run only) |
| `RETENTION_DAYS` | `7` | Default "keep last N runs" for new schedules |
| `BACKUP_ROOT_CONTAINER` | `/backups` | Backups root inside the container |
| `BACKUP_ROOT_HOST` | `E:/Docker/backups` | Same host path as the `/backups` mount (for helper-container volume mounts) |
| `TZ` | `Europe/Zagreb` | Timezone for the scheduler |
| `GUI_PORT` | `8088` | Web GUI port (also map it in `ports:`) |
| `HELPER_IMAGE` | `alpine:3.20` | Image used to tar/untar volumes |
