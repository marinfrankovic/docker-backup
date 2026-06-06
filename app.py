#!/usr/bin/env python3
"""Local web GUI + scheduler for the Docker backup tool.

Runs inside the docker-backup container. Serves a single-page UI and a small
JSON API. No external dependencies (Python stdlib only).

Core model
----------
* The **backups root** is the only required input. It is the host folder mounted
  at ``BACKUP_ROOT`` (default ``/backups``). Everything the tool produces and
  everything it can restore lives under this folder, so a rebuilt container only
  needs to be pointed at the same root to immediately see every prior backup.

* A **run** is one backup execution. It is written to
  ``<root>/<destination?>/<bucket>/<YYYY-MM-DD_HHMMSS>/`` and marked with a
  ``_BACKUP_OK.txt`` file. ``bucket`` is the schedule id, or ``_manual`` for
  ad-hoc backups. The Restore page is populated by scanning the root for any
  directory that contains ``_BACKUP_OK.txt`` (legacy ``YYYY-MM-DD`` day folders
  are detected the same way), so the layout is fully self-describing.

* **Schedules** are a list. Each has its own frequency (daily / weekly /
  monthly), time, container selection, and retention (keep last N runs).

* A global **destination** setting may place new runs in a sub-path *under* the
  mounted root (the container can only write paths that are mounted in).
"""
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("GUI_PORT", "8088"))
BACKUP_ROOT = os.path.realpath(os.environ.get("BACKUP_ROOT_CONTAINER", "/backups"))
SELF_NAME = os.environ.get("SELF_NAME", "docker-backup")
DEFAULT_RETENTION = int(os.environ.get("RETENTION_DAYS", "7"))
DEFAULT_HOUR = int(os.environ.get("BACKUP_HOUR", "3"))

CONFIG_DIR = os.path.join(BACKUP_ROOT, "_config")
SCHEDULES_PATH = os.path.join(CONFIG_DIR, "schedules.json")
SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.json")
LEGACY_PATH = os.path.join(CONFIG_DIR, "schedule.json")   # pre-redesign single schedule
LOG_DIR = os.path.join(BACKUP_ROOT, "_logs")
LOG_PATH = os.path.join(LOG_DIR, "activity.log")

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# A run sub-path: one or more safe path segments separated by "/".
SEG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

FREQUENCIES = ("daily", "weekly", "monthly")
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]  # Python weekday()

_run_lock = threading.Lock()      # only one backup/restore at a time
_log_lock = threading.Lock()
_cfg_lock = threading.Lock()      # serialise config reads/writes
_proc_lock = threading.Lock()     # guards the handle to the running child process
_current_proc = {"p": None}       # Popen of the in-flight backup/restore, or None
STATE = {"busy": False, "current": "", "last_result": "",
         "step": "", "done": 0, "total": 0, "container": "", "stopping": False}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    line = f"[{now()}] {msg}"
    print(line, flush=True)
    with _log_lock:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def run(cmd, timeout=None):
    """Run a command list, return (rc, combined_output)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return 1, str(exc)


def run_stream(cmd, on_line, timeout=None, env=None):
    """Run a command, streaming each output line to on_line as it is produced.

    Returns (rc, full_output). Unlike run(), output is delivered live so the
    activity log (and the GUI that polls it) updates while the job is running
    instead of only at the end. ``env`` (when given) replaces the child's
    environment so callers can pass per-run exclusion variables.
    """
    lines = []
    try:
        # start_new_session puts the child in its own process group so a Stop
        # request can signal the whole tree (backup.sh + any docker clients).
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, bufsize=1,
                             start_new_session=True, env=env)
    except Exception as exc:  # noqa: BLE001
        return 1, str(exc)
    with _proc_lock:
        _current_proc["p"] = p
    deadline = (time.time() + timeout) if timeout else None
    try:
        for line in p.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            try:
                on_line(line)
            except Exception:  # noqa: BLE001
                pass
            if deadline and time.time() > deadline:
                p.kill()
                lines.append("timed out")
                break
        p.wait(timeout=5)
    except Exception as exc:  # noqa: BLE001
        lines.append(str(exc))
    finally:
        with _proc_lock:
            _current_proc["p"] = None
    rc = p.returncode if p.returncode is not None else 1
    return rc, "\n".join(lines)


def stop_current():
    """Terminate the in-flight backup/restore and clean up helper containers.

    Helper containers (the detached ``docker run`` doing the actual volume tar)
    are removed first, then the child process group is force-killed. backup.sh
    is plain ``sh`` with no signal trap, so while it waits on a foreground
    ``docker run`` it would otherwise defer SIGTERM until that big tar finishes
    — leaving the job "stopping" for minutes. Removing the helper unblocks the
    docker client and SIGKILL guarantees the script tree dies promptly.
    """
    with _proc_lock:
        p = _current_proc.get("p")
    if p is None or p.poll() is not None:
        return False, "nothing is running"
    STATE["stopping"] = True
    STATE["step"] = "stopping\u2026"
    log("Stop requested \u2014 terminating current job")
    # Remove helper containers (volume tar / compose copy) spawned by backup.sh
    # first, so the foreground `docker run` it is blocked on returns at once.
    # Retry briefly in case a helper is created in the same instant.
    for _ in range(3):
        rc, out = run(["docker", "ps", "--filter", "label=docker-backup-helper=1", "-q"],
                      timeout=30)
        cids = out.split() if rc == 0 else []
        if not cids:
            break
        for cid in cids:
            run(["docker", "rm", "-f", cid], timeout=30)
        time.sleep(0.3)
    # Force-kill the whole child process group (backup.sh + docker clients).
    # SIGKILL rather than SIGTERM: the script has no trap and would defer a
    # graceful signal until its current foreground command returns.
    try:
        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001
        try:
            p.kill()
        except Exception:  # noqa: BLE001
            pass
    return True, "stop signal sent"


def slugify(text, fallback="schedule"):
    s = re.sub(r"[^a-z0-9]+", "-", str(text).strip().lower()).strip("-")
    return s or fallback


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# settings  (global destination + manual retention)
# --------------------------------------------------------------------------- #
def _safe_subpath(rel):
    """Return a cleaned relative sub-path under the root, or '' if invalid/empty."""
    rel = (rel or "").strip().strip("/").replace("\\", "/")
    if not rel:
        return ""
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." or not SEG_RE.match(p) for p in parts):
        return ""
    return "/".join(parts)


def load_settings():
    cfg = {"destination": "", "manual_retention": 10}
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        cfg["destination"] = _safe_subpath(data.get("destination", ""))
        mr = data.get("manual_retention")
        if isinstance(mr, int) and 1 <= mr <= 999:
            cfg["manual_retention"] = mr
    except FileNotFoundError:
        write_json(SETTINGS_PATH, cfg)
    except Exception as exc:  # noqa: BLE001
        log(f"WARN could not read settings: {exc}")
    return cfg


def save_settings(cfg):
    write_json(SETTINGS_PATH, cfg)


# --------------------------------------------------------------------------- #
# schedules
# --------------------------------------------------------------------------- #
def _default_schedule():
    return {
        "id": "nightly",
        "name": "Nightly backup",
        "enabled": True,
        "frequency": "daily",
        "time": f"{DEFAULT_HOUR:02d}:00",
        "weekdays": [0, 1, 2, 3, 4, 5, 6],   # used when frequency == weekly
        "day_of_month": 1,                   # used when frequency == monthly
        "mode": "all",                       # all | selected | running
        "containers": [],                    # used only when mode == selected
        "skip_network": False,               # exclude NFS/SMB/CIFS mounts this run
        "container_mounts": {},              # per-container mount selection (mode==selected)
        "retention": DEFAULT_RETENTION,      # keep last N runs of this schedule
    }


def _clean_container_mounts(value):
    """Validate a {container: {all, mounts[], skip_network}} selection map."""
    out = {}
    if not isinstance(value, dict):
        return out
    for name, entry in value.items():
        if not isinstance(name, str) or not NAME_RE.match(name):
            continue
        if not isinstance(entry, dict):
            continue
        keys = []
        raw_keys = entry.get("mounts")
        if isinstance(raw_keys, list):
            for k in raw_keys:
                k = str(k)
                if (k.startswith("vol:") or k.startswith("bind:")) and k not in keys:
                    keys.append(k)
                if len(keys) >= 200:
                    break
        out[name] = {
            "all": bool(entry.get("all", True)),
            "mounts": keys,
            "skip_network": bool(entry.get("skip_network", False)),
        }
    return out


def _normalise_schedule(s, existing_ids):
    out = _default_schedule()
    out["name"] = (str(s.get("name", "")).strip() or "Backup")[:60]
    # id: keep if valid & unique, else derive from name
    sid = str(s.get("id", "")).strip().lower()
    if not ID_RE.match(sid) or sid in existing_ids or sid in ("_manual", "_config", "_logs"):
        base = slugify(out["name"])
        sid = base
        n = 2
        while sid in existing_ids or sid in ("_manual", "_config", "_logs"):
            sid = f"{base}-{n}"
            n += 1
    out["id"] = sid
    out["enabled"] = bool(s.get("enabled", True))
    freq = str(s.get("frequency", "daily")).lower()
    out["frequency"] = freq if freq in FREQUENCIES else "daily"
    t = str(s.get("time", out["time"]))
    out["time"] = t if TIME_RE.match(t) else out["time"]
    wd = s.get("weekdays")
    if isinstance(wd, list):
        days = sorted({int(d) for d in wd if isinstance(d, int) and 0 <= d <= 6})
        out["weekdays"] = days or out["weekdays"]
    dom = s.get("day_of_month")
    if isinstance(dom, int) and 1 <= dom <= 31:
        out["day_of_month"] = dom
    cs = s.get("containers")
    if isinstance(cs, list):
        out["containers"] = [c for c in (str(x).strip() for x in cs) if NAME_RE.match(c)]
    else:
        out["containers"] = []
    mode = str(s.get("mode", "")).lower()
    if mode not in ("all", "selected", "running"):
        # backward compat: old schedules only stored containers ([] == everything)
        mode = "selected" if out["containers"] else "all"
    out["mode"] = mode
    out["skip_network"] = bool(s.get("skip_network", False))
    out["container_mounts"] = _clean_container_mounts(s.get("container_mounts"))
    ret = s.get("retention")
    if isinstance(ret, int) and 1 <= ret <= 999:
        out["retention"] = ret
    return out


def _migrate_legacy():
    """Convert the old single-schedule schedule.json into the new list format."""
    try:
        with open(LEGACY_PATH, encoding="utf-8") as fh:
            old = json.load(fh)
    except Exception:  # noqa: BLE001
        return None
    times = [t for t in old.get("times", []) if TIME_RE.match(str(t))]
    sched = _default_schedule()
    sched["enabled"] = bool(old.get("enabled", True))
    sched["time"] = times[0] if times else sched["time"]
    if isinstance(old.get("retention_days"), int):
        sched["retention"] = old["retention_days"]
    log("Migrated legacy schedule.json -> schedules.json")
    return {"schedules": [sched]}


def load_schedules():
    try:
        with open(SCHEDULES_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        raw = data.get("schedules", [])
    except FileNotFoundError:
        migrated = _migrate_legacy()
        if migrated:
            save_schedules(migrated["schedules"])
            return migrated["schedules"]
        defaults = [_default_schedule()]
        save_schedules(defaults)
        return defaults
    except Exception as exc:  # noqa: BLE001
        log(f"WARN could not read schedules: {exc}")
        raw = []
    result = []
    ids = set()
    for s in raw if isinstance(raw, list) else []:
        ns = _normalise_schedule(s, ids)
        ids.add(ns["id"])
        result.append(ns)
    return result


def save_schedules(schedules):
    write_json(SCHEDULES_PATH, {"schedules": schedules})


def replace_schedules(raw_list):
    """Validate and persist a full schedules list coming from the UI."""
    result = []
    ids = set()
    for s in raw_list if isinstance(raw_list, list) else []:
        ns = _normalise_schedule(s if isinstance(s, dict) else {}, ids)
        ids.add(ns["id"])
        result.append(ns)
    with _cfg_lock:
        save_schedules(result)
    log(f"Schedules updated ({len(result)} schedule(s))")
    return result


# --------------------------------------------------------------------------- #
# containers
# --------------------------------------------------------------------------- #
def list_containers():
    rc, out = run([
        "docker", "ps", "-a", "--format",
        '{{.Names}}\t{{.State}}\t{{.Image}}\t{{.Label "com.docker.compose.project"}}',
    ])
    items = []
    if rc == 0:
        for ln in out.splitlines():
            parts = ln.split("\t")
            if len(parts) < 3:
                continue
            name, state, image = parts[0], parts[1], parts[2]
            project = parts[3] if len(parts) > 3 and parts[3] else "_standalone"
            if name == SELF_NAME:
                continue
            items.append({"name": name, "state": state, "image": image,
                          "project": project})
    items.sort(key=lambda x: (x["project"], x["name"]))
    return items


# Network filesystem types reported by `docker volume inspect` .Options.type.
_NETWORK_VOL_TYPES = ("nfs", "nfs4", "cifs", "smb", "smb2", "smb3", "smbfs")


def _volume_is_network(name):
    """True if a named volume is backed by an NFS/SMB/CIFS driver option."""
    if not name:
        return False
    rc, out = run(["docker", "volume", "inspect", "-f",
                   '{{ index .Options "type" }}', name])
    if rc != 0:
        return False
    return out.strip().lower() in _NETWORK_VOL_TYPES


def list_mounts():
    """Enumerate every container's volumes and bind mounts.

    Returns one entry per container, each mount carrying a stable selection key
    (``vol:<name>`` or ``bind:<destination>``) used by the per-run / per-schedule
    mount picker. Bind fstype is not probed here (that needs a host helper and
    would be slow); the per-container "skip network mounts" toggle handles network
    binds at backup time instead.
    """
    containers = list_containers()
    result = []
    for c in containers:
        name = c["name"]
        # Tab-separated: type, volume-name, source, destination — one line per mount.
        rc, out = run(["docker", "inspect", "-f",
                       '{{range .Mounts}}{{.Type}}\t{{.Name}}\t{{.Source}}\t{{.Destination}}\n{{end}}',
                       name])
        if rc != 0:
            continue
        mounts = []
        for ln in out.splitlines():
            if not ln.strip():
                continue
            parts = ln.split("\t")
            if len(parts) < 4:
                continue
            mtype, vname, source, dest = parts[0], parts[1], parts[2], parts[3]
            if mtype == "volume" and vname:
                mounts.append({"kind": "volume", "mkey": "vol:" + vname,
                               "label": vname, "dest": dest,
                               "network": _volume_is_network(vname)})
            elif mtype == "bind" and source:
                mounts.append({"kind": "bind", "mkey": "bind:" + dest,
                               "label": source, "dest": dest, "network": False})
        if mounts:
            result.append({"container": name, "project": c["project"],
                           "mounts": mounts})
    return result


def list_self_mounts():
    """Return the docker-backup container's own mounts (for the root picker)."""
    items = []
    rc, out = run(["docker", "inspect", "-f",
                   '{{range .Mounts}}{{.Type}}\t{{.Source}}\t{{.Destination}}\n{{end}}',
                   SELF_NAME])
    if rc != 0:
        return items
    for ln in out.splitlines():
        if not ln.strip():
            continue
        parts = ln.split("\t")
        if len(parts) < 3:
            continue
        mtype, source, dest = parts[0], parts[1], parts[2]
        if dest in ("/var/run/docker.sock", "/run/docker.sock"):
            continue
        items.append({"type": mtype, "source": source, "dest": dest,
                      "is_root": os.path.realpath(dest) == BACKUP_ROOT})
    return items


