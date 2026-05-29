# Docker backup (always-on container + web GUI)

A self-contained, always-running container that backs up the other Docker
containers on this machine to disk. You define **multiple schedules** — each with
its own frequency (daily / weekly / monthly), set of containers, and retention —
and the tool keeps the most recent runs per schedule and prunes the rest. New
stacks/containers are picked up automatically. A built-in **web GUI** lets you
run manual backups, manage schedules, and restore — all from the browser.

> **The backups root folder is the single source of truth.** Everything the tool
> produces and can restore lives under one folder, so after a rebuild you only
> point the container at the same root — every prior backup is rediscovered and
> listed on the Restore page automatically.

## Contents

- [Key facts](#key-facts)
- [First run & setup](#first-run--setup)
- [Web GUI](#web-gui)
- [What gets backed up](#what-gets-backed-up)
- [Restore](#restore)
- [CLI reference](#cli-reference)
- [Recovery & resilience](#recovery--resilience)
- [Configuration](#configuration)
- [Source & repository](#source--repository)

## Key facts

| | |
|---|---|
| **Source (repo)** | `E:\Repositories\docker-backup\` (private GitHub repo) |
| **Web GUI** | http://127.0.0.1:8088 (localhost only) |
| **Backups root** | `E:\Docker\backups\` (host folder mounted at `/backups`) |
| **Run layout** | `<root>\<destination?>\<bucket>\<YYYY-MM-DD_HHMMSS>\<project>\<container>\` — `bucket` is the schedule id, or `_manual` for ad-hoc backups |
| **Schedules** | Any number; each daily/weekly/monthly with its own retention |
| **Auto-start** | `restart: unless-stopped` — relaunches with Docker Desktop |
| **Scheduler** | Runs inside the container (no host Task Scheduler) |

## First run & setup

Use this the **first time** you set the tool up on a machine.

1. **Install prerequisites:** Docker Desktop (Linux engine running) and Git.
2. **Share the `E:` drive with Docker.** In Docker Desktop go to
   *Settings → Resources → File sharing*, ensure `E:` (or at least `E:\Docker`)
   is shared, then *Apply & restart*. This is required for the
   `E:/Docker/backups` bind mount and the helper-container mounts to work.
3. **Get the source and create the backups folder:**
   ```powershell
   git clone <your-private-repo-url> E:\Repositories\docker-backup
   cd E:\Repositories\docker-backup
   New-Item -ItemType Directory -Force E:\Docker\backups | Out-Null
   ```
4. **Build and start the container:**
   ```powershell
   docker compose up -d --build
   docker ps --filter name=docker-backup
   ```
   On first start the tool seeds `E:\Docker\backups\_config\schedules.json` and
   `settings.json` (one daily schedule at `BACKUP_HOUR`, keeping `RETENTION_DAYS`
   runs). You edit these later from the GUI.
5. **Open the GUI** at **http://127.0.0.1:8088**.
6. **Run a first backup** to confirm everything works: on the **Backup** tab
   click *Back up all running* (lands in the `_manual` bucket), then check it
   appears under **Restore → Backup runs found**.
7. **Create your schedule(s)** on the **Schedules** tab — adjust the seeded
   schedule or add new ones, then click that schedule's **Save** button.
8. **(Recommended) Set up an off-machine copy** of `E:\Docker\backups` to survive
   a full disk loss — see [Recovery & resilience](#recovery--resilience).

## Web GUI

Open **http://127.0.0.1:8088** after the container is running. The top-right
corner shows the current local date and time (`DD.MM.YYYY` with 24-hour clock).
Five tabs:

- **Backup** — live list of all containers (any state). Back up selected ones,
  one container, a whole stack, or all running containers with a click. Click a
  stack's name (📦) to select/deselect all containers in that stack at once.
  Manual backups land in the `_manual` bucket.
- **Restore** — lists every completed backup run found under the root, **grouped
  by run date** (`DD.MM.YYYY`, newest day first; today's date is expanded, older
  days collapsed). Each run shows its start time and bucket, and every project
  inside a run shows its **own exact backup time** (`HH:MM:SS`) — so when several
  schedules run on the same day you can see precisely when each project was
  captured. Expand a run to restore a single project or the **whole run**
  end-to-end, or delete a run. **You don't import anything:** if you mount a root
  folder that already contains older backups, they are discovered and listed
  automatically the moment the tool starts. The list **auto-refreshes in the
  background** as soon as a backup finishes, so newly completed runs appear
  without reloading the page.
- **Schedules** — add/remove schedules. Each tile is collapsed by default with a
  summary in its header; expand one to edit it. Containers are grouped by project
  (toggle a whole project or individual containers). Each schedule has a name,
  frequency, time, container selection (all-running or explicit), and a "keep
  last N runs" retention. Each schedule has its **own Save button**; unsaved edits
  are flagged and you are warned before leaving the tab or page. Saved to
  `E:\Docker\backups\_config\schedules.json`.
- **Settings** — the backups root, an optional **destination subfolder** under
  the root for new runs, and the manual-backup retention. Saved to
  `E:\Docker\backups\_config\settings.json`.
- **Logs** — live activity log (`E:\Docker\backups\_logs\activity.log`).

## What gets backed up

Per running container:

| Item | File |
|------|------|
| Full container manifest | `inspect.json` |
| MySQL/MariaDB databases | `all-databases.sql.gz` (or `<db>.sql.gz`) via `mysqldump` |
| PostgreSQL databases | `all-databases.sql.gz` via `pg_dumpall` |
| Every named volume | `volume-<name>.tar.gz` |
| Compose file(s) | copied into the project folder |
| Completion marker | `_BACKUP_OK.txt` (at the run folder; marks a discoverable run) |

Databases are dumped logically **and** their volumes archived, giving you two
recovery paths.

## Restore

> **Restore is fully automated and overwrites current data.** Pick a run in the
> GUI (or use the [CLI](#cli-reference)) and the tool runs all four phases in the
> background, for the chosen project — or for every project in the run:

1. **Stop** every container belonging to the project (so volume writes are
   consistent).
2. **Overwrite** each named volume — existing contents are wiped and replaced
   with the backed-up snapshot.
3. **Start** the containers again (database containers first).
4. **Re-import** the SQL dump **only** where no volume archive covered the data.
   When a volume archive exists the database is already restored from it, so the
   redundant SQL import is skipped to avoid conflicts. The tool waits for the DB
   to accept connections before importing.

Progress shows in the status badge and the **Logs** tab. Only one backup or
restore runs at a time.

Known project names on this machine: `brajkovic-local`, `villamakar-local`,
`local-wordpress-multirent`, and the standalone `bank-statement-analyzer` (its
external volume `bank-statement-analyzer-data` is recreated on restore).

For recovery after a machine rebuild, see
[Full disaster recovery](#full-disaster-recovery-machine-rebuilt).

## CLI reference

Most operations are easiest from the GUI, but equivalent CLI commands exist:

```powershell
# Watch the daemon / GUI / scheduler
docker logs -f docker-backup

# Force a backup now (GUI "Back up" buttons do the same).
# First argument is the run sub-path (relative to the root).
docker exec docker-backup backup.sh _manual/manual-now                       # all running
docker exec docker-backup backup.sh _manual/manual-now brajkovic-local-db-1  # specific container(s)

# List backup runs found on disk
docker exec docker-backup restore.sh --list

# Restore from a run sub-path. Add a project name to limit it to that project.
docker exec docker-backup restore.sh _manual/2026-05-29_141500
docker exec docker-backup restore.sh nightly/2026-05-29_030000 brajkovic-local

# Legacy day folders from the previous version still work
docker exec docker-backup restore.sh 2026-05-26 villamakar-local
```

## Recovery & resilience

**The backup container is disposable — it stores nothing of its own.** Every bit
of state lives on disk via host bind mounts, not inside the container:

| Item | Where it lives | Survives container loss? |
|------|----------------|--------------------------|
| Your actual backups | `E:\Docker\backups\` | ✅ yes |
| Schedules config | `E:\Docker\backups\_config\schedules.json` | ✅ yes |
| Destination/manual settings | `E:\Docker\backups\_config\settings.json` | ✅ yes |
| Activity log | `E:\Docker\backups\_logs\activity.log` | ✅ yes |
| The tool's source | `E:\Repositories\docker-backup\` (+ GitHub) | ✅ yes |

> **The only real risk** is losing the **`E:` disk itself** — both your stacks
> *and* their backups live there. Guard against that with an off-machine copy of
> `E:\Docker\backups` (robocopy to another disk, or a restic/cloud job).

### Update / redeploy the tool

After editing the source, rebuild from the repo folder:

```powershell
cd E:\Repositories\docker-backup
docker compose up -d --build
docker ps --filter name=docker-backup
```

If the container was just deleted/stopped (image still present), it auto-recovers
via `restart: unless-stopped` when Docker Desktop starts; to bring it back
manually run `docker compose up -d`. Either way it re-attaches to the same
`E:\Docker\backups` folder, reads the existing schedules, and continues — **no
backups are lost.**

### Rebuild with an existing backups folder

Use this when you are **reinstalling / rebuilding the tool** but already have a
populated `E:\Docker\backups` folder (from this machine or restored from an
off-machine copy). Because the backups root is the single source of truth,
nothing needs to be imported — the tool rediscovers everything.

1. **Put the backups folder in place** at `E:\Docker\backups` (with its
   `_config\`, `_logs\`, and existing run folders). If restoring from an
   off-machine copy, put it back at that exact path first.
2. **Confirm `E:` is shared** with Docker Desktop
   (see [First run](#first-run--setup) step 2).
3. **Get the source and (re)build:**
   ```powershell
   git clone <your-private-repo-url> E:\Repositories\docker-backup   # if not already present
   cd E:\Repositories\docker-backup
   docker compose up -d --build
   ```
4. **Open the GUI** at **http://127.0.0.1:8088**. The tool re-reads the existing
   `_config\` files, and **Restore → Backup runs found** immediately lists every
   prior run on disk. No backups are lost and no schedules need recreating.

> This restores only the **backup tool**. To recover the actual application
> containers/volumes from a backup, see [Restore](#restore) and
> [Full disaster recovery](#full-disaster-recovery-machine-rebuilt).

### Full disaster recovery (machine rebuilt)

The automated restore overwrites **existing** containers. If the containers no
longer exist (e.g. the machine was rebuilt), recreate the stacks first so the
tool has containers to stop/start and DBs to import into:

1. Reinstall Docker Desktop and put `E:\Docker\backups` back in place (restore it
   from your off-machine copy if needed).
2. `git clone` the application repo(s) and `docker compose up -d` to create the
   containers, empty volumes, and databases.
3. `docker exec docker-backup restore.sh <run> <project>` — this stops the fresh
   containers, overwrites their volumes with the backup, starts them again, and
   re-imports the DB where needed. Use the **Restore** tab to find the exact run
   sub-path, or `restore.sh --list`.

## Configuration

Schedules, the destination subfolder, and manual retention are managed in the
**GUI** (persisted in `E:\Docker\backups\_config\schedules.json` and
`settings.json`). The `compose.yaml` env vars below set initial defaults and
runtime behavior; edit them, then `docker compose up -d --build`:

| Var | Default | Meaning |
|-----|---------|---------|
| `BACKUP_HOUR` | `3` | Time for the seed schedule (first run only) |
| `RETENTION_DAYS` | `7` | Default "keep last N runs" for new schedules |
| `BACKUP_ROOT_CONTAINER` | `/backups` | Backups root inside the container |
| `BACKUP_ROOT_HOST` | `E:/Docker/backups` | Same host path as the `/backups` mount (for helper-container volume mounts) |
| `TZ` | `Europe/Zagreb` | Timezone for the scheduler |
| `GUI_PORT` | `8088` | Web GUI port (also map it in `ports:`) |
| `HELPER_IMAGE` | `alpine:3.20` | Image used to tar/untar volumes |

## Source & repository

The tool lives in its own private Git repo at `E:\Repositories\docker-backup\`.
Only the source files are tracked:

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
`E:\Docker\backups` and are intentionally excluded.

> Shell scripts (`*.sh`) and `app.py` must keep **LF** line endings and UTF-8
> (no BOM) so they run under the container's BusyBox `sh`. A `.gitattributes`
> entry enforces this on checkout.
