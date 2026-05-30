# Docker Backup (always-on container + web GUI)

A self-contained, always-running container that backs up your **other Docker
containers** to disk — their volumes, databases, and compose files. You define
**multiple schedules** — each with its own frequency (daily / weekly / monthly),
scope (all containers, a selected set, or only running ones), and retention —
and the tool keeps the most recent runs per schedule and prunes the rest. New
stacks/containers are picked up automatically. A built-in **web GUI** lets you
run manual backups, manage schedules, and restore — all from the browser.

> **The backups root folder is the single source of truth.** Everything the tool
> produces and can restore lives under one folder, so after a rebuild you only
> point the container at the same root — every prior backup is rediscovered and
> listed on the Restore page automatically.

> **Security note:** this tool mounts the Docker socket (`/var/run/docker.sock`),
> which gives it full control of the Docker engine on the host — that is how it
> discovers, stops, starts, and dumps your containers. Only run it on a machine
> you trust. The web GUI is bound to `127.0.0.1` (localhost) by default.

## Contents

- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Web GUI](#web-gui)
- [What gets backed up](#what-gets-backed-up)
- [Restore](#restore)
- [CLI reference](#cli-reference)
- [Recovery & resilience](#recovery--resilience)
- [Configuration](#configuration)
- [Project layout](#project-layout)
- [License](#license)

## Requirements

- **Docker Desktop** (Windows/macOS) or **Docker Engine + Compose** (Linux),
  with the Linux container engine running.
- **Git** (to clone this repo).
- A folder on the host where backups will be stored.

## Quick start

1. **Clone the repo and enter it:**
   ```bash
   git clone <repo-url> docker-backup
   cd docker-backup
   ```

2. **Create your config file** from the template:
   ```bash
   # Linux/macOS
   cp .env.example .env
   ```
   ```powershell
   # Windows PowerShell
   Copy-Item .env.example .env
   ```
   Then open `.env` and set **two** things (the rest have sensible defaults):
   - `BACKUP_ROOT_HOST` — the host folder where backups are stored
     (e.g. `/srv/docker-backups`, or on Windows use forward slashes:
     `E:/Docker/backups`).
   - `TZ` — your timezone (e.g. `Europe/London`, `America/New_York`).

3. **Create the backups folder** if it does not exist yet:
   ```bash
   mkdir -p /srv/docker-backups            # Linux/macOS — match BACKUP_ROOT_HOST
   ```
   ```powershell
   New-Item -ItemType Directory -Force E:\Docker\backups | Out-Null   # Windows
   ```
   > **Windows only:** in Docker Desktop → *Settings → Resources → File sharing*,
   > make sure the drive holding your backups folder is shared, then *Apply &
   > restart*. This is required for the bind mount and helper-container mounts.

4. **Build and start:**
   ```bash
   docker compose up -d --build
   docker ps --filter name=docker-backup
   ```
   On first start the tool seeds a `_config/` folder under your backups root with
   one daily schedule (at `BACKUP_HOUR`, keeping `RETENTION_DAYS` runs). You edit
   these later from the GUI.

5. **Open the GUI** at the address for your `GUI_PORT`
   (default **http://127.0.0.1:8088**).

6. **Run a first backup** to confirm it works: on the **Backup** tab click
   *Back up all containers* (lands in the `_manual` bucket), then check it
   appears under **Restore → Backup runs found**.

7. **Create your schedule(s)** on the **Schedules** tab, then click that
   schedule's **Save** button.

8. **(Recommended)** Keep an off-machine copy of your backups root to survive a
   full disk loss — see [Recovery & resilience](#recovery--resilience).

## Web GUI

Open the GUI (default **http://127.0.0.1:8088**) after the container is running.
The top-right corner shows the current local date and time.

- **Backup** — live list of all containers (any state). Back up selected ones,
  one container, a whole stack, or all containers with a click. Click a
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
  summary in its header; expand one to edit it. Each schedule has a **scope** with
  three options (in order): **All** (every container, running or stopped),
  **Selected** (pick which projects/containers to include — grouped by project,
  toggle a whole project or individual containers), and **All Running** (only
  containers running at backup time). Each schedule also has a name, frequency,
  time, and a "keep last N runs" retention, its **own Save button**; unsaved
  edits are flagged and you are warned before leaving the tab or page. Saved to
  `<backups-root>/_config/schedules.json`.
- **Settings** — the backups root, an optional **destination subfolder** under
  the root for new runs, and the manual-backup retention. Saved to
  `<backups-root>/_config/settings.json`.
- **Logs** — live activity log (`<backups-root>/_logs/activity.log`).

## What gets backed up

Per container (running **or** stopped):

| Item | File |
|------|------|
| Full container manifest | `inspect.json` |
| MySQL/MariaDB databases | `all-databases.sql.gz` (or `<db>.sql.gz`) via `mysqldump` |
| PostgreSQL databases | `all-databases.sql.gz` via `pg_dumpall` |
| Every named volume | `volume-<name>.tar.gz` |
| Compose file(s) | copied into the project folder |
| Completion marker | `_BACKUP_OK.txt` (at the run folder; marks a discoverable run) |

Databases are dumped logically **and** their volumes archived, giving you two
recovery paths. For a **stopped** container the live SQL dump is skipped (it
can't be queried), but its named volumes are still archived, so its data is
fully captured and restorable.

Run layout on disk:

```
<backups-root>/<destination?>/<bucket>/<YYYY-MM-DD_HHMMSS>/<project>/<container>/
```
`bucket` is the schedule id, or `_manual` for ad-hoc backups.

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

## CLI reference

Most operations are easiest from the GUI, but equivalent CLI commands exist:

```bash
# Watch the daemon / GUI / scheduler
docker logs -f docker-backup

# Force a backup now (GUI "Back up" buttons do the same).
# First argument is the run sub-path (relative to the root).
docker exec docker-backup backup.sh _manual/manual-now              # all containers
docker exec docker-backup backup.sh _manual/manual-now --running    # only running containers
docker exec docker-backup backup.sh _manual/manual-now <container>  # specific container(s)

# List backup runs found on disk
docker exec docker-backup restore.sh --list

# Restore from a run sub-path. Add a project name to limit it to that project.
docker exec docker-backup restore.sh _manual/2026-05-29_141500
docker exec docker-backup restore.sh nightly/2026-05-29_030000 <project>
```

## Recovery & resilience

**The backup container is disposable — it stores nothing of its own.** Every bit
of state lives on disk via host bind mounts, not inside the container:

| Item | Where it lives | Survives container loss? |
|------|----------------|--------------------------|
| Your actual backups | `<backups-root>/` | ✅ yes |
| Schedules config | `<backups-root>/_config/schedules.json` | ✅ yes |
| Destination/manual settings | `<backups-root>/_config/settings.json` | ✅ yes |
| Activity log | `<backups-root>/_logs/activity.log` | ✅ yes |
| The tool's source | this repo (+ your Git remote) | ✅ yes |

> **The only real risk** is losing the **disk** that holds the backups root —
> both your stacks *and* their backups may live there. Guard against that with an
> off-machine copy of your backups root (rsync/robocopy to another disk, or a
> restic/cloud job).

### Update / redeploy the tool

After editing the source (or pulling updates), rebuild from the repo folder:

```bash
git pull
docker compose up -d --build
docker ps --filter name=docker-backup
```

If the container was just deleted/stopped (image still present), it auto-recovers
via `restart: unless-stopped` when Docker starts; to bring it back manually run
`docker compose up -d`. Either way it re-attaches to the same backups root, reads
the existing schedules, and continues — **no backups are lost.**

### Rebuild with an existing backups folder

Because the backups root is the single source of truth, nothing needs to be
imported — the tool rediscovers everything.

1. **Put the backups folder in place** at your `BACKUP_ROOT_HOST` path (with its
   `_config/`, `_logs/`, and existing run folders). If restoring from an
   off-machine copy, put it back at that exact path first.
2. **(Windows)** confirm the drive is shared with Docker Desktop.
3. **Get the source and (re)build:**
   ```bash
   git clone <repo-url> docker-backup   # if not already present
   cd docker-backup
   cp .env.example .env                 # set BACKUP_ROOT_HOST + TZ
   docker compose up -d --build
   ```
4. **Open the GUI.** The tool re-reads the existing `_config/` files, and
   **Restore → Backup runs found** immediately lists every prior run on disk. No
   backups are lost and no schedules need recreating.

### Full disaster recovery (machine rebuilt)

The automated restore overwrites **existing** containers. If the containers no
longer exist (e.g. the machine was rebuilt), recreate the stacks first so the
tool has containers to stop/start and DBs to import into:

1. Reinstall Docker and put your backups root back in place (restore it from your
   off-machine copy if needed).
2. `git clone` your application repo(s) and `docker compose up -d` to create the
   containers, empty volumes, and databases.
3. `docker exec docker-backup restore.sh <run> <project>` — this stops the fresh
   containers, overwrites their volumes with the backup, starts them again, and
   re-imports the DB where needed. Use the **Restore** tab to find the exact run
   sub-path, or `restore.sh --list`.

## Configuration

All host-specific settings come from your local `.env` file (copied from
`.env.example`). The `.env` file is **git-ignored**, so your settings are never
committed. After changing `.env`, run `docker compose up -d --build`.

| Var | Default | Meaning |
|-----|---------|---------|
| `BACKUP_ROOT_HOST` | _(required)_ | Host folder for all backups; also used for helper-container volume mounts. Use forward slashes on Windows. |
| `TZ` | `UTC` | Timezone for the scheduler |
| `BACKUP_HOUR` | `3` | Time for the seed schedule (first run only) |
| `RETENTION_DAYS` | `7` | Default "keep last N runs" for new schedules |
| `GUI_PORT` | `8088` | Web GUI port (localhost only) |
| `HELPER_IMAGE` | `alpine:3.20` | Image used to tar/untar volumes |

Schedules, the destination subfolder, and manual retention are managed in the
**GUI** (persisted under `<backups-root>/_config/`).

## Project layout

Only the source files are tracked in Git:

```
app.py          web GUI + scheduler (Python stdlib)
backup.sh       one-shot backup engine
restore.sh      end-to-end overwrite restore engine
Dockerfile      docker:27-cli + python3 image
compose.yaml    service definition (ports, env, bind mounts)
.env.example    config template — copy to .env and edit
README.md       this document
LICENSE         MIT license
.gitignore      excludes local/runtime artifacts and .env
```

Backups, logs, config, and your `.env` are **not** in the repo — they live under
your backups root and are intentionally excluded.

> Shell scripts (`*.sh`) and `app.py` must keep **LF** line endings and UTF-8
> (no BOM) so they run under the container's BusyBox `sh`. A `.gitattributes`
> entry enforces this on checkout.

## License

Released under the [MIT License](LICENSE).
