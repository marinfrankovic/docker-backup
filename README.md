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

## Quick start (Docker Hub)

The image is published on Docker Hub, so you don't have to build anything. Pick a
folder for your backups, then run:

```bash
docker run -d --name docker-backup --restart unless-stopped \
  -p 127.0.0.1:8088:8088 \
  -e BACKUP_ROOT_CONTAINER=/backups \
  -e BACKUP_ROOT_HOST=/srv/docker-backups \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /srv/docker-backups:/backups \
  mfrankovic/docker-backup:latest
```

Then open <http://127.0.0.1:8088>.

> **Important:** `BACKUP_ROOT_HOST` must be the **same host path** as the left
> side of the `/backups` volume mount. The tool launches sibling helper
> containers that mount that real host path, so the two must match.

Prefer Compose? Use this `compose.yaml` (pulls the image instead of building):

```yaml
services:
  docker-backup:
    image: mfrankovic/docker-backup:latest
    container_name: docker-backup
    restart: unless-stopped
    environment:
      TZ: ${TZ:-UTC}
      BACKUP_ROOT_CONTAINER: /backups
      BACKUP_ROOT_HOST: ${BACKUP_ROOT_HOST}
      GUI_PORT: ${GUI_PORT:-8088}
    ports:
      - "127.0.0.1:${GUI_PORT:-8088}:${GUI_PORT:-8088}"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - "${BACKUP_ROOT_HOST}:/backups"
```

Available tags: `latest`, plus versioned tags like `1.0.0` and `1.0`. The image
is multi-arch (`linux/amd64` and `linux/arm64`), so it runs on regular servers and
on ARM boards (Raspberry Pi, ARM NAS) alike.

The rest of this guide builds the image from source, which is useful if you want
to modify it.

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
- **Mounts** — see every container's volumes and bind mounts and choose, per
  mount, whether to **Keep** (always back up), **Skip** (never back up), or leave
  on **Default** (follow the global rules). Handy for skipping one big cache or
  media folder without touching anything else.
- **Settings** — the backup folder, an optional subfolder for new runs, how many
  manual backups to keep, a **Skip network mounts (NFS/SMB/CIFS)** toggle, and
  extra include/exclude pattern lists that apply to every run.
- **Logs** — live activity log.

While a backup or restore is running, the header shows a **live progress bar**
with a `done / total` container count and the item currently being processed
(for example `14/24 — sonarr · volume config`). The activity log streams
each step in real time, so you can watch progress on the **Logs** tab too.

A red **⏹ Stop** button appears in the header while a job is running. Click it to
cancel the current backup or restore: the tool stops the running script **and** any
helper containers it started, then returns to idle. The interrupted run is left
incomplete (no `_BACKUP_OK.txt`) and can be deleted from **Restore**.

## What gets backed up

For each container: a details file (`inspect.json`), all its data volumes
(`volume-*.tar.gz`), its **bind-mounted folders/files** (`bind-*.tar.gz`, listed
in `binds.tsv`), its databases (saved with `mysqldump` / `pg_dumpall`,
compressed), and its compose file. A `_BACKUP_OK.txt` file marks a finished
backup.

This means **every container is fully restorable with all its config** — whether
it stores data in a named Docker volume *or* a bind-mounted host folder (for
example AdGuard Home's `conf` folder, or any `./config:/...` mount in your compose
file). This applies to **every Docker project and container**, not any specific app.

**Large media libraries are skipped by default.** Volumes whose names look like
media content (movies, TV/shows/series, music, anime, downloads/torrents, or any
`remote_*` volume) are **not** archived, so backups stay small and fast and your
USB drive doesn't fill up with media you already have elsewhere. The default
skip patterns are:

```
*movies* *movie* *tv* *shows* *series* *media* *music* *anime* *downloads* *torrents* remote_*
```

To change them, set the `EXCLUDE_VOLUME_PATTERNS` environment variable on the
`docker-backup` service (space-separated shell globs). Set it to an empty string
to back up every volume.

**Bind mounts are filtered the same way.** A bind is skipped when its host path
*or* its in-container path matches a pattern in `EXCLUDE_BIND_PATTERNS`. The
defaults skip the Docker socket, host system files (`/proc`, `/sys`, `/dev`,
`/etc/localtime`, `/etc/hosts`, …) and the same media globs as above:

```
*/docker.sock /run/docker.sock /var/run/docker.sock /proc /proc/* /sys /sys/* /dev /dev/* \
/etc/localtime /etc/timezone /etc/hostname /etc/hosts /etc/resolv.conf \
*movies* *movie* *tv* *shows* *series* *media* *music* *anime* *downloads* *torrents*
```

To exclude an additional large, regenerable folder (for example AdGuard's 6 GB
query-log `work` directory), append your own glob, e.g.:

```
EXCLUDE_BIND_PATTERNS="…defaults… */adguardhome/work"
```

Set it to an empty string to back up every bind mount.

### Choosing what to skip from the web page

You don't have to edit environment variables — everything above is also
configurable from the GUI, and your choices apply to **every** backup (manual and
scheduled):

- **Mounts tab** — lists each container's volumes and bind mounts. Set any one to
  **Keep**, **Skip**, or **Default**. This is the easiest way to drop a single
  cache/media folder while keeping its database volume.
- **Settings tab → Exclusions** — a **Skip network mounts (NFS/SMB/CIFS)** toggle
  plus four pattern boxes (one glob per line): extra **exclude** patterns for
  binds and volumes, and force-**keep** patterns for binds and volumes.

