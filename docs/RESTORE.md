# Restoring & disaster recovery

Restoring **overwrites current data**.

## Restore levels

You can restore at three levels:

- **Whole run** — every project captured in that backup.
- **One project** — only the containers of a single compose project.
- **One container** — a single container inside a project (expand the run on the
  **Restore** tab and use **Restore container** next to its name).

For whatever you pick, the tool: stops the affected container(s) → replaces each
data volume **and** bind-mounted folder with the backup → starts them again
(databases first) → re-imports the database dump only where the volume/bind copy
didn't already cover it. When restoring a bind mount, only the backed-up
folder/file is replaced, so unrelated siblings on the host are left untouched.
Only one backup or restore runs at a time.

## From the terminal

The web page does the same as these commands:

```bash
docker exec docker-backup backup.sh _manual/now              # back up all
docker exec docker-backup backup.sh _manual/now --running    # only running
docker exec docker-backup restore.sh --list                  # list backups
docker exec docker-backup restore.sh _manual/2026-05-29_141500 <project>
docker exec docker-backup restore.sh _manual/2026-05-29_141500 <project> <container>
```

> **Power users / scripting.** From the command line, `backup.sh` captures every
> mount by default. The GUI drives per-container mount choices through two
> variables it sets automatically: `SKIP_NETWORK_MOUNTS=1` to skip NFS/SMB/CIFS
> mounts, and `SELECTION_FILE` — a path to a TSV file with one
> `container<TAB>skip_network(0|1)<TAB>spec` line per container, where `spec` is
> `*` (all mounts), `__none__` (none), or a comma-separated list of mount keys
> (`vol:<name>` / `bind:<container-path>`). Mounts not listed fall back to backing
> everything up.

## If your computer breaks

The tool itself stores nothing — your backups, schedules, settings, and logs all
live in your backup folder. The only real risk is losing the **disk** that holds
it, so keep a copy somewhere else (another drive, or a cloud/backup service).

There are two restore situations, both handled automatically:

1. **The container/project still exists** (you just want to roll back). Restore
   stops it, overwrites its volumes and bind-mounted config with the snapshot,
   and starts it again.
2. **From zero — a brand-new Docker engine** (disk died, fresh machine). The
   containers don't exist yet, so restore **recreates them** from the compose
   file saved in the backup (`docker compose up -d`), with the volumes and
   bind-mounted config restored first, so each app comes back with its real data.

For case 2 the backup already includes, per project, the compose file(s), the
project's `.env`, and a small `compose.meta` (the original working directory and
compose file names) so the stack can be rebuilt exactly where it was. **Any mounts
you chose not to back up (for example a large media library) are your
responsibility to restore separately** — such content is typically already stored
elsewhere.

### Recover after a total failure

1. Install Docker, put the backup folder back at the same `BACKUP_ROOT_HOST`
   path, and run `docker compose up -d --build` to start docker-backup itself.
2. Open the **Restore** tab, pick the latest run, and restore. Missing containers
   are recreated from their saved compose files; existing ones are overwritten in
   place.
3. Restore any mounts you chose to skip (for example media libraries) separately,
   by hand.
