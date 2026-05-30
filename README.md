# Docker Backup

An always-on container that backs up your **other Docker containers** — their
volumes, databases, and compose files — all from a simple **web page**: run
backups by hand, schedule automatic ones, and restore.

- **One folder holds everything.** Point the tool at a backup folder; after any
  reinstall, point it at the same folder and every old backup is found again.
- **Notices new containers on its own** — nothing to set up when you add one.
- **Backs up stopped containers too** (their data is still saved).

> **Safety note:** the tool needs access to Docker so it can back up your
> containers, so run it only on a computer you trust. The web page is only
> reachable from your own computer.

## What you need

- **Docker Desktop** (Windows/macOS) or **Docker Engine + Compose** (Linux).
- A folder on your computer where backups will be saved.

---

## Step-by-step guide (no experience needed)

Follow these steps in order. They assume you've never used Docker before.

### Step 1 — Install Docker Desktop

Docker is the program that runs this backup tool. You install it once.

- **Windows / macOS:** go to https://www.docker.com/products/docker-desktop/,
  download the installer, run it, then open **Docker Desktop**. On Windows, if it
  asks to enable **WSL 2**, click yes.
- **Linux:** follow https://docs.docker.com/engine/install/ for your distribution.

### Step 2 — Start Docker and leave it running

Open **Docker Desktop** and wait until the small whale icon shows **Running**
(Windows: bottom-right system tray; macOS: top menu bar). Docker must stay running
for everything below to work.

### Step 3 — Open a terminal

A "terminal" is where you type commands.

- **Windows:** click Start, type **PowerShell**, open it.
- **macOS:** open **Terminal** (Applications ▸ Utilities).
- **Linux:** open your terminal app.

Check that Docker is ready — type this and press Enter:

```bash
docker --version
```

If you see a version number (like `Docker version 27...`), continue. If it says
"command not found," Docker isn't running yet — go back to Step 2.

> A command just means: type the line exactly as shown, then press Enter. Lines
> after `#` are notes — you don't type those.

### Step 4 — Get the project files

Pick **one** option.

**Option A — Download (easiest, no Git):**
1. Open https://github.com/marinfrankovic/docker-backup
2. Click the green **Code** button ▸ **Download ZIP**.
3. Unzip it (for example to your Desktop), then in the terminal move into that
   folder:
   ```bash
   cd Desktop/docker-backup-main      # change to wherever you unzipped it
   ```

**Option B — Clone with Git** (if you already have Git):
```bash
git clone https://github.com/marinfrankovic/docker-backup.git
cd docker-backup
```

You're now "inside" the project folder in your terminal.

### Step 5 — Create your settings file

The project includes a template called `.env.example`. Copy it to `.env` (your
personal settings):

```bash
cp .env.example .env            # macOS / Linux
```
```powershell
Copy-Item .env.example .env     # Windows PowerShell
```

### Step 6 — Edit two settings

Open the new `.env` file in any text editor (Notepad is fine) and change just two
lines:

- **`BACKUP_ROOT_HOST`** — the folder where backups will be saved.
  - Windows example (use forward slashes): `E:/Docker/backups`
  - macOS/Linux example: `/srv/docker-backups`
- **`TZ`** — your timezone, e.g. `Europe/London` or `America/New_York`.

Save the file. Leave the other lines as they are.

### Step 7 — Create the backup folder

Make the folder you just named in `BACKUP_ROOT_HOST`:

```powershell
New-Item -ItemType Directory -Force E:\Docker\backups | Out-Null   # Windows
```
```bash
mkdir -p /srv/docker-backups                                       # macOS / Linux
```

> **Windows only:** in Docker Desktop go to **Settings ▸ Resources ▸ File
> sharing**, make sure the drive your backup folder is on (e.g. `E:`) is listed,
> then click **Apply & restart**. This lets Docker write to that folder.

### Step 8 — Start the tool

From inside the project folder, run:

```bash
docker compose up -d --build
```

The first run downloads and builds things, so it may take a few minutes. When it
finishes you'll get your prompt back. The tool now runs in the background and
restarts automatically with Docker.

