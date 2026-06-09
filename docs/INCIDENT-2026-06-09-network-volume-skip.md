# Incident & Fix Report — docker-backup runaway NFS-volume archiving

**Date:** 2026-06-09
**Author:** automated (GitHub Copilot session)
**Component:** `docker-backup` (Windows-authored tool running on Linux host `192.168.100.122`)
**Severity:** High — silent backup failure + disk-fill risk; 3 days of missed nightly backups
**Fix version:** `v1.3.1` (commit `9091178`, tag `v1.3.1`)

---

## 1. Symptom

The Docker Backup GUI (`http://192.168.100.122:8089/`) showed a backup permanently
"in progress" (14/30, stuck on `bazarr · volume remote_movies`). No completed
nightly backups existed for **2026-06-08** or **2026-06-09**.

Activity log showed both nightly runs were skipped:

```
[2026-06-08 03:00:19] Schedule 'Nightly backup' due but tool busy; skipped this minute
[2026-06-09 03:00:07] Schedule 'Nightly backup' due but tool busy; skipped this minute
```

## 2. Root cause

The nightly schedule is configured with `skip_network = true` (skip NFS/SMB/CIFS
mounts) and **one** per-container mount override (`immich_server`, to limit which
mounts it captures).

In `app.py`, `_write_selection_file()` writes a per-container TSV consumed by
`backup.sh`. The presence of *any* per-container override causes a selection file
to be written for **every** selected container. For containers **without** an
explicit override, the code wrote `skip_network = 0`:

```python
# BEFORE (buggy)
sn = "1" if cm.get("skip_network") else "0"   # cm == {} -> "0"
```

`backup.sh` treats the per-container value as authoritative, so the run-level
`SKIP_NETWORK_MOUNTS=1` was **overridden to 0** for `bazarr`, `radarr`, `sonarr`,
etc. Their `remote_movies` / `remote_torrents` volumes are NFS mounts of the
entire Synology media library (`:/volume1/Media`, 42 TB, **27 TB used**).

`bazarr` therefore began tar-gzipping the whole media library into
`volume-remote_movies.tar.gz`. It reached **268 GB** (and was still growing) before
it was caught — it would eventually have filled `/mnt/docker-storage` (1.4 TB free).
Because the run never finished, the tool stayed `busy=true`, so every subsequent
nightly was skipped.

## 3. Immediate remediation

1. **Stopped** the runaway job via `POST /api/stop` → `busy=false`.
2. **Deleted** the 268 GB partial run `nightly/2026-06-07_030012`.
   Disk reclaimed: **393 GB → 123 GB used** (270 GB freed).

## 4. The fix (v1.3.1)

`_write_selection_file()` now receives the run-level `skip_network` default and
uses it for any container lacking an explicit per-container override, so the
global toggle is never silently lost:

```python
# AFTER (fixed)
def _write_selection_file(names, container_mounts, skip_network=False):
    ...
    sn = "1" if cm.get("skip_network", skip_network) else "0"   # inherits run-level
```

`do_backup()` passes the run-level value through:

```python
sel_path = _write_selection_file(names, container_mounts, skip_network)
```

`backup.sh` was already correct (it skips NFS/SMB/CIFS volumes when the per-container
flag is `1`); no shell changes were needed.

### Precedence (unchanged, now correctly applied)
explicit per-container override → run-level `skip_network` default → back up.

## 5. Tests

Added `tests/test_exclusions.py` (stdlib only, no Docker needed). 15 cases, all pass:

- Unconfigured container inherits global `skip_network=1` **and** `=0`.
- Explicit per-container `skip_network=False` correctly overrides a global `True`.
- Spec generation: `*` / comma-keys / `__none__` sentinel.
- `None` returned when no/empty `container_mounts` (so `backup.sh` uses the global).
- Network-fstype classification (`nfs`/`cifs` in, `ext4`/`local` out).

```
ALL TESTS PASSED  (15/15)
```

## 6. Live validation on the host

Deployed the fixed image to the host (`docker compose build && up -d`, verified the
patched line is present in the running container), then ran a manual backup of the
exact problem containers with `skip_network=true` and a forced selection file
(`immich_server` override) — the precise condition that triggered the bug:

```
10:33:59  container: bazarr   → skip volume remote_movies   (network filesystem)
10:34:15  container: radarr   → skip volume remote_movies   (network filesystem)
10:34:15                        skip volume remote_torrents (network filesystem)
```

Result (`rc=0`): only **local config binds** were archived; **no** `remote_*`
archives created anywhere under `/backups`.

| Container | Archived | NFS volume(s) skipped |
|-----------|----------|------------------------|
| bazarr    | `bind-config.tar.gz` (8.2 MB) | remote_movies |
| radarr    | `bind-config.tar.gz` (1.5 GB) | remote_movies, remote_torrents |
| sonarr    | `bind-config.tar.gz` (125 MB) | remote_movies, remote_torrents |
| immich_server | `inspect.json` only (override) | — |

`find /backups -name "volume-remote_*.tar.gz"` → **empty**.

The full nightly schedule was then re-triggered; it now correctly skips all NFS
volumes. (It runs slowly because the host CPU is currently saturated by the Immich
import grind — this is unrelated to the bug.)

## 7. Release

- Commit `9091178` on `main`, pushed to `github.com/marinfrankovic/docker-backup`.
- Tag **`v1.3.1`** pushed (triggers the DockerHub publish workflow:
  `mfrankovic/docker-backup:1.3.1` / `1.3` / `latest`).
- Host `/opt/docker-backup` rebuilt and running the fixed image; container healthy.

## 8. Follow-ups / recommendations

- The nightly retention is 3; verify the next scheduled 03:00 run completes cleanly
  and that old partials are pruned.
- Consider a guard rail: refuse (or warn) when a single volume/bind archive exceeds
  a configurable size threshold, as defense-in-depth against future misconfiguration.
- The host is heavily loaded by the ongoing Immich import; backups will be slow until
  that backlog drains.
