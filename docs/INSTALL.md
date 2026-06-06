# Install (build from source)

This guide builds the image from source — useful if you want to modify it. If you
just want to run the tool, use the [Quick start](../README.md#quick-start) instead.

Follow the steps in order. They assume you've never used Docker before.

## 1. Install Docker

- **Windows / macOS:** install [Docker Desktop](https://www.docker.com/products/docker-desktop/),
  then open it. On Windows, allow it to enable **WSL 2** if asked.
- **Linux:** follow the [Docker Engine install guide](https://docs.docker.com/engine/install/).

Start Docker and wait until it shows **Running** (Windows tray / macOS menu bar).
Docker must stay running for everything below.

## 2. Open a terminal

- **Windows:** Start ▸ **PowerShell**.
- **macOS:** **Terminal** (Applications ▸ Utilities).
- **Linux:** your terminal app.

Check Docker is ready:

```bash
docker --version
```

A version number means you're good. "Command not found" means Docker isn't
running yet.

## 3. Get the project files

**Option A — Download (no Git):** open
<https://github.com/marinfrankovic/docker-backup>, click **Code ▸ Download ZIP**,
unzip it, then move into the folder:

```bash
cd Desktop/docker-backup-main      # wherever you unzipped it
```

**Option B — Clone with Git:**

```bash
git clone https://github.com/marinfrankovic/docker-backup.git
cd docker-backup
```

## 4. Create your settings file

Copy the template to your own `.env`:

```bash
cp .env.example .env            # macOS / Linux
```
```powershell
Copy-Item .env.example .env     # Windows PowerShell
```

## 5. Edit two settings

Open `.env` in any text editor and change:

- **`BACKUP_ROOT_HOST`** — where backups are saved.
  - Windows (forward slashes): `E:/Docker/backups`
  - macOS/Linux: `/srv/docker-backups`
- **`TZ`** — your timezone, e.g. `Europe/London`.

See the [settings reference](../README.md#settings-reference-env) for the rest.

## 6. Create the backup folder

```powershell
New-Item -ItemType Directory -Force E:\Docker\backups | Out-Null   # Windows
```
```bash
mkdir -p /srv/docker-backups                                       # macOS / Linux
```

> **Windows only:** in Docker Desktop go to **Settings ▸ Resources ▸ File
> sharing**, make sure your backup drive (e.g. `E:`) is listed, then **Apply &
> restart**. This lets Docker write to that folder.

## 7. Start the tool

```bash
docker compose up -d --build
```

The first run builds the image, so it may take a few minutes. The tool then runs
in the background and restarts automatically with Docker.

## 8. Open the web page and run a first backup

Go to **<http://127.0.0.1:8088>**, open the **Backup** tab, click **Back up all
containers**, then check the **Restore** tab — your backup appears under today's
date.

See [Using the web page](USAGE.md) for what each tab does.

## Updating later

Get the latest files (re-download the ZIP or `git pull`), then run
`docker compose up -d --build` again. Your backups and settings are kept.

## If something goes wrong

- Make sure Docker says **Running**.
- Make sure you're inside the project folder.
- Make sure the folder in `BACKUP_ROOT_HOST` exists.