# --------------------------------------------------------------------------- #
# backups on disk  (run discovery + filesystem browse)
# --------------------------------------------------------------------------- #
def dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _abs(rel):
    """Resolve a relative path under the root, confined to the root."""
    rel = (rel or "").strip().strip("/").replace("\\", "/")
    target = os.path.realpath(os.path.join(BACKUP_ROOT, rel))
    if target != BACKUP_ROOT and os.path.commonpath([target, BACKUP_ROOT]) != BACKUP_ROOT:
        return None
    return target


def _run_projects(run_abs):
    projects = []
    try:
        entries = sorted(os.listdir(run_abs))
    except OSError:
        return projects
    for proj in entries:
        ppath = os.path.join(run_abs, proj)
        if proj.startswith("_") or not os.path.isdir(ppath):
            continue
        containers = sorted(c for c in os.listdir(ppath)
                            if os.path.isdir(os.path.join(ppath, c)))
        try:
            pmtime = os.path.getmtime(ppath)
        except OSError:
            pmtime = 0
        projects.append({"name": proj, "containers": containers,
                         "size": dir_size(ppath), "mtime": pmtime})
    return projects


def list_runs():
    """Scan the whole root for completed runs (dirs holding _BACKUP_OK.txt)."""
    runs = []
    if not os.path.isdir(BACKUP_ROOT):
        return runs
    for root, dirs, files in os.walk(BACKUP_ROOT):
        # never descend into bookkeeping folders
        dirs[:] = [d for d in dirs if d not in ("_config", "_logs")]
        if "_BACKUP_OK.txt" not in files:
            continue
        dirs[:] = []  # a run is a leaf; don't recurse into project folders
        rel = os.path.relpath(root, BACKUP_ROOT).replace("\\", "/")
        bucket = rel.split("/", 1)[0] if "/" in rel else rel
        try:
            mtime = os.path.getmtime(os.path.join(root, "_BACKUP_OK.txt"))
        except OSError:
            mtime = 0
        runs.append({
            "run": rel,
            "bucket": bucket,
            "label": rel.replace("/", " · "),
            "projects": _run_projects(root),
            "size": dir_size(root),
            "mtime": mtime,
            "complete": True,
        })
    runs.sort(key=lambda r: r["mtime"], reverse=True)
    return runs


# --------------------------------------------------------------------------- #
# backup / restore / delete actions
# --------------------------------------------------------------------------- #
def _prune_bucket(bucket_rel, keep):
    """Keep only the newest `keep` run folders inside a bucket sub-path."""
    bucket_abs = _abs(bucket_rel)
    if bucket_abs is None or not os.path.isdir(bucket_abs):
        return
    runs = []
    for name in os.listdir(bucket_abs):
        p = os.path.join(bucket_abs, name)
        if os.path.isdir(p) and os.path.isfile(os.path.join(p, "_BACKUP_OK.txt")):
            runs.append((os.path.getmtime(os.path.join(p, "_BACKUP_OK.txt")), name))
    if len(runs) <= keep:
        return
    runs.sort()  # oldest first
    for _mt, name in runs[:len(runs) - keep]:
        victim = os.path.join(bucket_abs, name)
        shutil.rmtree(victim, ignore_errors=True)
        log(f"  retention: pruned old run {bucket_rel}/{name}")


