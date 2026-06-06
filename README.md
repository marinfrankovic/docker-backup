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

## Docs

- **[Install from source](docs/INSTALL.md)** — beginner-friendly, step by step.
- **[Using the web page](docs/USAGE.md)** — the tabs, and what gets backed up.
- **[Restoring & disaster recovery](docs/RESTORE.md)** — roll back or rebuild from zero.

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

To build from source instead (handy if you want to modify it), see
**[docs/INSTALL.md](docs/INSTALL.md)**.

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
