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
STATE = {"busy": False, "current": "", "last_result": ""}


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
        "containers": [],                    # [] / "all" => all running
        "retention": DEFAULT_RETENTION,      # keep last N runs of this schedule
    }


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
    if cs in ("all", None) or cs == []:
        out["containers"] = []
    elif isinstance(cs, list):
        out["containers"] = [c for c in (str(x).strip() for x in cs) if NAME_RE.match(c)]
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
        projects.append({"name": proj, "containers": containers,
                         "size": dir_size(ppath)})
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


def do_backup(names, bucket, retention, label):
    settings = load_settings()
    dest = settings["destination"]
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    bucket_rel = "/".join(p for p in (dest, bucket) if p)
    run_subpath = f"{bucket_rel}/{stamp}"
    with _run_lock:
        STATE["busy"] = True
        STATE["current"] = f"backup: {label}"
        log(f"Backup started ({label}) -> {run_subpath}")
        rc, out = run(["/usr/local/bin/backup.sh", run_subpath, *names], timeout=3600)
        for ln in out.splitlines():
            log("  " + ln)
        if rc == 0 and retention:
            _prune_bucket(bucket_rel, retention)
        STATE["busy"] = False
        STATE["current"] = ""
        STATE["last_result"] = f"backup ({label}) rc={rc} @ {now()}"
        log(f"Backup finished ({label}) rc={rc}")
    return rc, out


def do_manual_backup(names):
    settings = load_settings()
    label = "all running" if not names else ", ".join(names)
    return do_backup(names, "_manual", settings["manual_retention"], label)


def do_scheduled_backup(sched):
    names = sched["containers"] if sched["containers"] else []
    label = f"schedule '{sched['name']}'"
    return do_backup(names, sched["id"], sched["retention"], label)


def _valid_run(rel):
    target = _abs(rel)
    return target is not None and os.path.isfile(os.path.join(target, "_BACKUP_OK.txt"))