These GUI choices are **added on top of** the built-in defaults — they never wipe
the media-library defaults — so you only ever specify what's different for your
setup. Under the hood they are passed to the backup script through the
`EXTRA_EXCLUDE_*` / `EXTRA_INCLUDE_*` and `SKIP_NETWORK_MOUNTS` variables.

### How the rules combine (precedence)

For every volume and bind mount, the decision is made in this order:

1. **Force-keep wins.** If it matches an include pattern (`INCLUDE_*_PATTERNS` /
   `EXTRA_INCLUDE_*` / a **Keep** choice in the Mounts tab), it is **always**
   backed up — even if it also matches an exclude or sits on a network share.
2. **Exclude.** Otherwise, if it matches an exclude pattern (`EXCLUDE_*_PATTERNS`
   / `EXTRA_EXCLUDE_*` / a **Skip** choice), it is skipped.
3. **Network filesystem.** Otherwise, if `SKIP_NETWORK_MOUNTS=1` and the mount
   lives on NFS/SMB/CIFS, it is skipped (so you don't pull gigabytes off another
   server). Volume type is read from `docker volume inspect`; bind type is probed
   with `stat -f`.
4. **Default: keep.** Anything not skipped above is backed up.

### Environment variables (for Compose / power users)

| Variable | Effect |
| --- | --- |
| `EXCLUDE_VOLUME_PATTERNS` | **Replaces** the default volume-exclude list. |
| `EXCLUDE_BIND_PATTERNS` | **Replaces** the default bind-exclude list. |
| `INCLUDE_VOLUME_PATTERNS` | Force-keep volume globs (empty by default). |
| `INCLUDE_BIND_PATTERNS` | Force-keep bind globs (empty by default). |
| `EXTRA_EXCLUDE_VOLUME_PATTERNS` | **Appends** to the volume-exclude list (GUI uses this). |
| `EXTRA_EXCLUDE_BIND_PATTERNS` | **Appends** to the bind-exclude list (GUI uses this). |
| `EXTRA_INCLUDE_VOLUME_PATTERNS` | **Appends** force-keep volume globs (GUI uses this). |
| `EXTRA_INCLUDE_BIND_PATTERNS` | **Appends** force-keep bind globs (GUI uses this). |
| `SKIP_NETWORK_MOUNTS` | `1` to skip NFS/SMB/CIFS mounts (off by default). |

> **Backward compatible:** with no GUI choices, blank Settings, and
> `SKIP_NETWORK_MOUNTS` unset, behaviour is identical to before — only the
> built-in media defaults are skipped. Any existing `EXCLUDE_*` overrides keep
> working exactly as they did.

Databases are saved two ways — as a database dump **and** inside the volume copy
— so you have two ways to recover. Stopped containers skip the live database dump,
but their volumes are still saved, so their data is fully captured.

Where files land on disk:
```
<backup-folder>/<bucket>/<YYYY-MM-DD_HHMMSS>/<project>/<container>/
```
`bucket` is the schedule name, or `_manual` for backups you start by hand.

## Restoring (how it works)

Restoring **overwrites current data**. You can restore at three levels:

- **Whole run** — every project captured in that backup.
- **One project** — only the containers of a single compose project.
- **One container** — just a single container inside a project (expand the run on
  the **Restore** tab and use the **Restore container** button next to its name).

For whatever you pick, the tool: stops the affected container(s) → replaces each
data volume **and bind-mounted folder** with the backup → starts them again
(databases first) → re-imports the database dump only where the volume/bind copy
didn't already cover it. When restoring a bind mount, only the backed-up
folder/file is replaced, so unrelated siblings on the host are left untouched.
Only one backup or restore runs at a time.

You can also do it from the terminal (the web page does the same):
```bash
docker exec docker-backup backup.sh _manual/now              # back up all
docker exec docker-backup backup.sh _manual/now --running    # only running
docker exec docker-backup restore.sh --list                  # list backups
docker exec docker-backup restore.sh _manual/2026-05-29_141500 <project>
docker exec docker-backup restore.sh _manual/2026-05-29_141500 <project> <container>
```

## If your computer breaks (disaster recovery)

The tool itself stores nothing — your backups, schedules, settings, and logs all
live in your backup folder. The only real risk is losing the **disk** that holds
it, so keep a copy somewhere else (another drive, or a cloud/backup service).

There are two restore situations, and both are handled automatically:

1. **The container/project still exists** (you just want to roll back). Restore
   stops it, overwrites its volumes and bind-mounted config with the snapshot,
   and starts it again.
2. **From zero — a brand-new Docker engine** (disk died, fresh machine). The
   containers don't exist yet, so restore **recreates them** from the compose
   file saved in the backup (`docker compose up -d`), with the volumes and
   bind-mounted config restored first, so each app comes back with its real data.

For case 2 the backup already includes, per project, the compose file(s), the
project's `.env`, and a small `compose.meta` (the original working directory and
compose file names) so the stack can be rebuilt exactly where it was. **Large
media libraries are excluded from backups on purpose — you are responsible for
restoring that media separately** (it's typically already stored elsewhere).

To recover after a total failure:

1. Install Docker, put the backup folder back at the same `BACKUP_ROOT_HOST`
   path, and run `docker compose up -d --build` to start docker-backup itself.
2. Open the **Restore** tab, pick the latest run, and restore. Missing
   containers are recreated from their saved compose files; existing ones are
   overwritten in place.
3. Restore any excluded media libraries separately, by hand.

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