_CONTAINER_LINE = re.compile(r"container:\s+(\S+)\s+\(project=")
_VOLUME_LINE = re.compile(r"volume\s*->\s*(\S+)")


def _reset_progress():
    STATE["step"] = ""
    STATE["done"] = 0
    STATE["total"] = 0
    STATE["container"] = ""
    STATE["stopping"] = False


def _backup_progress_line(line):
    """Stream callback: log the line and update the live progress counter."""
    log("  " + line)
    m = _CONTAINER_LINE.search(line)
    if m:
        STATE["done"] = STATE.get("done", 0) + 1
        STATE["container"] = m.group(1)
        STATE["step"] = m.group(1)
        return
    mv = _VOLUME_LINE.search(line)
    if mv:
        cur = STATE.get("container", "")
        STATE["step"] = f"{cur} \u00b7 volume {mv.group(1)}" if cur else f"volume {mv.group(1)}"


def _write_selection_file(names, container_mounts):
    """Write a per-container TSV selection file for backup.sh; return its path.

    One line per named container: ``<container>\t<skip_network>\t<spec>`` where
    spec is ``*`` (all mounts) or a comma-separated list of mount keys
    (``vol:<name>`` / ``bind:<dest>``). Returns None when there is nothing to
    write (so backup.sh defaults every container to all mounts).
    """
    if not names or not container_mounts:
        return None
    lines = []
    for n in names:
        cm = container_mounts.get(n) or {}
        sn = "1" if cm.get("skip_network") else "0"
        if cm.get("all", True):
            spec = "*"
        else:
            keys = [str(k) for k in (cm.get("mounts") or []) if k]
            # "__none__" is a sentinel matching no mount key, so a container can
            # be backed up (manifest/compose) with none of its data mounts.
            spec = ",".join(keys) if keys else "__none__"
        lines.append(f"{n}\t{sn}\t{spec}")
    os.makedirs(CONFIG_DIR, exist_ok=True)
    path = os.path.join(CONFIG_DIR,
                        f".selection-{datetime.now().strftime('%Y%m%d%H%M%S%f')}.tsv")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def do_backup(names, bucket, retention, label, running_only=False,
              container_mounts=None, skip_network=False):
    settings = load_settings()
    dest = settings["destination"]
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    bucket_rel = "/".join(p for p in (dest, bucket) if p)
    run_subpath = f"{bucket_rel}/{stamp}"
    extra = ["--running"] if running_only and not names else []
    # Build the child environment: a global network-skip default (used for
    # all/running scope and any container without an explicit line) plus a
    # per-container selection file when specific containers were chosen.
    env = dict(os.environ)
    env["SKIP_NETWORK_MOUNTS"] = "1" if skip_network else "0"
    sel_path = _write_selection_file(names, container_mounts)
    if sel_path:
        env["SELECTION_FILE"] = sel_path
    # Estimate how many containers this run will touch, for the progress bar.
    if names:
        total = len(names)
    else:
        conts = list_containers()
        total = (len([c for c in conts if c["state"] == "running"])
                 if running_only else len(conts))
    with _run_lock:
        STATE["busy"] = True
        STATE["current"] = f"backup: {label}"
        _reset_progress()
        STATE["total"] = total
        STATE["step"] = "starting\u2026"
        log(f"Backup started ({label}) -> {run_subpath}")
        try:
            rc, out = run_stream(["/usr/local/bin/backup.sh", run_subpath, *extra, *names],
                                 _backup_progress_line, timeout=3600, env=env)
        finally:
            if sel_path:
                try:
                    os.remove(sel_path)
                except OSError:
                    pass
        if rc == 0 and retention:
            _prune_bucket(bucket_rel, retention)
        STATE["busy"] = False
        STATE["current"] = ""
        _reset_progress()
        STATE["last_result"] = f"backup ({label}) rc={rc} @ {now()}"
        log(f"Backup finished ({label}) rc={rc}")
    return rc, out


def do_manual_backup(mode, names, container_mounts, skip_network):
    settings = load_settings()
    ret = settings["manual_retention"]
    if mode == "running":
        return do_backup([], "_manual", ret, "all running",
                         running_only=True, skip_network=skip_network)
    if mode == "selected":
        if not names:
            log("Backup skipped: scope 'selected' but no containers chosen")
            STATE["last_result"] = f"backup skipped: no containers @ {now()}"
            return 0, "no containers selected"
        label = ", ".join(names)
        return do_backup(names, "_manual", ret, label,
                         container_mounts=container_mounts, skip_network=skip_network)
    return do_backup([], "_manual", ret, "all containers", skip_network=skip_network)


def do_scheduled_backup(sched):
    mode = sched.get("mode", "all")
    skip_network = sched.get("skip_network", False)
    cmounts = sched.get("container_mounts", {})
    label = f"schedule '{sched['name']}'"
    if mode == "selected":
        names = sched["containers"]
        if not names:
            log(f"Backup skipped ({label}): mode 'selected' but no containers chosen")
            STATE["last_result"] = f"backup ({label}) skipped: no containers @ {now()}"
            return 0, "no containers selected"
        return do_backup(names, sched["id"], sched["retention"], label,
                         container_mounts=cmounts, skip_network=skip_network)
    if mode == "running":
        return do_backup([], sched["id"], sched["retention"], label,
                         running_only=True, skip_network=skip_network)
    return do_backup([], sched["id"], sched["retention"], label,
                     skip_network=skip_network)


def _valid_run(rel):
    target = _abs(rel)
    return target is not None and os.path.isfile(os.path.join(target, "_BACKUP_OK.txt"))


def do_restore(run_subpath, project=None, container=None):
    if not _valid_run(run_subpath):
        return 1, "invalid or unknown backup run"
    if project and not NAME_RE.match(project):
        return 1, "invalid project name"
    if container and not NAME_RE.match(container):
        return 1, "invalid container name"
    if container and not project:
        return 1, "container restore requires a project"
    args = [run_subpath]
    if project:
        args.append(project)
    if container:
        args.append(container)
    desc = run_subpath + (f"/{project}" if project else "") + (f"/{container}" if container else "")
    with _run_lock:
        STATE["busy"] = True
        STATE["current"] = f"restore: {desc}"
        _reset_progress()
        STATE["step"] = "restoring\u2026"
        log(f"Restore started ({desc})")
        rc, out = run_stream(["/usr/local/bin/restore.sh", *args],
                             lambda ln: log("  " + ln), timeout=3600)
        STATE["busy"] = False
        STATE["current"] = ""
        _reset_progress()
        STATE["last_result"] = f"restore ({desc}) rc={rc} @ {now()}"
        log(f"Restore finished ({desc}) rc={rc}")
    return rc, out


def delete_path(rel):
    target = _abs(rel)
    if target is None:
        return 1, "path outside backup root"
    if target == BACKUP_ROOT:
        return 1, "refusing to delete the backup root"
    if not os.path.exists(target):
        return 1, "not found"
    shutil.rmtree(target, ignore_errors=True)
    log(f"Deleted: {rel}")
    return 0, "deleted"


def read_log_tail(n=400):
    try:
        with open(LOG_PATH, encoding="utf-8") as fh:
            return fh.readlines()[-n:]
    except FileNotFoundError:
        return []


# --------------------------------------------------------------------------- #
# scheduler
# --------------------------------------------------------------------------- #
def _should_fire(sched, dt):
    if sched["frequency"] == "daily":
        return True
    if sched["frequency"] == "weekly":
        return dt.weekday() in sched["weekdays"]
    if sched["frequency"] == "monthly":
        import calendar
        last = calendar.monthrange(dt.year, dt.month)[1]
        target = min(sched["day_of_month"], last)  # clamp e.g. 31 -> 28/30
        return dt.day == target
    return False