def do_restore(run_subpath, project=None):
    if not _valid_run(run_subpath):
        return 1, "invalid or unknown backup run"
    if project and not NAME_RE.match(project):
        return 1, "invalid project name"
    args = [run_subpath]
    if project:
        args.append(project)
    desc = run_subpath + (f"/{project}" if project else "")
    with _run_lock:
        STATE["busy"] = True
        STATE["current"] = f"restore: {desc}"
        log(f"Restore started ({desc})")
        rc, out = run(["/usr/local/bin/restore.sh", *args], timeout=3600)
        for ln in out.splitlines():
            log("  " + ln)
        STATE["busy"] = False
        STATE["current"] = ""
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
            })
        elif path == "/api/containers":
            self._json(list_containers())
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
            names = [n for n in body.get("containers", []) if NAME_RE.match(str(n))]
            threading.Thread(target=do_manual_backup, args=(names,), daemon=True).start()
            self._json({"ok": True, "started": names or "all running"})
        elif path == "/api/restore":
            if STATE["busy"]:
                return self._json({"error": "busy", "current": STATE["current"]}, 409)
            run_subpath = str(body.get("run", ""))
            project = body.get("project") or None
            if not _valid_run(run_subpath):
                return self._json({"ok": False, "message": "invalid backup run"}, 400)
            threading.Thread(target=do_restore, args=(run_subpath, project),
                             daemon=True).start()
            self._json({"ok": True, "started": run_subpath})
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
tr.grp td{background:var(--panel2);border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
tr.grp b{font-size:13px}tr.member td{background:transparent}
.caret{display:inline-block;width:16px;text-align:center;cursor:pointer;color:var(--muted);user-select:none;margin-right:4px}
.sched{border:1px solid var(--line);border-radius:9px;padding:14px;margin-bottom:12px;background:var(--panel2)}
.sched .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-top:10px}
.sched .fld{display:flex;flex-direction:column;gap:4px}
.sched-proj{border:1px solid var(--line);border-radius:7px;padding:8px 10px;margin-top:8px}
.sched-head .sched-caret{color:var(--muted);user-select:none;width:14px;text-align:center}
.chip.sm{font-size:10px;padding:1px 6px}
.sched .fld span{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
</style></head>
<body>
<header>
  <h1>🐳 Docker Backup</h1>
  <span id="statusBadge" class="badge idle">idle</span>
  <span id="lastResult" class="badge"></span>
  <span class="right tag" id="clock"></span>
</header>
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
          <button class="btn sec" id="btnBackupAll" onclick="backupAll()">Back up all running</button>
        </div>
      </div>
      <table>
        <thead><tr><th style="width:34px"><input type="checkbox" id="selAll" onclick="toggleAll(this)"></th>
        <th>Container</th><th>Project</th><th>Image</th><th>State</th><th></th></tr></thead>
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
      <p class="tag" style="margin:6px 0 0">Each schedule runs on its own frequency and keeps its own number of recent runs. Save each schedule with its own Save button.</p>
      <div id="scheds" style="margin-top:12px"></div>
    </div>
  </section>
  <!-- SETTINGS -->
  <section id="tab-settings" class="hidden">
    <div class="card">
      <h2>Storage</h2>
      <div class="row" style="margin-bottom:12px">
        <label>Backups root <span class="tag">(host folder mounted into the container)</span></label>
        <input type="text" id="setRoot" disabled style="min-width:280px">
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
let SCH_COLLAPSED=new Set();
let SCH_INIT=false;
let SCH_EXPLICIT=new Set();
let SCH_BASELINE={};
function schSnapshot(){SCH_BASELINE={};SCHEDS.forEach(s=>{SCH_BASELINE[s.id]=JSON.stringify(s)})}
function schDirty(s){return SCH_BASELINE[s.id]!==JSON.stringify(s)}
function schAnyDirty(){return SCHEDS.some(schDirty)}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
function fmtSize(b){if(!b)return"0 B";const u=["B","KB","MB","GB","TB"];let i=0;while(b>=1024&&i<u.length-1){b/=1024;i++}return b.toFixed(b<10&&i>0?1:0)+" "+u[i]}
function toast(m){const t=$("#toast");t.textContent=m;t.classList.add("show");clearTimeout(t._t);t._t=setTimeout(()=>t.classList.remove("show"),3500)}
async function api(path,opts){const r=await fetch(path,opts);let j={};try{j=await r.json()}catch(e){}if(!r.ok)throw new Error(j.error||j.message||r.status);return j}

$$("nav button").forEach(b=>b.onclick=()=>{
  const cur=$$("nav button").find(x=>x.classList.contains("active"));
  if(cur&&cur.dataset.tab==="schedules"&&b.dataset.tab!=="schedules"&&schAnyDirty()
     &&!confirm("You have unsaved schedule changes. Leave without saving?"))return;
  $$("nav button").forEach(x=>x.classList.remove("active"));b.classList.add("active");
  ["backup","restore","schedules","settings","logs"].forEach(t=>$("#tab-"+t).classList.toggle("hidden",t!==b.dataset.tab));
  if(b.dataset.tab==="logs")loadLogs();
});

/* ---------------- Backup ---------------- */
function renderContainers(){
  const tb=$("#ctn");tb.innerHTML="";
  if(!CT.length){tb.innerHTML='<tr><td colspan=6 class=muted>No containers found.</td></tr>';return}
  const groups={};CT.forEach(c=>{(groups[c.project]=groups[c.project]||[]).push(c)});
  Object.keys(groups).sort().forEach(proj=>{
    const list=groups[proj];
    const running=list.filter(c=>c.state==="running").length;
    const label=proj==="_standalone"?"(standalone)":proj;
    const gid="g_"+proj.replace(/[^A-Za-z0-9]/g,"_");
    const gh=document.createElement("tr");gh.className="grp";
    gh.innerHTML=`<td><input type=checkbox title="Select stack" onclick="toggleGroup('${gid}',this)"></td>
      <td colspan=4><span class=caret onclick="toggleCollapse('${gid}',this)">\u25b8</span>
        <b>\uD83D\uDCE6 ${esc(label)}</b>
        <span class=tag>&nbsp;${list.length} container${list.length>1?"s":""} \u00b7 ${running} running</span></td>
      <td class=right><button class="btn sm" onclick="backupProject('${esc(proj)}')">Back up stack</button></td>`;
    tb.appendChild(gh);
    list.forEach(c=>{
      const tr=document.createElement("tr");tr.className="member hidden "+gid;
      tr.innerHTML=`<td style="padding-left:30px"><input type=checkbox class="csel ${gid}" value="${esc(c.name)}"></td>
        <td><b>${esc(c.name)}</b></td><td class=muted>${esc(c.project)}</td>
        <td class=tag>${esc(c.image)}</td>
        <td><span class="dot ${c.state}"></span>${c.state}</td>
        <td><button class="btn sm" onclick="backupOne('${esc(c.name)}')">Back up</button></td>`;
      tb.appendChild(tr);
    });
  });
}
function toggleAll(cb){$$(".csel").forEach(x=>x.checked=cb.checked)}
function toggleGroup(gid,cb){$$(".csel."+gid).forEach(x=>x.checked=cb.checked)}
function toggleCollapse(gid,el){const hide=el.textContent==="\u25be";$$("tr.member."+gid).forEach(r=>r.classList.toggle("hidden",hide));el.textContent=hide?"\u25b8":"\u25be"}
function backupProject(proj){const names=CT.filter(c=>c.project===proj).map(c=>c.name);if(!names.length)return;backup(names)}
function selected(){return $$(".csel").filter(x=>x.checked).map(x=>x.value)}
async function backup(names){
  try{await api("/api/backup",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({containers:names})});
    toast("Backup started: "+(names.length?names.join(", "):"all running"));setTimeout(refresh,800);
  }catch(e){toast("Error: "+e.message)}
}
const backupSelected=()=>{const s=selected();if(!s.length)return toast("Select at least one container");backup(s)};
const backupAll=()=>backup([]);
const backupOne=n=>backup([n]);

