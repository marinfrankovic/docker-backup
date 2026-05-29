#!/usr/bin/env python3
"""Local web GUI + scheduler for the Docker backup tool.

Runs inside the docker-backup container. Serves a single-page UI and a small
JSON API that drives backup.sh / restore.sh, manages a daily schedule, lists
and deletes backups, and streams an activity log. No external dependencies.
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
BACKUP_ROOT = os.environ.get("BACKUP_ROOT_CONTAINER", "/backups")
SELF_NAME = os.environ.get("SELF_NAME", "docker-backup")
DEFAULT_RETENTION = int(os.environ.get("RETENTION_DAYS", "7"))
DEFAULT_HOUR = int(os.environ.get("BACKUP_HOUR", "3"))

CONFIG_DIR = os.path.join(BACKUP_ROOT, "_config")
CONFIG_PATH = os.path.join(CONFIG_DIR, "schedule.json")
LOG_DIR = os.path.join(BACKUP_ROOT, "_logs")
LOG_PATH = os.path.join(LOG_DIR, "activity.log")

DAY_RE = re.compile(r"^20\d\d-\d\d-\d\d$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")

_run_lock = threading.Lock()      # only one backup/restore at a time
_log_lock = threading.Lock()
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


def load_config():
    cfg = {"enabled": True, "times": [f"{DEFAULT_HOUR:02d}:00"],
           "retention_days": DEFAULT_RETENTION}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data.get("enabled"), bool):
            cfg["enabled"] = data["enabled"]
        if isinstance(data.get("times"), list):
            cfg["times"] = [t for t in data["times"] if TIME_RE.match(str(t))]
        if isinstance(data.get("retention_days"), int) and data["retention_days"] > 0:
            cfg["retention_days"] = data["retention_days"]
    except FileNotFoundError:
        save_config(cfg)
    except Exception as exc:  # noqa: BLE001
        log(f"WARN could not read config: {exc}")
    if not cfg["times"]:
        cfg["times"] = [f"{DEFAULT_HOUR:02d}:00"]
    return cfg


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)


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


def dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def list_backups():
    days = []
    if not os.path.isdir(BACKUP_ROOT):
        return days
    for day in sorted(os.listdir(BACKUP_ROOT), reverse=True):
        dpath = os.path.join(BACKUP_ROOT, day)
        if not (DAY_RE.match(day) and os.path.isdir(dpath)):
            continue
        projects = []
        for proj in sorted(os.listdir(dpath)):
            ppath = os.path.join(dpath, proj)
            if not os.path.isdir(ppath):
                continue
            containers = sorted(c for c in os.listdir(ppath)
                                if os.path.isdir(os.path.join(ppath, c)))
            projects.append({"name": proj, "containers": containers,
                             "size": dir_size(ppath)})
        complete = os.path.isfile(os.path.join(dpath, "_BACKUP_OK.txt"))
        days.append({"day": day, "projects": projects,
                     "size": dir_size(dpath), "complete": complete})
    return days


def do_backup(names):
    cfg = load_config()
    env = dict(os.environ)
    env["RETENTION_DAYS"] = str(cfg["retention_days"])
    label = "all running" if not names else ", ".join(names)
    with _run_lock:
        STATE["busy"] = True
        STATE["current"] = f"backup: {label}"
        log(f"Backup started ({label})")
        rc, out = run(["/usr/local/bin/backup.sh", *names], timeout=3600)
        for ln in out.splitlines():
            log("  " + ln)
        STATE["busy"] = False
        STATE["current"] = ""
        STATE["last_result"] = f"backup ({label}) rc={rc} @ {now()}"
        log(f"Backup finished ({label}) rc={rc}")
    return rc, out


def do_restore(project, day):
    if not NAME_RE.match(project):
        return 1, "invalid project name"
    if day != "latest" and not DAY_RE.match(day):
        return 1, "invalid day"
    with _run_lock:
        STATE["busy"] = True
        STATE["current"] = f"restore: {project} ({day})"
        log(f"Restore started ({project} {day})")
        rc, out = run(["/usr/local/bin/restore.sh", project, day], timeout=3600)
        for ln in out.splitlines():
            log("  " + ln)
        STATE["busy"] = False
        STATE["current"] = ""
        STATE["last_result"] = f"restore ({project} {day}) rc={rc} @ {now()}"
        log(f"Restore finished ({project} {day}) rc={rc}")
    return rc, out


def delete_backup(day, project=None):
    if not DAY_RE.match(day):
        return 1, "invalid day"
    target = os.path.join(BACKUP_ROOT, day)
    if project:
        if not NAME_RE.match(project):
            return 1, "invalid project name"
        target = os.path.join(target, project)
    # confine to BACKUP_ROOT
    if os.path.commonpath([os.path.realpath(target), os.path.realpath(BACKUP_ROOT)]) \
            != os.path.realpath(BACKUP_ROOT):
        return 1, "path outside backup root"
    if not os.path.exists(target):
        return 1, "not found"
    shutil.rmtree(target, ignore_errors=True)
    log(f"Deleted backup: {day}" + (f"/{project}" if project else ""))
    return 0, "deleted"


def read_log_tail(n=300):
    try:
        with open(LOG_PATH, encoding="utf-8") as fh:
            return fh.readlines()[-n:]
    except FileNotFoundError:
        return []


# --------------------------------------------------------------------------- #
# scheduler
# --------------------------------------------------------------------------- #
def scheduler_loop():
    last_fired = {}  # "HH:MM" -> "YYYY-MM-DD"
    log("Scheduler thread started")
    while True:
        try:
            cfg = load_config()
            if cfg["enabled"]:
                hm = datetime.now().strftime("%H:%M")
                today = datetime.now().strftime("%Y-%m-%d")
                if hm in cfg["times"] and last_fired.get(hm) != today:
                    last_fired[hm] = today
                    if not STATE["busy"]:
                        log(f"Scheduled backup triggered at {hm}")
                        threading.Thread(target=do_backup, args=([],),
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

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(200, HTML, "text/html; charset=utf-8")
        elif path == "/api/state":
            self._json({
                "containers": list_containers(),
                "backups": list_backups(),
                "schedule": load_config(),
                "busy": STATE["busy"],
                "current": STATE["current"],
                "last_result": STATE["last_result"],
            })
        elif path == "/api/containers":
            self._json(list_containers())
        elif path == "/api/backups":
            self._json(list_backups())
        elif path == "/api/schedule":
            self._json(load_config())
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
            threading.Thread(target=do_backup, args=(names,), daemon=True).start()
            self._json({"ok": True, "started": names or "all running"})
        elif path == "/api/restore":
            if STATE["busy"]:
                return self._json({"error": "busy", "current": STATE["current"]}, 409)
            project = str(body.get("project", ""))
            day = str(body.get("day", "latest"))
            threading.Thread(target=do_restore, args=(project, day), daemon=True).start()
            self._json({"ok": True, "started": f"{project} ({day})"})
        elif path == "/api/delete":
            rc, msg = delete_backup(str(body.get("day", "")),
                                    body.get("project") or None)
            self._json({"ok": rc == 0, "message": msg}, 200 if rc == 0 else 400)
        elif path == "/api/schedule":
            cfg = load_config()
            if isinstance(body.get("enabled"), bool):
                cfg["enabled"] = body["enabled"]
            if isinstance(body.get("times"), list):
                times = sorted({t for t in (str(x).strip() for x in body["times"])
                                if TIME_RE.match(t)})
                cfg["times"] = times or cfg["times"]
            rd = body.get("retention_days")
            if isinstance(rd, int) and 1 <= rd <= 365:
                cfg["retention_days"] = rd
            save_config(cfg)
            log(f"Schedule updated: {cfg}")
            self._json({"ok": True, "schedule": cfg})
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
nav{display:flex;gap:4px;padding:10px 20px 0;background:var(--panel);border-bottom:1px solid var(--line)}
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
.right{margin-left:auto}input[type=text],input[type=number]{background:var(--panel2);border:1px solid var(--line);
color:var(--fg);padding:7px 10px;border-radius:7px;font-size:13px}
input[type=checkbox]{width:16px;height:16px;accent-color:var(--accent)}
.chip{background:var(--panel2);border:1px solid var(--line);border-radius:20px;padding:4px 10px;
display:inline-flex;align-items:center;gap:8px;font-size:13px}
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
  <button data-tab="schedule">Schedule</button>
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
      <h2>Backups on disk</h2>
      <div id="backups"></div>
    </div>
  </section>
  <!-- SCHEDULE -->
  <section id="tab-schedule" class="hidden">
    <div class="card">
      <h2>Daily schedule</h2>
      <div class="row" style="margin-bottom:14px">
        <label class="row"><input type="checkbox" id="schEnabled"> Enabled</label>
      </div>
      <div style="margin-bottom:14px">
        <div class="muted" style="margin-bottom:6px">Run times (local time)</div>
        <div id="timeChips" class="row" style="margin-bottom:8px"></div>
        <div class="row">
          <input type="text" id="newTime" placeholder="HH:MM e.g. 03:00" style="width:160px">
          <button class="btn sec sm" onclick="addTime()">+ Add time</button>
        </div>
      </div>
      <div class="row" style="margin-bottom:16px">
        <label>Keep last
          <input type="number" id="schRetention" min="1" max="365" style="width:70px">
          daily backups (older are deleted)</label>
      </div>
      <button class="btn" onclick="saveSchedule()">Save schedule</button>
      <span id="schSaved" class="tag" style="margin-left:10px"></span>
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
let CT=[], SCHED={times:[]};
function fmtSize(b){if(!b)return"0 B";const u=["B","KB","MB","GB","TB"];let i=0;while(b>=1024&&i<u.length-1){b/=1024;i++}return b.toFixed(b<10&&i>0?1:0)+" "+u[i]}
function toast(m){const t=$("#toast");t.textContent=m;t.classList.add("show");clearTimeout(t._t);t._t=setTimeout(()=>t.classList.remove("show"),3500)}
async function api(path,opts){const r=await fetch(path,opts);let j={};try{j=await r.json()}catch(e){}if(!r.ok)throw new Error(j.error||j.message||r.status);return j}

$$("nav button").forEach(b=>b.onclick=()=>{
  $$("nav button").forEach(x=>x.classList.remove("active"));b.classList.add("active");
  ["backup","restore","schedule","logs"].forEach(t=>$("#tab-"+t).classList.toggle("hidden",t!==b.dataset.tab));
  if(b.dataset.tab==="logs")loadLogs();
});

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
      <td colspan=4><span class=caret onclick="toggleCollapse('${gid}',this)">\u25be</span>
        <b>\uD83D\uDCE6 ${label}</b>
        <span class=tag>&nbsp;${list.length} container${list.length>1?"s":""} \u00b7 ${running} running</span></td>
      <td class=right><button class="btn sm" onclick="backupProject('${proj}')">Back up stack</button></td>`;
    tb.appendChild(gh);
    list.forEach(c=>{
      const tr=document.createElement("tr");tr.className="member "+gid;
      tr.innerHTML=`<td style="padding-left:30px"><input type=checkbox class="csel ${gid}" value="${c.name}"></td>
        <td><b>${c.name}</b></td><td class=muted>${c.project}</td>
        <td class=tag>${c.image}</td>
        <td><span class="dot ${c.state}"></span>${c.state}</td>
        <td><button class="btn sm" onclick="backupOne('${c.name}')">Back up</button></td>`;
      tb.appendChild(tr);
    });
  });
}
function renderBackups(days){
  const el=$("#backups");el.innerHTML="";
  if(!days.length){el.innerHTML='<p class=muted>No backups yet.</p>';return}
  days.forEach(d=>{
    const det=document.createElement("details");det.open=days[0]===d;
    let rows=d.projects.map(p=>`<tr>
      <td><b>${p.name}</b><div class=tag>${p.containers.join(", ")}</div></td>
      <td>${fmtSize(p.size)}</td>
      <td class=right>
        <button class="btn sm" onclick="restore('${p.name}','${d.day}')">Restore</button>
        <button class="btn sec sm danger" onclick="del('${d.day}','${p.name}')">Delete</button>
      </td></tr>`).join("");
    det.innerHTML=`<summary>${d.day} &nbsp;<span class=tag>${fmtSize(d.size)} · ${d.complete?"✔ complete":"⚠ partial"}</span>
      <button class="btn sec sm danger" style="margin-left:10px" onclick="event.preventDefault();del('${d.day}',null)">Delete day</button></summary>
      <table style="margin-top:8px">${rows||'<tr><td class=muted>empty</td></tr>'}</table>`;
    el.appendChild(det);
  });
}
function renderSchedule(){
  $("#schEnabled").checked=!!SCHED.enabled;
  $("#schRetention").value=SCHED.retention_days||7;
  const c=$("#timeChips");c.innerHTML="";
  (SCHED.times||[]).forEach(t=>{
    const s=document.createElement("span");s.className="chip";
    s.innerHTML=`${t} <button onclick="rmTime('${t}')">×</button>`;c.appendChild(s);
  });
  if(!(SCHED.times||[]).length)c.innerHTML='<span class=muted>no times set</span>';
}

function setStatus(s){
  const b=$("#statusBadge");
  b.textContent=s.busy?("⏳ "+(s.current||"working…")):"idle";
  b.className="badge "+(s.busy?"busy":"idle");
  $("#lastResult").textContent=s.last_result||"";
  ["btnBackupSel","btnBackupAll"].forEach(id=>$("#"+id).disabled=s.busy);
}

async function refresh(){
  try{const s=await api("/api/state");CT=s.containers;SCHED=s.schedule;
    renderContainers();renderBackups(s.backups);renderSchedule();setStatus(s);
  }catch(e){toast("Error: "+e.message)}
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

async function restore(project,day){
  if(!confirm(`Restore "${project}" from ${day}?\n\nThis runs end-to-end: it stops the stack, overwrites its volumes with the backup, starts the containers again, and re-imports the database where needed. Current data is replaced.`))return;
  try{await api("/api/restore",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({project,day})});toast("Restore started: "+project);setTimeout(refresh,800);
  }catch(e){toast("Error: "+e.message)}
}
async function del(day,project){
  if(!confirm(`Delete backup ${day}${project?"/"+project:""}? This cannot be undone.`))return;
  try{await api("/api/delete",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({day,project})});toast("Deleted");refresh();
  }catch(e){toast("Error: "+e.message)}
}

function addTime(){const v=$("#newTime").value.trim();if(!/^([01]?\d|2[0-3]):[0-5]\d$/.test(v))return toast("Use HH:MM");
  SCHED.times=[...new Set([...(SCHED.times||[]),v])].sort();$("#newTime").value="";renderSchedule()}
function rmTime(t){SCHED.times=(SCHED.times||[]).filter(x=>x!==t);renderSchedule()}
async function saveSchedule(){
  const body={enabled:$("#schEnabled").checked,times:SCHED.times||[],
    retention_days:parseInt($("#schRetention").value)||7};
  try{const r=await api("/api/schedule",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify(body)});SCHED=r.schedule;renderSchedule();
    $("#schSaved").textContent="saved ✔";setTimeout(()=>$("#schSaved").textContent="",2500);
  }catch(e){toast("Error: "+e.message)}
}

async function loadLogs(){
  try{const r=await api("/api/logs");const el=$("#log");
    el.textContent=r.lines.join("")||"(empty)";el.scrollTop=el.scrollHeight;
  }catch(e){}
}
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
    load_config()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    log(f"Docker Backup GUI listening on :{PORT} (retention {DEFAULT_RETENTION}d)")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