def scheduler_loop():
    last_fired = {}  # schedule id -> "YYYY-MM-DD HH:MM"
    log("Scheduler thread started")
    while True:
        try:
            dt = datetime.now()
            hm = dt.strftime("%H:%M")
            stamp = dt.strftime("%Y-%m-%d %H:%M")
            for sched in load_schedules():
                if not sched["enabled"] or sched["time"] != hm:
                    continue
                if last_fired.get(sched["id"]) == stamp:
                    continue
                if not _should_fire(sched, dt):
                    continue
                last_fired[sched["id"]] = stamp
                if STATE["busy"]:
                    log(f"Schedule '{sched['name']}' due but tool busy; skipped this minute")
                    continue
                log(f"Scheduled backup triggered: '{sched['name']}' ({sched['frequency']})")
                threading.Thread(target=do_scheduled_backup, args=(sched,),
                                 daemon=True).start()
        except Exception as exc:  # noqa: BLE001
            log(f"WARN scheduler error: {exc}")
        time.sleep(20)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a):  # silence default request logging
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj))

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            return {}

    def _query(self):
        from urllib.parse import urlparse, parse_qs
        return parse_qs(urlparse(self.path).query)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(200, HTML, "text/html; charset=utf-8")
        elif path == "/api/state":
            self._json({
                "containers": list_containers(),
                "runs": list_runs(),
                "schedules": load_schedules(),
                "settings": load_settings(),
                "root": BACKUP_ROOT,
                "weekdayNames": WEEKDAY_NAMES,
                "busy": STATE["busy"],
                "current": STATE["current"],
                "last_result": STATE["last_result"],
                "step": STATE["step"],
                "done": STATE["done"],
                "total": STATE["total"],
                "stopping": STATE["stopping"],
            })
        elif path == "/api/containers":
            self._json(list_containers())
        elif path == "/api/mounts":
            self._json({"containers": list_mounts()})
        elif path == "/api/self-mounts":
            self._json({"mounts": list_self_mounts(), "root": BACKUP_ROOT})
        elif path == "/api/runs":
            self._json(list_runs())
        elif path == "/api/schedules":
            self._json({"schedules": load_schedules()})
        elif path == "/api/settings":
            self._json(load_settings())
        elif path == "/api/logs":
            self._json({"lines": read_log_tail(400)})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        body = self._body()
        if path == "/api/backup":
            if STATE["busy"]:
                return self._json({"error": "busy", "current": STATE["current"]}, 409)
            mode = str(body.get("mode", "all")).lower()
            if mode not in ("all", "selected", "running"):
                mode = "all"
            names = [n for n in body.get("containers", []) if NAME_RE.match(str(n))]
            skip_network = bool(body.get("skip_network", False))
            cmounts = _clean_container_mounts(body.get("container_mounts"))
            threading.Thread(target=do_manual_backup,
                             args=(mode, names, cmounts, skip_network),
                             daemon=True).start()
            self._json({"ok": True, "started": names if mode == "selected" else mode})
        elif path == "/api/restore":
            if STATE["busy"]:
                return self._json({"error": "busy", "current": STATE["current"]}, 409)
            run_subpath = str(body.get("run", ""))
            project = body.get("project") or None
            container = body.get("container") or None
            if not _valid_run(run_subpath):
                return self._json({"ok": False, "message": "invalid backup run"}, 400)
            threading.Thread(target=do_restore, args=(run_subpath, project, container),
                             daemon=True).start()
            self._json({"ok": True, "started": run_subpath})
        elif path == "/api/run-schedule":
            if STATE["busy"]:
                return self._json({"error": "busy", "current": STATE["current"]}, 409)
            sid = str(body.get("id", ""))
            sched = next((s for s in load_schedules() if s.get("id") == sid), None)
            if sched is None:
                return self._json({"ok": False, "message": "unknown schedule (save it first)"}, 404)
            threading.Thread(target=do_scheduled_backup, args=(sched,),
                             daemon=True).start()
            self._json({"ok": True, "started": sched.get("name", sid)})
        elif path == "/api/stop":
            ok, msg = stop_current()
            self._json({"ok": ok, "message": msg}, 200 if ok else 409)
        elif path == "/api/delete":
            rc, msg = delete_path(str(body.get("run", "")))
            self._json({"ok": rc == 0, "message": msg}, 200 if rc == 0 else 400)
        elif path == "/api/schedules":
            scheds = replace_schedules(body.get("schedules", []))
            self._json({"ok": True, "schedules": scheds})
        elif path == "/api/settings":
            cfg = load_settings()
            cfg["destination"] = _safe_subpath(body.get("destination", cfg["destination"]))
            mr = body.get("manual_retention")
            if isinstance(mr, int) and 1 <= mr <= 999:
                cfg["manual_retention"] = mr
            save_settings(cfg)
            log(f"Settings updated: {cfg}")
            self._json({"ok": True, "settings": cfg})
        else:
            self._json({"error": "not found"}, 404)


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Docker Backup</title>
<style>
:root{--bg:#0f1419;--panel:#1a212b;--panel2:#232c38;--line:#2e3a48;--fg:#e6edf3;
--muted:#8b98a8;--accent:#3b82f6;--ok:#22c55e;--warn:#f59e0b;--err:#ef4444;}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif;
background:var(--bg);color:var(--fg)}
header{background:var(--panel);padding:14px 20px;border-bottom:1px solid var(--line);
display:flex;align-items:center;gap:14px;position:sticky;top:0;z-index:5}
header h1{font-size:17px;margin:0;font-weight:600}
.badge{font-size:12px;padding:3px 9px;border-radius:20px;background:var(--panel2);color:var(--muted)}
.badge.busy{background:var(--warn);color:#1a1207}.badge.idle{background:#14361f;color:var(--ok)}
.progwrap{height:5px;background:var(--panel2);overflow:hidden}
#progBar{height:100%;width:0;background:var(--accent);transition:width .4s ease}
#progBar.indet{width:35%;animation:indet 1.1s ease-in-out infinite}
@keyframes indet{0%{margin-left:-35%}100%{margin-left:100%}}
nav{display:flex;gap:4px;padding:10px 20px 0;background:var(--panel);border-bottom:1px solid var(--line);flex-wrap:wrap}
nav button{background:none;border:none;color:var(--muted);padding:9px 16px;cursor:pointer;
border-bottom:2px solid transparent;font-size:14px}
nav button.active{color:var(--fg);border-bottom-color:var(--accent)}
main{padding:20px;max-width:1100px;margin:0 auto}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
.card h2{margin:0 0 12px;font-size:15px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
tr:last-child td{border-bottom:none}
.btn{background:var(--accent);color:#fff;border:none;padding:8px 14px;border-radius:7px;cursor:pointer;font-size:13px}
.btn:hover{filter:brightness(1.1)}.btn:disabled{opacity:.5;cursor:not-allowed}
.btn.sec{background:var(--panel2);color:var(--fg);border:1px solid var(--line)}
.btn.danger{background:var(--err)}.btn.sm{padding:5px 10px;font-size:12px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px;vertical-align:middle}
.dot.running{background:var(--ok)}.dot.exited,.dot.created,.dot.dead{background:var(--err)}
.dot.paused,.dot.restarting{background:var(--warn)}
.muted{color:var(--muted)}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.right{margin-left:auto}
input[type=text],input[type=number],select{background:var(--panel2);border:1px solid var(--line);
color:var(--fg);padding:7px 10px;border-radius:7px;font-size:13px}
textarea{background:var(--panel2);border:1px solid var(--line);color:var(--fg);padding:8px 10px;
border-radius:7px;font:12px/1.45 ui-monospace,Consolas,monospace;resize:vertical}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}
.mnt{border:1px solid var(--line);border-radius:9px;padding:12px 14px;margin-bottom:12px;background:var(--panel2)}
.mnt h3{margin:0 0 8px;font-size:14px}
.mnt table{width:100%}.mnt td{padding:5px 6px;vertical-align:middle}
.mnt .path{font:12px/1.4 ui-monospace,Consolas,monospace;word-break:break-all}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:7px;overflow:hidden}
.seg button{background:var(--panel);border:none;color:var(--muted);padding:5px 10px;cursor:pointer;font-size:12px}
.seg button.on{background:var(--accent);color:#fff}
.seg button.on.skip{background:var(--err)}.seg button.on.keep{background:var(--ok);color:#06210f}
input[type=checkbox]{width:16px;height:16px;accent-color:var(--accent)}
label{display:inline-flex;align-items:center;gap:6px}
.chip{background:var(--panel2);border:1px solid var(--line);border-radius:20px;padding:4px 10px;
display:inline-flex;align-items:center;gap:8px;font-size:13px}
.chip.on{background:#16324f;border-color:var(--accent)}
.chip button{background:none;border:none;color:var(--muted);cursor:pointer;font-size:15px;line-height:1}
pre{background:#0a0e13;border:1px solid var(--line);border-radius:8px;padding:12px;overflow:auto;
max-height:65vh;font:12px/1.45 ui-monospace,Consolas,monospace;white-space:pre-wrap;margin:0}
.toast{position:fixed;right:18px;bottom:18px;background:var(--panel2);border:1px solid var(--line);
padding:12px 16px;border-radius:9px;box-shadow:0 6px 24px rgba(0,0,0,.4);max-width:360px;opacity:0;
transition:opacity .2s;z-index:20}.toast.show{opacity:1}
.tag{font-size:11px;color:var(--muted)}.hidden{display:none}
summary{cursor:pointer;font-weight:600}details{margin:6px 0}
.dayGroup>summary.daySummary{font-size:15px;padding:8px 4px;border-bottom:2px solid var(--line);margin-bottom:4px}
.dayGroup>details{margin-left:14px}
tr.grp td{background:var(--panel2);border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
tr.grp b{font-size:13px}tr.member td{background:transparent}
tr.ctnRow td{background:transparent;color:var(--muted);font-size:12px;border-bottom:1px dashed var(--line)}
.caret{display:inline-block;width:16px;text-align:center;cursor:pointer;color:var(--muted);user-select:none;margin-right:4px}
.stackName{cursor:pointer;user-select:none}.stackName:hover{text-decoration:underline}
.sched{border:1px solid var(--line);border-radius:9px;padding:14px;margin-bottom:12px;background:var(--panel2)}
.sched .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-top:10px}
.sched .fld{display:flex;flex-direction:column;gap:4px}
.sched-proj{border:1px solid var(--line);border-radius:7px;padding:8px 10px;margin-top:8px}
.sched-head .sched-caret{color:var(--muted);user-select:none;width:14px;text-align:center}
.chip.sm{font-size:10px;padding:1px 6px}
.sched .fld span{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.mntpanel{border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:6px 0 10px;background:var(--bg)}
.mntopt{display:flex;align-items:flex-start;gap:8px;padding:3px 2px}
.mntopt .mlbl{font:12px/1.4 ui-monospace,Consolas,monospace;word-break:break-all}
.mexp{color:var(--muted)}.mexp:hover{color:var(--fg)}
.schctn{display:inline-block;vertical-align:top;margin:0 8px 6px 0}
.schctn .mntpanel{min-width:280px}
</style></head>
<body>
<header>
  <h1>🐳 Docker Backup</h1>
  <span id="statusBadge" class="badge idle">idle</span>
  <span id="progText" class="tag"></span>
  <button id="btnStop" class="btn danger sm hidden" onclick="stopJob()">⏹ Stop</button>
  <span id="lastResult" class="badge"></span>
  <span class="right tag" id="clock"></span>
</header>
<div id="progWrap" class="progwrap hidden"><div id="progBar"></div></div>
<nav>
  <button data-tab="backup" class="active">Backup</button>
  <button data-tab="restore">Restore</button>
  <button data-tab="schedules">Schedules</button>
  <button data-tab="settings">Settings</button>
  <button data-tab="logs">Logs</button>
</nav>
<main>
  <!-- BACKUP -->
  <section id="tab-backup">
    <div class="card">
      <div class="row">
        <h2 style="margin:0">Containers</h2>
        <div class="right row">
          <button class="btn sec sm" onclick="refresh()">↻ Refresh</button>
          <button class="btn" id="btnBackupSel" onclick="backupSelected()">Back up selected</button>
          <button class="btn sec" id="btnBackupAll" onclick="backupAll()">Back up all containers</button>
        </div>
      </div>
      <p class="tag" style="margin:6px 0 0">Pick containers to back up now. Expand a container to choose <b>which mounts</b> to include — everything needed for a full restore (manifest, databases, compose) is always captured regardless. Per container you can exclude its network (NFS / SMB / CIFS) mounts.</p>
      <table>
        <thead><tr><th style="width:34px"><input type="checkbox" id="selAll" onclick="toggleAll(this)"></th>
        <th>Container</th><th>Project</th><th>Mounts</th><th>State</th><th></th></tr></thead>
        <tbody id="ctn"></tbody>
      </table>
    </div>
  </section>
  <!-- RESTORE -->
  <section id="tab-restore" class="hidden">
    <div class="card">
      <div class="row"><h2 style="margin:0">Backup runs found</h2>
        <span class="right tag" id="rootTag"></span></div>
      <p class="tag" style="margin:6px 0 0">Discovered by scanning the backups root. After a rebuild, point the tool at the same root and every prior run appears here automatically.</p>
      <div id="runs" style="margin-top:10px"></div>
    </div>
  </section>
  <!-- SCHEDULES -->
  <section id="tab-schedules" class="hidden">
    <div class="card">
      <div class="row"><h2 style="margin:0">Schedules</h2>
        <div class="right row">
          <button class="btn sec sm" onclick="addSchedule()">+ Add schedule</button>
        </div></div>
      <p class="tag" style="margin:6px 0 0">Each schedule runs on its own frequency, keeps its own number of recent runs, and (in <b>Selected</b> scope) has its own per-container mount selection. Save each schedule with its own Save button.</p>
      <div id="scheds" style="margin-top:12px"></div>
    </div>
  </section>
  <!-- SETTINGS -->
  <section id="tab-settings" class="hidden">
    <div class="card">
      <h2>Storage</h2>
      <p class="tag" style="margin:0 0 12px">The <b>backups root</b> is the host folder mounted into the docker-backup container at <code>/backups</code>. It is auto-detected from the container's own mounts below. To change it, edit the volume mapping in <code>compose.yaml</code> and rebuild — every prior run under the same root is rediscovered automatically.</p>
      <div class="row" style="margin-bottom:8px">
        <label>Detected mounts on <b>docker-backup</b></label>
        <button class="right btn sec sm" onclick="loadSelfMounts()">↻ Re-read mounts</button>
      </div>
      <div id="selfMounts" class="tag" style="margin-bottom:12px">loading…</div>
      <div class="row" style="margin-bottom:12px">
        <div class="fld" style="display:flex;flex-direction:column;gap:4px">
          <label>Backups root <span class="tag">(active mount → /backups)</span></label>
          <input type="text" id="setRoot" disabled style="min-width:280px">
        </div>
      </div>
      <div class="row" style="margin-bottom:12px">
        <div class="fld" style="display:flex;flex-direction:column;gap:4px">
          <label>Destination subfolder <span class="tag">(optional, under the root)</span></label>
          <input type="text" id="setDest" placeholder="e.g. nas or 2026" style="min-width:280px">
        </div>
      </div>
      <div class="row" style="margin-bottom:16px">
        <label>Keep last
          <input type="number" id="setManualRet" min="1" max="999" style="width:70px">
          manual backups</label>
      </div>
      <button class="btn" onclick="saveSettings()">Save settings</button>
      <span id="setSaved" class="tag" style="margin-left:10px"></span>
    </div>
  </section>
  <!-- LOGS -->
  <section id="tab-logs" class="hidden">
    <div class="card">
      <div class="row" style="margin-bottom:10px">
        <h2 style="margin:0">Activity log</h2>
        <button class="right btn sec sm" onclick="loadLogs()">↻ Refresh</button>
        <label class="row tag"><input type="checkbox" id="autoLog" checked> auto</label>
      </div>
      <pre id="log">loading…</pre>
    </div>
  </section>
</main>
<div id="toast" class="toast"></div>
<script>
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
let CT=[], SCHEDS=[], SETTINGS={}, WD=["Mon","Tue","Wed","Thu","Fri","Sat","Sun"];
let MNT={};            // container name -> [ {kind,mkey,label,dest,network} ]
let BK_EXP=new Set();  // backup tab: containers whose mount panel is expanded
let BK_SEL=new Set();  // backup tab: containers ticked for backup
let GRP_OPEN=new Set();// backup tab: expanded project groups
let SCH_COLLAPSED=new Set();
let SCH_MEXP=new Set();  // schedules: "<schedId>|<container>" with mount panel open
let SCH_INIT=false;
let SCH_BASELINE={};
function schSnapshot(){SCH_BASELINE={};SCHEDS.forEach(s=>{SCH_BASELINE[s.id]=JSON.stringify(s)})}
function schDirty(s){return SCH_BASELINE[s.id]!==JSON.stringify(s)}
function schAnyDirty(){return SCHEDS.some(schDirty)}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
function fmtSize(b){if(!b)return"0 B";const u=["B","KB","MB","GB","TB"];let i=0;while(b>=1024&&i<u.length-1){b/=1024;i++}return b.toFixed(b<10&&i>0?1:0)+" "+u[i]}
function _p2(x){return String(x).padStart(2,"0")}
function fmtDate(ts){const n=new Date(ts*1000);return _p2(n.getDate())+"."+_p2(n.getMonth()+1)+"."+n.getFullYear()}
function fmtTime(ts){const n=new Date(ts*1000);return _p2(n.getHours())+":"+_p2(n.getMinutes())+":"+_p2(n.getSeconds())}
function toast(m){const t=$("#toast");t.textContent=m;t.classList.add("show");clearTimeout(t._t);t._t=setTimeout(()=>t.classList.remove("show"),3500)}
async function api(path,opts){const r=await fetch(path,opts);let j={};try{j=await r.json()}catch(e){}if(!r.ok)throw new Error(j.error||j.message||r.status);return j}

$$("nav button").forEach(b=>b.onclick=()=>{
  const cur=$$("nav button").find(x=>x.classList.contains("active"));
  if(cur&&cur.dataset.tab==="schedules"&&b.dataset.tab!=="schedules"&&schAnyDirty()
     &&!confirm("You have unsaved schedule changes. Leave without saving?"))return;
  $$("nav button").forEach(x=>x.classList.remove("active"));b.classList.add("active");
  ["backup","restore","schedules","settings","logs"].forEach(t=>$("#tab-"+t).classList.toggle("hidden",t!==b.dataset.tab));
  if(b.dataset.tab==="logs")loadLogs();
  if(b.dataset.tab==="settings")loadSelfMounts();
});

/* ---------------- Backup ---------------- */
function _q(s){return String(s).replace(/'/g,"\\'")}
function mountSummary(name){
  const ms=MNT[name];
  if(ms===undefined)return '<span class=tag>…</span>';
  if(!ms.length)return '<span class=tag>no mounts</span>';
  const net=ms.filter(m=>m.network).length;
  return `<span class=tag>${ms.length} mount${ms.length>1?'s':''}${net?` · ${net} net`:''}</span>`;
}
function bkPanel(name){
  const ms=MNT[name];
  const head=`<div class="row" style="margin-bottom:8px;gap:8px">
      <span class=tag>Mounts to back up</span>
      <button class="btn sec sm" onclick="bkAllMounts('${_q(name)}',true)">All</button>
      <button class="btn sec sm" onclick="bkAllMounts('${_q(name)}',false)">None</button>
      <label class="chip"><input type=checkbox class="bks" data-c="${esc(name)}"> exclude network (NFS/SMB/CIFS)</label>
    </div>`;
  if(ms===undefined)return `<div class="mntpanel">${head}<div class=tag>loading mounts…</div></div>`;
  if(!ms.length)return `<div class="mntpanel">${head}<div class=tag>No volumes or bind mounts — only the container manifest/compose are captured.</div></div>`;
  const rows=ms.map(m=>{
    const net=m.network?' <span class="tag">network</span>':'';
    return `<label class="mntopt"><input type=checkbox class="bkm" data-c="${esc(name)}" data-mkey="${esc(m.mkey)}" checked>
      <span class="mlbl"><b>${esc(m.kind)}</b> ${esc(m.label)}${net}<span class="tag"> → ${esc(m.dest)}</span></span></label>`;
  }).join("");
  return `<div class="mntpanel">${head}${rows}</div>`;
}
function bkAllMounts(name,on){$$('.bkm[data-c="'+CSS.escape(name)+'"]').forEach(x=>x.checked=on)}
function renderContainers(){
  const tb=$("#ctn");tb.innerHTML="";
  if(!CT.length){tb.innerHTML='<tr><td colspan=6 class=muted>No containers found.</td></tr>';return}
  const groups={};CT.forEach(c=>{(groups[c.project]=groups[c.project]||[]).push(c)});
  Object.keys(groups).sort().forEach(proj=>{
    const list=groups[proj];
    const running=list.filter(c=>c.state==="running").length;
    const label=proj==="_standalone"?"(standalone)":proj;
    const gid="g_"+proj.replace(/[^A-Za-z0-9]/g,"_");
    const open=GRP_OPEN.has(gid);
    const gh=document.createElement("tr");gh.className="grp";
    gh.innerHTML=`<td><input type=checkbox id="${gid}_cb" data-gid="${gid}" title="Select stack" onclick="toggleGroup('${gid}',this)"></td>
      <td colspan=4><span class=caret onclick="toggleCollapse('${gid}')">${open?'\u25be':'\u25b8'}</span>
        <b class=stackName title="Select all containers in this stack" onclick="selectStack('${gid}')">\uD83D\uDCE6 ${esc(label)}</b>
        <span class=tag>&nbsp;${list.length} container${list.length>1?"s":""} \u00b7 ${running} running</span></td>
      <td class=right><button class="btn sm" onclick="backupProject('${esc(proj)}')">Back up stack</button></td>`;
    tb.appendChild(gh);
    list.forEach(c=>{
      const exp=BK_EXP.has(c.name);
      const tr=document.createElement("tr");tr.className="member "+gid+(open?"":" hidden");
      tr.innerHTML=`<td style="padding-left:30px"><input type=checkbox class="csel ${gid}" value="${esc(c.name)}" ${BK_SEL.has(c.name)?'checked':''} onchange="bkSel('${_q(c.name)}',this.checked)"></td>
        <td><b>${esc(c.name)}</b></td><td class=muted>${esc(c.project)}</td>
        <td><span class="mexp" style="cursor:pointer" onclick="bkToggle('${_q(c.name)}')">${exp?'\u25be':'\u25b8'} ${mountSummary(c.name)}</span></td>
        <td><span class="dot ${c.state}"></span>${c.state}</td>
        <td><button class="btn sm" onclick="backupOne('${esc(c.name)}')">Back up</button></td>`;
      tb.appendChild(tr);
      if(exp){
        const pr=document.createElement("tr");pr.className="member mntrow "+gid+(open?"":" hidden");
        pr.innerHTML=`<td></td><td colspan=5>${bkPanel(c.name)}</td>`;
        tb.appendChild(pr);
      }
    });
  });
  syncStates();
}
function bkSel(name,on){if(on)BK_SEL.add(name);else BK_SEL.delete(name);syncStates()}
function bkToggle(name){if(BK_EXP.has(name))BK_EXP.delete(name);else BK_EXP.add(name);renderContainers()}
function toggleAll(cb){$$(".csel").forEach(x=>{x.checked=cb.checked;bkSel(x.value,cb.checked)});syncStates()}
function toggleGroup(gid,cb){$$(".csel."+gid).forEach(x=>{x.checked=cb.checked;bkSel(x.value,cb.checked)});syncStates()}
function selectStack(gid){const boxes=$$(".csel."+gid);const all=boxes.length&&[...boxes].every(x=>x.checked);
  boxes.forEach(x=>{x.checked=!all;bkSel(x.value,!all)});syncStates()}
function syncStates(){
  $$("[data-gid]").forEach(cb=>{
    const boxes=$$(".csel."+cb.dataset.gid);
    cb.checked=boxes.length>0&&[...boxes].every(x=>x.checked);
  });
  const all=$$(".csel"),sel=$("#selAll");
  if(sel)sel.checked=all.length>0&&[...all].every(x=>x.checked);
}
function toggleCollapse(gid){if(GRP_OPEN.has(gid))GRP_OPEN.delete(gid);else GRP_OPEN.add(gid);renderContainers()}
function backupProject(proj){const names=CT.filter(c=>c.project===proj).map(c=>c.name);if(!names.length)return;backupNames(names)}
function selectedNames(){return $$(".csel").filter(x=>x.checked).map(x=>x.value)}
function bkSelectionFor(names){
  // build {container:{all,mounts,skip_network}} from the rendered panels
  const cm={};
  names.forEach(n=>{
    const boxes=$$('.bkm[data-c="'+CSS.escape(n)+'"]');
    const skip=$$('.bks[data-c="'+CSS.escape(n)+'"]').some(x=>x.checked);
    if(!boxes.length){cm[n]={all:true,mounts:[],skip_network:skip};return}
    const checked=boxes.filter(x=>x.checked).map(x=>x.dataset.mkey);
    const all=checked.length===boxes.length;
    cm[n]={all,mounts:all?[]:checked,skip_network:skip};
  });
  return cm;
}
async function backupRun(payload){
  try{await api("/api/backup",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify(payload)});
    toast("Backup started");setTimeout(refresh,800);
  }catch(e){toast("Error: "+e.message)}
}
function backupNames(names){
  // expand panels for any selected container so its mount choices are read
  const need=names.filter(n=>!BK_EXP.has(n));
  if(need.length){need.forEach(n=>BK_EXP.add(n));
    CT.forEach(c=>{if(names.includes(c.name))GRP_OPEN.add("g_"+c.project.replace(/[^A-Za-z0-9]/g,"_"))});
    renderContainers();}
  backupRun({mode:"selected",containers:names,container_mounts:bkSelectionFor(names)});
}
const backupSelected=()=>{const s=selectedNames();if(!s.length)return toast("Select at least one container");backupNames(s)};
const backupAll=()=>{if(!confirm("Back up ALL containers with all their mounts?"))return;backupRun({mode:"all",skip_network:false})};
const backupOne=n=>backupNames([n]);
async function stopJob(){
  if(!confirm("Stop the current backup/restore?\nThe in-progress run is incomplete and will be discarded."))return;
  try{const r=await api("/api/stop",{method:"POST"});toast(r.message||"stopping\u2026");setTimeout(refresh,600);
  }catch(e){toast("Error: "+e.message)}
}

/* ---------------- Restore ---------------- */
function renderRuns(runs){
  const el=$("#runs");el.innerHTML="";
  if(!runs.length){el.innerHTML='<p class=muted>No backup runs found under the root yet.</p>';return}
  // group runs by calendar date (DD.MM.YYYY), newest day first
  const days=[];const dayMap={};
  runs.forEach(r=>{const d=fmtDate(r.mtime||0);
    if(!dayMap[d]){dayMap[d]={date:d,mtime:r.mtime||0,runs:[]};days.push(dayMap[d])}
    dayMap[d].runs.push(r);
    if((r.mtime||0)>dayMap[d].mtime)dayMap[d].mtime=r.mtime||0;
  });
  days.sort((a,b)=>b.mtime-a.mtime);
  const today=fmtDate(Date.now()/1000);
  days.forEach(day=>{
    const dayWrap=document.createElement("details");
    dayWrap.className="dayGroup";
    dayWrap.open=(day.date===today);
    const dayBytes=day.runs.reduce((s,r)=>s+(r.size||0),0);
    const nRuns=day.runs.length;
    const head=document.createElement("summary");
    head.className="daySummary";
    head.innerHTML=`<b>${esc(day.date)}</b> <span class=tag>${nRuns} ${nRuns===1?"run":"runs"} · ${fmtSize(dayBytes)}</span>`;
    dayWrap.appendChild(head);
    day.runs.forEach(r=>{
      const det=document.createElement("details");
      const rows=r.projects.map(p=>{
        const ctnRows=(p.containers||[]).map(c=>`<tr class=ctnRow>
          <td style="padding-left:22px">↳ ${esc(c)}</td>
          <td class=tag></td>
          <td></td>
          <td class=right>
            <button class="btn sec sm" onclick="restore('${esc(r.run)}','${esc(p.name)}','${esc(c)}')">Restore container</button>
          </td></tr>`).join("");
        return `<tr>
        <td><b>${esc(p.name)}</b><div class=tag>${esc((p.containers||[]).join(", "))}</div></td>
        <td class=tag>${p.mtime?fmtTime(p.mtime):""}</td>
        <td>${fmtSize(p.size)}</td>
        <td class=right>
          <button class="btn sm" onclick="restore('${esc(r.run)}','${esc(p.name)}',null)">Restore project</button>
        </td></tr>${ctnRows}`;
      }).join("");
      det.innerHTML=`<summary><span class=tag>${r.mtime?fmtTime(r.mtime):""}</span> &nbsp;${esc(r.label)}
        <span class=tag>${fmtSize(r.size)}</span>
        <button class="btn sm" style="margin-left:10px" onclick="event.preventDefault();restore('${esc(r.run)}',null)">Restore whole run</button>
        <button class="btn sec sm danger" style="margin-left:6px" onclick="event.preventDefault();del('${esc(r.run)}')">Delete</button></summary>
        <table style="margin-top:8px">${rows||'<tr><td class=muted>empty</td></tr>'}</table>`;
      dayWrap.appendChild(det);
    });
    el.appendChild(dayWrap);
  });
}
async function restore(run,project,container){
  const what=container?`container "${container}"`:(project?`project "${project}"`:"the whole run");
  if(!confirm(`Restore ${what} from\n${run}?\n\nThis stops the affected container(s), overwrites their volumes with the backup, starts them again, and re-imports databases where needed. Current data is replaced.`))return;
  try{await api("/api/restore",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({run,project,container})});toast("Restore started");setTimeout(refresh,800);
  }catch(e){toast("Error: "+e.message)}
}
async function del(run){
  if(!confirm(`Delete backup\n${run}?\nThis cannot be undone.`))return;
  try{await api("/api/delete",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({run})});toast("Deleted");refresh();
  }catch(e){toast("Error: "+e.message)}
}

/* ---------------- Schedules ---------------- */
function renderSchedules(){
  const el=$("#scheds");el.innerHTML="";
  if(!SCHEDS.length){el.innerHTML='<p class=muted>No schedules. Add one to enable automatic backups.</p>'}
  SCHEDS.forEach((s,idx)=>{
    const mode=s.mode||'all';const isSel=mode==='selected';
    const groups={};CT.forEach(c=>{(groups[c.project]=groups[c.project]||[]).push(c)});
    const opts=Object.keys(groups).sort().map(proj=>{
      const chips=groups[proj].map(c=>{
        const on=isSel&&s.containers.includes(c.name);
        const exp=on&&SCH_MEXP.has(s.id+"|"+c.name);
        return `<div class="schctn">
          <label class="chip ${on?'on':''}"><input type=checkbox ${on?'checked':''}
            onchange="schCtn(${idx},'${_q(c.name)}',this.checked)"> ${esc(c.name)}</label>
          ${on?`<button class="btn sec sm" style="margin-left:4px" onclick="schMExp('${esc(s.id)}','${_q(c.name)}')">${exp?'\u25be mounts':'\u25b8 mounts'}</button>`:''}
          ${exp?schMountPanel(idx,c.name):''}</div>`;}).join(" ");
      const projNames=groups[proj].map(c=>c.name);
      const projSel=isSel&&projNames.every(n=>s.containers.includes(n));
      return `<div class="sched-proj">
        <div class="row" style="margin-bottom:4px">
          <span class=tag>${esc(proj)}</span>
          <label class="chip sm ${projSel?'on':''}"><input type=checkbox ${projSel?'checked':''}
            onchange="schProj(${idx},'${esc(proj)}',this.checked)"> whole project</label>
        </div>
        <div class="row">${chips}</div>
      </div>`;}).join("");
    const wdChips=WD.map((d,i)=>`<label class="chip ${s.weekdays.includes(i)?'on':''}">
      <input type=checkbox ${s.weekdays.includes(i)?'checked':''} onchange="schWd(${idx},${i},this.checked)"> ${d}</label>`).join(" ");
    const div=document.createElement("div");div.className="sched";
    const collapsed=SCH_COLLAPSED.has(s.id);
    const dirty=schDirty(s);
    const freqLabel=s.frequency==='weekly'?`weekly · ${(s.weekdays||[]).map(i=>WD[i]).join(',')||'no days'}`
      :s.frequency==='monthly'?`monthly · day ${s.day_of_month}`:'daily';
    const ctnLabel=mode==='all'?'all containers':mode==='running'?'all running':`${s.containers.length} container${s.containers.length===1?'':'s'}`;
    div.innerHTML=`
      <div class="row sched-head" onclick="schToggle('${esc(s.id)}',event)" style="cursor:pointer">
        <span class="sched-caret">${collapsed?'\u25b6':'\u25bc'}</span>
        <label onclick="event.stopPropagation()"><input type=checkbox ${s.enabled?'checked':''} onchange="SCHEDS[${idx}].enabled=this.checked"></label>
        <input type=text value="${esc(s.name)}" placeholder="Schedule name" style="min-width:220px" onclick="event.stopPropagation()"
          oninput="SCHEDS[${idx}].name=this.value">
        <span class="tag">${esc(freqLabel)} @ ${esc(s.time)} · ${ctnLabel} · keep ${s.retention}</span>
        ${s.enabled?'':'<span class="tag" style="opacity:.6">disabled</span>'}
        <span class="right row" onclick="event.stopPropagation()">
          ${dirty?'<span class="tag" style="color:var(--warn)">● unsaved</span>':'<span class="tag" style="color:var(--ok)">saved</span>'}
          <button class="btn sm ${dirty?'':'sec'}" onclick="saveSchedule(${idx})">Save</button>
          <button class="btn sec sm" onclick="runSchedule(${idx})">▶ Run now</button>
          <button class="btn sec sm danger" onclick="rmSchedule(${idx})">Remove</button>
        </span>
      </div>
      <div class="sched-body" style="${collapsed?'display:none':''}">
      <div class="grid">
        <div class="fld"><span>Frequency</span>
          <select onchange="SCHEDS[${idx}].frequency=this.value;renderSchedules()">
            ${["daily","weekly","monthly"].map(f=>`<option value="${f}" ${s.frequency===f?'selected':''}>${f}</option>`).join("")}
          </select></div>
        <div class="fld"><span>Time</span>
          <input type=text value="${esc(s.time)}" placeholder="HH:MM" oninput="SCHEDS[${idx}].time=this.value"></div>
        <div class="fld"><span>Keep last (runs)</span>
          <input type=number min=1 max=999 value="${s.retention}" oninput="SCHEDS[${idx}].retention=parseInt(this.value)||1"></div>
        ${s.frequency==="monthly"?`<div class="fld"><span>Day of month</span>
          <input type=number min=1 max=31 value="${s.day_of_month}" oninput="SCHEDS[${idx}].day_of_month=parseInt(this.value)||1"></div>`:""}
      </div>
      ${s.frequency==="weekly"?`<div style="margin-top:10px"><div class=tag style="margin-bottom:4px">Days of week</div><div class="row">${wdChips}</div></div>`:""}
      <div style="margin-top:12px">
        <div class="row" style="margin-bottom:6px"><div class=tag>Scope</div>
          <label class="chip ${mode==='all'?'on':''}"><input type=radio name="schmode_${esc(s.id)}" ${mode==='all'?'checked':''} onchange="schMode(${idx},'all')"> All</label>
          <label class="chip ${mode==='selected'?'on':''}"><input type=radio name="schmode_${esc(s.id)}" ${mode==='selected'?'checked':''} onchange="schMode(${idx},'selected')"> Selected</label>
          <label class="chip ${mode==='running'?'on':''}"><input type=radio name="schmode_${esc(s.id)}" ${mode==='running'?'checked':''} onchange="schMode(${idx},'running')"> All Running</label></div>
        ${mode!=='selected'?`<label class="chip ${s.skip_network?'on':''}" style="margin-bottom:6px"><input type=checkbox ${s.skip_network?'checked':''} onchange="SCHEDS[${idx}].skip_network=this.checked;renderSchedules()"> exclude network mounts (NFS/SMB/CIFS)</label>`:''}
        ${isSel?`<div class="row">${opts||'<span class=muted>no containers</span>'}</div>`:''}
      </div>
      </div>`;
    el.appendChild(div);
  });
}
function schMountPanel(idx,name){
  const s=SCHEDS[idx];const ms=MNT[name];
  const cm=(s.container_mounts&&s.container_mounts[name])||{all:true,mounts:[],skip_network:false};
  const head=`<div class="row" style="margin:6px 0;gap:8px">
    <button class="btn sec sm" onclick="schMntAll(${idx},'${_q(name)}',true)">All</button>
    <button class="btn sec sm" onclick="schMntAll(${idx},'${_q(name)}',false)">None</button>
    <label class="chip"><input type=checkbox ${cm.skip_network?'checked':''} onchange="schMntNet(${idx},'${_q(name)}',this.checked)"> exclude network</label></div>`;
  if(ms===undefined)return `<div class="mntpanel">${head}<div class=tag>loading mounts…</div></div>`;
  if(!ms.length)return `<div class="mntpanel">${head}<div class=tag>No mounts — manifest/compose only.</div></div>`;
  const rows=ms.map(m=>{
    const checked=cm.all?true:cm.mounts.includes(m.mkey);
    const net=m.network?' <span class="tag">network</span>':'';
    return `<label class="mntopt"><input type=checkbox ${checked?'checked':''} onchange="schMnt(${idx},'${_q(name)}','${esc(m.mkey)}',this.checked)">
      <span class="mlbl"><b>${esc(m.kind)}</b> ${esc(m.label)}${net}<span class="tag"> → ${esc(m.dest)}</span></span></label>`;
  }).join("");
  return `<div class="mntpanel">${head}${rows}</div>`;
}
function _schCM(s,name){if(!s.container_mounts)s.container_mounts={};
  if(!s.container_mounts[name])s.container_mounts[name]={all:true,mounts:[],skip_network:false};
  return s.container_mounts[name]}
function schMnt(idx,name,mkey,on){const s=SCHEDS[idx];const cm=_schCM(s,name);
  const all=(MNT[name]||[]).map(m=>m.mkey);
  let set=new Set(cm.all?all:cm.mounts);
  if(on)set.add(mkey);else set.delete(mkey);
  cm.mounts=all.filter(k=>set.has(k));cm.all=cm.mounts.length===all.length;renderSchedules()}
function schMntAll(idx,name,on){const s=SCHEDS[idx];const cm=_schCM(s,name);
  cm.all=on;cm.mounts=[];renderSchedules()}
function schMntNet(idx,name,on){const s=SCHEDS[idx];const cm=_schCM(s,name);cm.skip_network=on;renderSchedules()}
function schMExp(id,name){const k=id+"|"+name;if(SCH_MEXP.has(k))SCH_MEXP.delete(k);else SCH_MEXP.add(k);renderSchedules()}
function schToggle(id,ev){if(ev)ev.stopPropagation();
  if(SCH_COLLAPSED.has(id))SCH_COLLAPSED.delete(id);else SCH_COLLAPSED.add(id);renderSchedules()}
function schMode(idx,mode){const s=SCHEDS[idx];s.mode=mode;renderSchedules()}
function schProj(idx,proj,on){const s=SCHEDS[idx];const projNames=CT.filter(c=>c.project===proj).map(c=>c.name);
  s.mode='selected';let set=new Set(s.containers||[]);
  projNames.forEach(n=>{if(on)set.add(n);else set.delete(n)});s.containers=[...set];renderSchedules()}
function schCtn(idx,name,on){const s=SCHEDS[idx];s.mode='selected';let set=new Set(s.containers||[]);
  if(on)set.add(name);else{set.delete(name);if(s.container_mounts)delete s.container_mounts[name];SCH_MEXP.delete(s.id+"|"+name);}
  s.containers=[...set];renderSchedules()}
function schWd(idx,day,on){const s=SCHEDS[idx];let set=new Set(s.weekdays);if(on)set.add(day);else set.delete(day);s.weekdays=[...set].sort((a,b)=>a-b);renderSchedules()}
function addSchedule(){SCHEDS.push({id:"sched-"+Date.now().toString(36),name:"New backup",enabled:true,
  frequency:"daily",time:"03:00",weekdays:[0,1,2,3,4,5,6],day_of_month:1,mode:"all",containers:[],
  skip_network:false,container_mounts:{},retention:7});renderSchedules()}
function rmSchedule(idx){
  if(schDirty(SCHEDS[idx])&&!confirm("This schedule has unsaved changes. Remove it anyway?"))return;
  SCHEDS.splice(idx,1);renderSchedules()}
async function saveSchedule(idx){
  try{const r=await api("/api/schedules",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({schedules:SCHEDS})});SCHEDS=r.schedules;schSnapshot();renderSchedules();
    toast("Schedule saved ✔");
  }catch(e){toast("Error: "+e.message)}
}
async function runSchedule(idx){
  const s=SCHEDS[idx];
  if(schDirty(s)){
    if(!confirm("Save changes and run this schedule now?"))return;
    try{const r=await api("/api/schedules",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({schedules:SCHEDS})});SCHEDS=r.schedules;schSnapshot();renderSchedules();
    }catch(e){toast("Error saving: "+e.message);return}
  }
  try{const r=await api("/api/run-schedule",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({id:s.id})});
    toast("Running schedule: "+(r.started||s.name));refresh();
  }catch(e){toast("Error: "+(e.message||"could not start"))}
}

/* ---------------- Settings ---------------- */
function renderSettings(){
  $("#setRoot").value=SETTINGS.rootHost||SETTINGS.root||"";
  $("#setDest").value=SETTINGS.destination||"";
  $("#setManualRet").value=SETTINGS.manual_retention||10;
}
async function loadSelfMounts(){
  const box=$("#selfMounts");
  try{const r=await api("/api/self-mounts");
    SETTINGS.root=r.root;
    const ms=r.mounts||[];
    const root=ms.find(m=>m.is_root);
    SETTINGS.rootHost=root?root.source:r.root;
    $("#setRoot").value=SETTINGS.rootHost||"";
    if(!ms.length){box.innerHTML='<span class=muted>none detected</span>';return}
    box.innerHTML=ms.map(m=>`<div class="row" style="gap:8px;margin:2px 0">
      <button class="btn ${m.is_root?'':'sec'} sm" onclick="pickRoot('${_q(m.source)}')">${m.is_root?'\u25cf root':'use'}</button>
      <span class="mlbl">${esc(m.dest)} <span class=tag>\u2190 ${esc(m.source)}</span></span></div>`).join("");
  }catch(e){box.innerHTML='<span class=muted>error reading mounts</span>'}
}
function pickRoot(src){$("#setRoot").value=src;
  toast("The backups root is fixed by the compose mount \u2192 /backups. Host path: "+src)}
async function saveSettings(){
  const body={destination:$("#setDest").value,manual_retention:parseInt($("#setManualRet").value)||10};
  try{const r=await api("/api/settings",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify(body)});SETTINGS=Object.assign(SETTINGS,r.settings);renderSettings();
    $("#setSaved").textContent="saved \u2714";setTimeout(()=>$("#setSaved").textContent="",2500);
  }catch(e){toast("Error: "+e.message)}
}