/* ---------------- Restore ---------------- */
function renderRuns(runs){
  const el=$("#runs");el.innerHTML="";
  if(!runs.length){el.innerHTML='<p class=muted>No backup runs found under the root yet.</p>';return}
  runs.forEach((r,i)=>{
    const det=document.createElement("details");
    const when=r.mtime?new Date(r.mtime*1000).toLocaleString():"";
    const rows=r.projects.map(p=>`<tr>
      <td><b>${esc(p.name)}</b><div class=tag>${esc(p.containers.join(", "))}</div></td>
      <td>${fmtSize(p.size)}</td>
      <td class=right>
        <button class="btn sm" onclick="restore('${esc(r.run)}','${esc(p.name)}')">Restore project</button>
      </td></tr>`).join("");
    det.innerHTML=`<summary>${esc(r.label)} &nbsp;<span class=tag>${fmtSize(r.size)} · ${when}</span>
      <button class="btn sm" style="margin-left:10px" onclick="event.preventDefault();restore('${esc(r.run)}',null)">Restore whole run</button>
      <button class="btn sec sm danger" style="margin-left:6px" onclick="event.preventDefault();del('${esc(r.run)}')">Delete</button></summary>
      <table style="margin-top:8px">${rows||'<tr><td class=muted>empty</td></tr>'}</table>`;
    el.appendChild(det);
  });
}
async function restore(run,project){
  const what=project?`project "${project}"`:"the whole run";
  if(!confirm(`Restore ${what} from\n${run}?\n\nThis stops the affected stack(s), overwrites their volumes with the backup, starts the containers again, and re-imports databases where needed. Current data is replaced.`))return;
  try{await api("/api/restore",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({run,project})});toast("Restore started");setTimeout(refresh,800);
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
    const allSel=!SCH_EXPLICIT.has(s.id)&&(!s.containers||s.containers.length===0);
    const groups={};CT.forEach(c=>{(groups[c.project]=groups[c.project]||[]).push(c)});
    const opts=Object.keys(groups).sort().map(proj=>{
      const chips=groups[proj].map(c=>`<label class="chip ${(!allSel&&s.containers.includes(c.name))?'on':''}">
        <input type=checkbox ${(!allSel&&s.containers.includes(c.name))?'checked':''}
          onchange="schCtn(${idx},'${esc(c.name)}',this.checked)"> ${esc(c.name)}</label>`).join(" ");
      const projNames=groups[proj].map(c=>c.name);
      const projSel=!allSel&&projNames.every(n=>s.containers.includes(n));
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
    const ctnLabel=allSel?'all running':`${s.containers.length} container${s.containers.length===1?'':'s'}`;
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
        <div class="row" style="margin-bottom:6px"><div class=tag>Containers</div>
          <label class="chip ${allSel?'on':''}"><input type=checkbox ${allSel?'checked':''} onchange="schAll(${idx},this.checked)"> All running</label></div>
        <div class="row" ${allSel?'style="opacity:.45;pointer-events:none"':''}>${opts||'<span class=muted>no containers</span>'}</div>
      </div>
      </div>`;
    el.appendChild(div);
  });
}
function schToggle(id,ev){if(ev)ev.stopPropagation();
  if(SCH_COLLAPSED.has(id))SCH_COLLAPSED.delete(id);else SCH_COLLAPSED.add(id);renderSchedules()}
function schAll(idx,on){const s=SCHEDS[idx];if(on)SCH_EXPLICIT.delete(s.id);else SCH_EXPLICIT.add(s.id);s.containers=[];renderSchedules()}
function schProj(idx,proj,on){const s=SCHEDS[idx];const projNames=CT.filter(c=>c.project===proj).map(c=>c.name);
  SCH_EXPLICIT.add(s.id);let set=new Set(s.containers||[]);
  projNames.forEach(n=>{if(on)set.add(n);else set.delete(n)});s.containers=[...set];renderSchedules()}
function schCtn(idx,name,on){const s=SCHEDS[idx];SCH_EXPLICIT.add(s.id);let set=new Set(s.containers||[]);
  if(on)set.add(name);else set.delete(name);s.containers=[...set];renderSchedules()}
function schWd(idx,day,on){const s=SCHEDS[idx];let set=new Set(s.weekdays);if(on)set.add(day);else set.delete(day);s.weekdays=[...set].sort((a,b)=>a-b);renderSchedules()}
function addSchedule(){SCHEDS.push({id:"sched-"+Date.now().toString(36),name:"New backup",enabled:true,
  frequency:"daily",time:"03:00",weekdays:[0,1,2,3,4,5,6],day_of_month:1,containers:[],retention:7});renderSchedules()}
function rmSchedule(idx){
  if(schDirty(SCHEDS[idx])&&!confirm("This schedule has unsaved changes. Remove it anyway?"))return;
  SCHEDS.splice(idx,1);renderSchedules()}
async function saveSchedule(idx){
  try{const r=await api("/api/schedules",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({schedules:SCHEDS})});SCHEDS=r.schedules;schSnapshot();renderSchedules();
    toast("Schedule saved ✔");
  }catch(e){toast("Error: "+e.message)}
}

/* ---------------- Settings ---------------- */
function renderSettings(){
  $("#setRoot").value=SETTINGS.root||"";
  $("#setDest").value=SETTINGS.destination||"";
  $("#setManualRet").value=SETTINGS.manual_retention||10;
}
async function saveSettings(){
  const body={destination:$("#setDest").value,manual_retention:parseInt($("#setManualRet").value)||10};
  try{const r=await api("/api/settings",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify(body)});SETTINGS=Object.assign(SETTINGS,r.settings);renderSettings();
    $("#setSaved").textContent="saved ✔";setTimeout(()=>$("#setSaved").textContent="",2500);
  }catch(e){toast("Error: "+e.message)}
}

/* ---------------- common ---------------- */
function setStatus(s){
  const b=$("#statusBadge");
  b.textContent=s.busy?("⏳ "+(s.current||"working…")):"idle";
  b.className="badge "+(s.busy?"busy":"idle");
  $("#lastResult").textContent=s.last_result||"";
  ["btnBackupSel","btnBackupAll"].forEach(id=>{const e=$("#"+id);if(e)e.disabled=s.busy});
}
async function refresh(){
  try{const s=await api("/api/state");
    CT=s.containers;SCHEDS=s.schedules;WD=s.weekdayNames||WD;
    if(!SCH_INIT){SCHEDS.forEach(x=>SCH_COLLAPSED.add(x.id));SCH_INIT=true;}
    schSnapshot();
    SETTINGS=Object.assign({},s.settings,{root:s.root});
    $("#rootTag").textContent="root: "+s.root;
    renderContainers();renderRuns(s.runs);renderSchedules();renderSettings();setStatus(s);
  }catch(e){toast("Error: "+e.message)}
}
async function loadLogs(){
  try{const r=await api("/api/logs");const el=$("#log");
    el.textContent=r.lines.join("")||"(empty)";el.scrollTop=el.scrollHeight;
  }catch(e){}
}
window.addEventListener("beforeunload",e=>{if(schAnyDirty()){e.preventDefault();e.returnValue=""}});
setInterval(()=>{$("#clock").textContent=new Date().toLocaleTimeString()},1000);
setInterval(async()=>{try{setStatus(await api("/api/state"))}catch(e){}},4000);
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