### Step 9 — Open the web page

In your browser, go to:

**http://127.0.0.1:8088**

### Step 10 — Run your first backup

1. Click the **Backup** tab — it lists your containers.
2. Click **Back up all containers**.
3. Open the **Restore** tab — your new backup appears under today's date. That
   confirms everything works.

### Step 11 — (Optional) Schedule automatic backups

On the **Schedules** tab, add a daily/weekly/monthly job, pick a **scope**
(All / Selected / All Running), choose how many runs to keep, and click that
schedule's **Save** button.

> **To update later:** get the latest files (re-download the ZIP or `git pull`),
> then run `docker compose up -d --build` again. Your backups and settings are
> kept.
>
> **If something goes wrong:** make sure Docker Desktop says **Running**, that
> you're inside the project folder, and that the folder in `BACKUP_ROOT_HOST`
> exists.

---

## Using the web page

- **Backup** — see all containers and back up one, a whole group, a few you pick,
  or everything. Manual backups go to the `_manual` folder.
- **Schedules** — add daily/weekly/monthly jobs. Each has a **scope**: **All**
  (everything), **Selected** (pick what to include), or **All Running** (only
  what's running). Each also keeps the last N runs and has its own **Save** button.
- **Restore** — every backup found on disk, grouped by date. Open one to restore a
  single project or the whole run. New backups show up automatically.
- **Settings** — the backup folder, an optional subfolder for new runs, and how
  many manual backups to keep.
- **Logs** — live activity log.

## What gets backed up

For each container: a details file (`inspect.json`), all its data volumes
(`volume-*.tar.gz`), its databases (saved with `mysqldump` / `pg_dumpall`,
compressed), and its compose file. A `_BACKUP_OK.txt` file marks a finished
backup.

Databases are saved two ways — as a database dump **and** inside the volume copy
— so you have two ways to recover. Stopped containers skip the live database dump,
but their volumes are still saved, so their data is fully captured.

Where files land on disk:
```
<backup-folder>/<bucket>/<YYYY-MM-DD_HHMMSS>/<project>/<container>/
```
`bucket` is the schedule name, or `_manual` for backups you start by hand.

## Restoring (how it works)

Restoring **overwrites current data**. For the project (or whole run) you pick,
the tool: stops its containers → replaces each data volume with the backup →
starts them again (databases first) → re-imports the database dump only where the
volume copy didn't already cover it. Only one backup or restore runs at a time.

You can also do it from the terminal (the web page does the same):
```bash
docker exec docker-backup backup.sh _manual/now              # back up all
docker exec docker-backup backup.sh _manual/now --running    # only running
docker exec docker-backup restore.sh --list                  # list backups
docker exec docker-backup restore.sh _manual/2026-05-29_141500 <project>
```

## If your computer breaks (disaster recovery)

The tool itself stores nothing — your backups, schedules, settings, and logs all
live in your backup folder. The only real risk is losing the **disk** that holds
it, so keep a copy somewhere else (another drive, or a cloud/backup service).

To recover after a reinstall: put the backup folder back at the same
`BACKUP_ROOT_HOST` path, run `docker compose up -d --build`, and every old backup
is listed again. If the original apps no longer exist, recreate them first
(`docker compose up -d` in their own folders), then restore from the web page.

## Settings reference (`.env`)

| Setting | Default | What it does |
|---------|---------|--------------|
| `BACKUP_ROOT_HOST` | _(required)_ | Folder where all backups are saved (forward slashes on Windows) |
| `TZ` | `UTC` | Your timezone, used for scheduling |
| `BACKUP_HOUR` | `3` | Time of the first auto-created schedule |
| `RETENTION_DAYS` | `7` | Default "keep last N runs" for new schedules |
| `GUI_PORT` | `8088` | Web page port (your computer only) |
| `HELPER_IMAGE` | `alpine:3.20` | Helper image used to pack up volumes |

Your `.env` file is never uploaded to GitHub, so your settings stay private.

## License

[MIT](LICENSE).