/* ---------------- Mounts data ---------------- */
async function loadAllMounts(){
  try{const r=await api("/api/mounts");const m={};
    (r.containers||[]).forEach(c=>{m[c.container]=c.mounts||[]});
    MNT=m;renderContainers();renderSchedules();
  }catch(e){}
}

/* ---------------- common ---------------- */
function setStatus(s){
  const b=$("#statusBadge");
  b.textContent=s.busy?("⏳ "+(s.current||"working…")):"idle";
  b.className="badge "+(s.busy?"busy":"idle");
  $("#lastResult").textContent=s.last_result||"";
  const pt=$("#progText"),pw=$("#progWrap"),pb=$("#progBar"),bs=$("#btnStop");
  if(s.busy){
    pw.classList.remove("hidden");
    if(bs){bs.classList.remove("hidden");bs.disabled=!!s.stopping;bs.textContent=s.stopping?"⏹ Stopping…":"⏹ Stop";}
    if(s.total){
      const pct=Math.min(100,Math.round((s.done/s.total)*100));
      pb.classList.remove("indet");pb.style.width=pct+"%";
      pt.textContent=`${s.done}/${s.total}`+(s.step?" — "+s.step:"");
    }else{
      pb.classList.add("indet");pt.textContent=s.step||s.current||"working…";
    }
  }else{
    pw.classList.add("hidden");pb.classList.remove("indet");pb.style.width="0";pt.textContent="";
    if(bs)bs.classList.add("hidden");
  }
  ["btnBackupSel","btnBackupAll"].forEach(id=>{const e=$("#"+id);if(e)e.disabled=s.busy});
}
async function refresh(){
  try{const s=await api("/api/state");
    CT=s.containers;SCHEDS=s.schedules;WD=s.weekdayNames||WD;
    if(!SCH_INIT){SCHEDS.forEach(x=>SCH_COLLAPSED.add(x.id));SCH_INIT=true;}
    schSnapshot();
    SETTINGS=Object.assign({},s.settings,{root:s.root,rootHost:SETTINGS.rootHost});
    $("#rootTag").textContent="root: "+s.root;
    renderContainers();renderRuns(s.runs);renderSchedules();renderSettings();setStatus(s);
    loadAllMounts();
  }catch(e){toast("Error: "+e.message)}
}
async function loadLogs(){
  try{const r=await api("/api/logs");const el=$("#log");
    el.textContent=r.lines.join("")||"(empty)";el.scrollTop=el.scrollHeight;
  }catch(e){}
}
window.addEventListener("beforeunload",e=>{if(schAnyDirty()){e.preventDefault();e.returnValue=""}});
setInterval(()=>{const n=new Date();const p=x=>String(x).padStart(2,"0");
  $("#clock").textContent=p(n.getDate())+"."+p(n.getMonth()+1)+"."+n.getFullYear()+" "+p(n.getHours())+":"+p(n.getMinutes())+":"+p(n.getSeconds())},1000);
let _wasBusy=false;
setInterval(async()=>{try{const s=await api("/api/state");setStatus(s);
  // while busy, keep the Logs tab live so progress is visible there too
  if(s.busy && !$("#tab-logs").classList.contains("hidden"))loadLogs();
  // when a backup/restore finishes, refresh the Restore list in the background
  if(_wasBusy&&!s.busy){renderRuns(s.runs);}
  _wasBusy=!!s.busy;
}catch(e){}},2000);
setInterval(()=>{if($("#autoLog")?.checked && !$("#tab-logs").classList.contains("hidden"))loadLogs()},5000);
refresh();
</script>
</body></html>
"""


def main():
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(CONFIG_DIR, exist_ok=True)
    load_settings()
    load_schedules()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    log(f"Docker Backup GUI listening on :{PORT} (root {BACKUP_ROOT})")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
