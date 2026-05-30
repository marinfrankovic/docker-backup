# Docker Backup

An always-on container that backs up your **other Docker containers** — their
volumes, databases, and compose files — and lets you manage everything from a
simple **web GUI**: run manual backups, schedule automatic ones, and restore.

- **One folder holds everything.** Point the tool at a backups folder; on any
  rebuild, point it at the same folder and every old backup is found again.
- **Picks up new stacks automatically** — no config when you add containers.
- **Backs up stopped containers too** (volumes are archived even when off).

> **Security note:** the tool needs the Docker socket (`/var/run/docker.sock`)
> to control your containers, so run it only on a machine you trust. The web GUI
> is bound to `localhost` only.

## Requirements

- Docker Desktop (Windows/macOS) or Docker Engine + Compose (Linux).
- A folder on the host to store backups.

## Setup

You need the project files on your machine. Pick **one** of the two options
below, then continue with [Configure & run](#configure--run).

### Option A — Download (no Git needed)

1. Go to **https://github.com/marinfrankovic/docker-backup**.
2. Click **Code ▸ Download ZIP**.
3. Unzip it and open a terminal in the unzipped folder.

### Option B — Clone with Git

```bash
git clone https://github.com/marinfrankovic/docker-backup.git
cd docker-backup
```

## Configure & run

1. **Make your config file** by copying the template:
   ```bash
   cp .env.example .env            # Linux/macOS
   ```
   ```powershell
   Copy-Item .env.example .env     # Windows
   ```

2. **Edit `.env`** and set two values (the rest have sensible defaults):
   - `BACKUP_ROOT_HOST` — folder where backups are stored
     (Linux/macOS e.g. `/srv/docker-backups`; Windows use forward slashes
     e.g. `E:/Docker/backups`).
   - `TZ` — your timezone, e.g. `Europe/London`.

3. **Create that folder** if it doesn't exist:
   ```bash
   mkdir -p /srv/docker-backups                                   # Linux/macOS
   ```
   ```powershell
   New-Item -ItemType Directory -Force E:\Docker\backups | Out-Null   # Windows
   ```
   > **Windows only:** in Docker Desktop → *Settings ▸ Resources ▸ File sharing*,
   > share the drive your backups folder lives on, then *Apply & restart*.

4. **Start it:**
   ```bash
   docker compose up -d --build
   ```

5. **Open the GUI:** http://127.0.0.1:8088

That's it. The container stays running and restarts with Docker. To update later,
get the latest files (re-download or `git pull`) and run `docker compose up -d --build` again.

## Using the web GUI

- **Backup** — see all containers and back up one, a whole stack, selected ones,
  or everything. Manual backups go to the `_manual` folder.
- **Schedules** — add daily/weekly/monthly jobs. Each has a **scope**: **All**
  (everything), **Selected** (pick projects/containers), or **All Running**
  (only what's running). Each also has a "keep last N runs" retention and its own
  **Save** button.
- **Restore** — every backup found on disk, grouped by date. Expand one to
  restore a single project or the whole run. New backups appear automatically.
- **Settings** — backups folder, optional subfolder for new runs, manual
  retention.
- **Logs** — live activity log.

## What gets backed up

Per container: a manifest (`inspect.json`), all named volumes
(`volume-*.tar.gz`), databases (`mysqldump` / `pg_dumpall`, gzipped), and the
compose file. A `_BACKUP_OK.txt` marks a completed run.

Databases are saved twice — as a SQL dump **and** inside the volume archive —
for two recovery paths. Stopped containers skip the live SQL dump but their
volumes are still archived, so their data is fully captured.

Layout on disk:
```
<backups-folder>/<bucket>/<YYYY-MM-DD_HHMMSS>/<project>/<container>/
```
`bucket` is the schedule name, or `_manual` for ad-hoc backups.

## Restore (how it works)

Restoring **overwrites current data**. For the chosen project (or whole run) the
tool: stops its containers → replaces each volume with the backup → starts them
again (databases first) → re-imports the SQL dump only where a volume didn't
already cover it. Only one backup or restore runs at a time.

CLI equivalents (the GUI does the same):
```bash
docker exec docker-backup backup.sh _manual/now              # back up all
docker exec docker-backup backup.sh _manual/now --running    # only running
docker exec docker-backup restore.sh --list                  # list runs
docker exec docker-backup restore.sh _manual/2026-05-29_141500 <project>
```

## Disaster recovery

The container stores nothing of its own — backups, schedules, settings, and logs
all live in your backups folder. The only real risk is losing the **disk** that
holds it, so keep an off-machine copy (rsync/robocopy, or restic/cloud).

To recover after a rebuild: put the backups folder back at the same
`BACKUP_ROOT_HOST` path, run `docker compose up -d --build`, and every old backup
is listed again. If the original app containers no longer exist, recreate them
first (`docker compose up -d` in their repos), then restore from the GUI.

## Configuration (`.env`)

| Var | Default | Meaning |
|-----|---------|---------|
| `BACKUP_ROOT_HOST` | _(required)_ | Folder for all backups (forward slashes on Windows) |
| `TZ` | `UTC` | Timezone for the scheduler |
| `BACKUP_HOUR` | `3` | Time for the first auto-created schedule |
| `RETENTION_DAYS` | `7` | Default "keep last N runs" for new schedules |
| `GUI_PORT` | `8088` | Web GUI port (localhost only) |
| `HELPER_IMAGE` | `alpine:3.20` | Image used to archive volumes |

Your `.env` is git-ignored, so your settings are never committed.

## License

[MIT](LICENSE).
