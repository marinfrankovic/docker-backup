/* Presentation-only additions. Backup, restore and schedule requests stay in app.py. */
"use strict";
(() => {
  const icons = {
    overview: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="11" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="18" width="7" height="3" rx="1"/>',
    docker: '<path d="M3 8h18v10H3zM7 8V4h4v4m2 0V4h4v4M3 13h18M9 8v10m6-10v10"/>',
    restore: '<path d="M3 10a9 9 0 1 1 2 8M3 4v6h6M12 7v5l3 2"/>',
    schedules: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4m10-4v4M3 11h18M8 15h2m4 0h2"/>',
    settings: '<path d="M3 7h18M3 17h18"/><circle cx="8" cy="7" r="3"/><circle cx="16" cy="17" r="3"/>',
    logs: '<path d="M5 3h14v18H5zM9 8h6m-6 4h6m-6 4h4"/>',
    statistics: '<path d="M3 3v18h18M7 16v-4m5 4V8m5 8V5"/>',
    alert: '<path d="m12 3 10 18H2zM12 9v5m0 3h.01"/>',
    shield: '<path d="m12 3-8 3v6c0 5 8 9 8 9s8-4 8-9V6z"/><path d="m8 12 3 3 5-6"/>'
  };
  const pages = {
    overview: ["Docker workspace", "Backup command center", "Your container backups, schedules and recovery inventory in one place."],
    backup: ["Workloads / Docker", "Docker backups", "Choose stacks, containers and individual mounts using your existing backup controls."],
    restore: ["Recovery / Docker", "Restore points", "Existing backups, discovered directly from your current backup root."],
    schedules: ["Automation / Schedules", "Backup schedules", "Your existing jobs, scopes, mount policies and retention. Nothing is saved automatically."],
    settings: ["Configuration / Workspace", "Settings", "Your existing storage configuration and manual-backup retention."],
    logs: ["Observability / Activity", "Activity log", "Live activity from the existing backup and restore engine."],
    statistics: ["Insights / Statistics", "See the shape of your backups.", "Visualizations derived from actual retained backup files."]
  };
  const svg = name => `<svg class="cc-icon" viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.shield}</svg>`;
  const status = (text, type = "") => `<span class="cc-status ${type}">${esc(text)}</span>`;
  const metric = (label, value, note) => `<article class="cc-metric"><span>${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(note)}</small></article>`;
  const note = (text, info = false) => `<div class="notice${info ? " info" : ""}">${svg(info ? "shield" : "alert")}<div>${text}</div></div>`;
  const link = (label, tab) => `<button class="cc-link" data-open-tab="${tab}">${esc(label)}<span>&rarr;</span></button>`;
  const kv = (key, value) => `<div class="cc-kv"><span>${esc(key)}</span><span>${esc(value)}</span></div>`;
  const activeTab = () => document.querySelector("nav button[data-tab].active")?.dataset.tab || "overview";
  const bytes = value => Number.isFinite(value) && value >= 0 ? value : 0;
  let current = null;
  let signature = "";
  let logLines = [];
  const runs = () => current?.runs || [];
  const totalSize = list => list.reduce((sum, run) => sum + bytes(run.size), 0);
  const projects = list => {
    const totals = new Map();
    list.forEach(run => (run.projects || []).forEach(project => {
      totals.set(project.name, (totals.get(project.name) || 0) + bytes(project.size));
    }));
    return [...totals].map(([name, size]) => ({name, size})).sort((a, b) => b.size - a.size || a.name.localeCompare(b.name));
  };
  function pageHeader(tab) {
    const [eyebrow, title, subtitle] = pages[tab];
    document.getElementById("pageEyebrow").textContent = eyebrow;
    document.getElementById("pageTitle").textContent = title;
    document.getElementById("pageSubtitle").textContent = subtitle;
    document.getElementById("pageCrumb").textContent = tab === "overview" ? "Overview" : title;
    document.title = (tab === "overview" ? "Overview" : title) + " / Docker Backup";
    document.getElementById("pageActions").innerHTML = tab === "overview"
      ? '<button class="btn" data-open-tab="backup">Choose a backup scope</button>' : "";
    if (tab === "overview") renderOverview();
    if (tab === "statistics") renderStatistics();
  }
  function renderOverview() {
    if (!current) return;
    const containers = current.containers || [];
    const schedules = current.schedules || [];
    const enabled = schedules.filter(schedule => schedule.enabled);
    const latest = [...runs()].sort((a, b) => b.mtime - a.mtime).slice(0, 5);
    const names = new Set(containers.map(container => container.project));
    document.getElementById("overviewContent").innerHTML =
      `<div class="cc-metrics">${metric("Discovered containers", containers.length, `${names.size} stacks on this host`)}${metric("Completed restore points", runs().length, "Discovered completion markers")}${metric("Enabled schedules", enabled.length, `${schedules.length} saved jobs / not modified`)}${metric("Retained backup bytes", fmtSize(totalSize(runs())), "Compressed files currently on disk")}</div>` +
      note("<b>Recovery evidence, not assumptions.</b> A completed backup marker records a successful run. It does not prove a tested restore. Verify recovery separately.", true) +
      `<div class="cc-split"><div class="cc-stack"><section class="card"><div class="cc-head"><h2>Protection overview</h2>${status(current.busy ? "Job running" : "Idle", current.busy ? "warn" : "good")}</div><div class="table-scroll"><table><thead><tr><th>Workload</th><th>Evidence</th><th>Action</th></tr></thead><tbody><tr><td><b>Docker stacks</b><div class="tag">${containers.length} discovered containers</div></td><td>${runs().length ? status("Completed runs on disk", "good") : status("No completed runs", "warn")}</td><td><button class="btn sec sm" data-open-tab="backup">Open</button></td></tr></tbody></table></div></section>
      <section class="card"><div class="cc-head"><h2>Latest completed restore points</h2>${status("From disk")}</div>${latest.length ? `<table><thead><tr><th>Completed</th><th>Bucket</th><th>Size</th></tr></thead><tbody>${latest.map(run => `<tr><td>${esc(fmtDate(run.mtime))}<div class="tag">${esc(fmtTime(run.mtime))}</div></td><td>${esc(run.bucket)}</td><td>${esc(fmtSize(run.size))}</td></tr>`).join("")}</tbody></table>` : '<p class="cc-caption">No completed backup folders have been discovered.</p>'}${link("Inspect all restore points", "restore")}</section></div>
      <aside class="cc-stack"><section class="card"><h2>Current job result</h2><p class="cc-caption cc-mono">${esc(current.last_result || "No job result recorded in this process.")}</p><hr class="cc-rule">${kv("Engine", current.busy ? "Running" : "Idle")}${kv("Backup root", current.root)}${kv("Manual retention", `${current.settings.manual_retention} runs`)}${link("Storage settings", "settings")}</section>
      <section class="card"><h2>Saved automation</h2>${enabled.length ? enabled.map(schedule => kv(schedule.name, `${schedule.frequency} / ${schedule.time}`)).join("") : '<p class="cc-caption">No schedules are enabled.</p>'}<p class="cc-caption">Times use the server timezone. Open Schedules for weekday and monthly rules.</p>${link("Review existing schedules", "schedules")}</section><section class="card"><h2>Inspect your backup footprint</h2><p class="cc-caption">Size trends, daily run counts and project composition, calculated from retained files.</p>${link("View statistics", "statistics")}</section></aside></div>`;
  }
  function filterContainers() {
    const query = document.getElementById("containerSearch").value.trim().toLowerCase();
    const rows = [...document.querySelectorAll("#ctn tr")];
    let showGroup = true;
    rows.forEach(row => {
      if (row.classList.contains("grp")) {
        showGroup = row.textContent.toLowerCase().includes(query);
        const siblings = [];
        let next = row.nextElementSibling;
        while (next && !next.classList.contains("grp")) { siblings.push(next); next = next.nextElementSibling; }
        row.classList.toggle("cc-filtered", !!query && !showGroup && !siblings.some(member => member.querySelector(".csel")?.value.toLowerCase().includes(query)));
      } else {
        const container = row.querySelector(".csel")?.value || row.querySelector(".bkm")?.dataset.c || "";
        row.classList.toggle("cc-filtered", !!query && !showGroup && !container.toLowerCase().includes(query));
      }
    });
  }
  function filterLogs() {
    const query = document.getElementById("logFilter").value.toLowerCase();
    const shown = logLines.filter(line => line.toLowerCase().includes(query));
    document.getElementById("log").textContent = shown.join("") || (query ? "(no matching entries)" : "(empty)");
  }
  function chart(values, labels, title, bars = false, unit = "GiB") {
    const left = 48, width = 500, top = 20, height = 145, base = top + height;
    const maximum = Math.max(...values, 0) || 1;
    const step = Math.max(1, Math.ceil(maximum / 3));
    const scale = bars ? step * Math.ceil(maximum / step) : maximum * 1.12;
    const x = index => values.length === 1 ? left + width / 2 : left + index * width / (values.length - 1);
    const points = values.map((value, index) => [x(index), base - value / scale * height]);
    const tickValues = bars ? Array.from({length: scale / step + 1}, (_, index) => index * step)
      : [0, scale / 3, scale * 2 / 3, scale];
    const ticks = tickValues.map(value => {
      const y = base - value / scale * height;
      return `<line class="gridline" x1="${left}" y1="${y}" x2="${left + width}" y2="${y}"/><text x="${left - 7}" y="${y + 3}" text-anchor="end">${bars ? Math.round(value) : value.toFixed(1)}</text>`;
    }).join("");
    const marks = bars ? values.map((value, index) => {
      const barWidth = Math.min(36, width / values.length * .6);
      const cx = left + (index + .5) * width / values.length;
      return `<rect class="chart-bar" x="${cx - barWidth / 2}" y="${base - value / scale * height}" width="${barWidth}" height="${value / scale * height}" rx="3"><title>${esc(labels[index])}: ${value} completed runs</title></rect>`;
    }).join("") : `<path class="area" d="M${points[0][0]},${base} L${points.map(point => point.join(",")).join(" L")} L${points.at(-1)[0]},${base} Z"/><path class="trend" d="M${points.map(point => point.join(",")).join(" L")}"/>${points.map(([cx, cy], index) => `<circle class="point" cx="${cx}" cy="${cy}" r="3.5"><title>${esc(labels[index])}: ${values[index].toFixed(3)} ${unit}</title></circle>`).join("")}`;
    const stride = Math.max(1, Math.ceil(labels.length / 7));
    const axis = labels.map((label, index) => index % stride === 0 || index === labels.length - 1 ? `<text x="${bars ? left + (index + .5) * width / values.length : x(index)}" y="193" text-anchor="middle">${esc(label)}</text>` : "").join("");
    return `<svg class="cc-chart" role="img" aria-label="${esc(title)}" viewBox="0 0 570 210"><title>${esc(title)}</title><desc>${esc(labels.map((label, index) => `${label}: ${values[index].toFixed(bars ? 0 : 3)} ${bars ? "runs" : unit}`).join("; "))}</desc>${ticks}${marks}${axis}</svg>`;
  }
  function donut(items) {
    const colors = ["#aaa0ff", "#6f9bd4", "#64c8ac", "#e1b979", "#b981ac", "#7aa09b"];
    const total = items.reduce((sum, item) => sum + item.size, 0);
    if (!total) return '<div class="cc-empty">No project sizes available.</div>';
    const circumference = 2 * Math.PI * 66;
    let offset = 0;
    const segments = items.map((item, index) => {
      const length = item.size / total * circumference;
      const segment = `<circle cx="90" cy="90" r="66" stroke="${colors[index % colors.length]}" stroke-dasharray="${length} ${circumference - length}" stroke-dashoffset="${-offset}" transform="rotate(-90 90 90)"><title>${esc(item.name)}: ${esc(fmtSize(item.size))}</title></circle>`;
      offset += length;
      return segment;
    }).join("");
    return `<div class="cc-donut-wrap"><svg class="cc-donut" viewBox="0 0 180 180" role="img" aria-label="Retained project byte composition"><title>Retained project byte composition</title>${segments}<text x="90" y="88">${esc(fmtSize(total))}</text><text class="donut-label" x="90" y="108">project bytes</text></svg><div class="cc-legend">${items.map((item, index) => `<div><span><i style="background:${colors[index % colors.length]}"></i>${esc(item.name)}</span><b>${esc(fmtSize(item.size))}</b></div>`).join("")}</div></div>`;
  }
  function renderStatistics() {
    if (!current) return;
    const select = document.getElementById("statsProject");
    const selectedProject = select.value;
    const projectNames = [...new Set(runs().flatMap(run => (run.projects || []).map(project => project.name)))].sort();
    select.innerHTML = '<option value="">All projects</option>' + projectNames.map(name => `<option value="${esc(name)}"${name === selectedProject ? " selected" : ""}>${esc(name)}</option>`).join("");
    const project = select.value;
    const days = Number(document.getElementById("statsPeriod").value);
    const cutoff = new Date();
    cutoff.setHours(0, 0, 0, 0);
    cutoff.setDate(cutoff.getDate() - Math.max(0, days - 1));
    const filtered = runs().filter(run => (!days || run.mtime * 1000 >= cutoff.getTime()) && (!project || (run.projects || []).some(item => item.name === project)));
    const selected = filtered.map(run => project ? {...run, size: run.projects.find(item => item.name === project).size, projects: run.projects.filter(item => item.name === project)} : run);
    const groups = projects(selected);
    const largest = [...selected].sort((a, b) => b.size - a.size)[0];
    const average = selected.length ? totalSize(selected) / selected.length : 0;
    let content = `<div class="cc-metrics">${metric("Retained complete runs", selected.length, "Within the selected period and project")}${metric("Retained bytes", fmtSize(totalSize(selected)), "Not unique or deduplicated data")}${metric("Mean run size", fmtSize(average), "Selected retained completed runs")}${metric("Largest run", largest ? fmtSize(largest.size) : "No data", largest ? fmtDate(largest.mtime) : "No completed runs in this range")}</div>` +
      note("<b>Retention changes the view.</b> These charts cover completed runs still present on disk, not all historical attempts. Failure rates, total capacity, restore-test outcomes and elapsed run durations are not recorded by the existing inventory and are not invented here.", true);
    if (!selected.length) {
      document.getElementById("statisticsContent").innerHTML = content + '<section class="card cc-empty"><h3>No retained backups match this range</h3><p>Try a wider period or a different project.</p></section>';
      return;
    }
    const daily = new Map();
    selected.forEach(run => {
      const date = new Date(run.mtime * 1000);
      const key = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
      const day = daily.get(key) || {bytes: 0, count: 0};
      day.bytes += bytes(run.size); day.count += 1; daily.set(key, day);
    });
    const ordered = [...daily].sort(([a], [b]) => a.localeCompare(b));
    const labels = ordered.map(([key]) => key.slice(5));
    const maxDaily = Math.max(...ordered.map(([, day]) => day.bytes));
    const unit = maxDaily >= 1073741824 ? {name: "GiB", divisor: 1073741824}
      : maxDaily >= 1048576 ? {name: "MiB", divisor: 1048576}
      : maxDaily >= 1024 ? {name: "KiB", divisor: 1024} : {name: "bytes", divisor: 1};
    const top = groups.slice(0, 5);
    if (groups.length > 5) top.push({name: "Other projects", size: groups.slice(5).reduce((sum, item) => sum + item.size, 0)});
    content += `<div class="cc-chart-grid"><section class="card"><div class="cc-head"><h2>Retained backup bytes by day</h2>${status(unit.name + " / compressed output")}</div>${chart(ordered.map(([, day]) => day.bytes / unit.divisor), labels, "Retained compressed backup bytes by completion day", false, unit.name)}<p class="cc-caption">Dates group completion-marker timestamps in your browser timezone. Only days containing retained runs are plotted.</p></section><section class="card"><div class="cc-head"><h2>Project composition</h2>${status("Selected retained runs")}</div>${donut(top)}<p class="cc-caption">Project bytes exclude root-level manifests and other overhead. They may not add up to total run bytes.</p></section></div>
      <div class="cc-chart-grid"><section class="card"><div class="cc-head"><h2>Completed runs by day</h2>${status("Count / not success rate")}</div>${chart(ordered.map(([, day]) => day.count), labels, "Retained completed run count by completion day", true)}</section><section class="card"><h2>What this inventory can prove</h2>${kv("Completion evidence", `${selected.length} completion markers`)}${kv("Attempt success rate", "Not recorded")}${kv("Elapsed run duration", "Not recorded")}${kv("Free destination capacity", "Not recorded")}${kv("Successful recovery test", "Not recorded")}<hr class="cc-rule"><p class="cc-caption">A completion marker is not a recovery test. Test a restore separately before relying on a backup.</p></section></div>
      <section class="card"><div class="cc-head"><h2>Project footprint</h2>${status("Aggregated retained bytes")}</div><table><thead><tr><th>Project</th><th>Retained bytes</th><th>Share of project bytes</th></tr></thead><tbody>${groups.map(item => `<tr><td><b>${esc(item.name)}</b></td><td>${esc(fmtSize(item.size))}</td><td>${groups.reduce((sum, group) => sum + group.size, 0) ? (item.size / groups.reduce((sum, group) => sum + group.size, 0) * 100).toFixed(1) : "0"}%</td></tr>`).join("")}</tbody></table></section>`;
    document.getElementById("statisticsContent").innerHTML = content;
  }
  window.addEventListener("backup:state", event => {
    const next = event.detail;
    current = next;
    document.getElementById("connectionNotice").classList.add("hidden");
    document.getElementById("workspaceHost").textContent = location.host;
    document.getElementById("workspaceCount").textContent = `${next.containers.length} containers / ${new Set(next.containers.map(container => container.project)).size} stacks`;
    document.getElementById("dataUpdated").textContent = "Last read " + new Date().toLocaleTimeString();
    const nextSignature = JSON.stringify([next.runs, next.schedules, next.containers, next.busy, next.last_result, next.settings]);
    if (signature !== nextSignature) {
      signature = nextSignature;
      if (activeTab() === "overview") renderOverview();
      if (activeTab() === "statistics") renderStatistics();
    }
  });
  window.addEventListener("backup:error", event => {
    const notice = document.getElementById("connectionNotice");
    notice.textContent = "Connection error: " + event.detail + ". Displayed inventory may be stale.";
    notice.classList.remove("hidden");
    document.getElementById("dataUpdated").textContent = "Connection lost / displayed data may be stale";
  });
  window.addEventListener("backup:tab", event => pageHeader(event.detail));
  window.addEventListener("backup:containers", filterContainers);
  window.addEventListener("backup:logs", event => { logLines = event.detail; });
  window.addEventListener("backup:log-rendered", filterLogs);
  document.addEventListener("click", event => {
    const tab = event.target.closest("[data-open-tab]");
    if (tab) switchTab(tab.dataset.openTab);
  });
  document.querySelector(".brand").addEventListener("click", event => { event.preventDefault(); switchTab("overview"); });
  document.getElementById("containerSearch").addEventListener("input", filterContainers);
  document.getElementById("logFilter").addEventListener("input", filterLogs);
  document.getElementById("statsPeriod").addEventListener("change", renderStatistics);
  document.getElementById("statsProject").addEventListener("change", renderStatistics);
  document.getElementById("downloadLog").onclick = () => {
    const url = URL.createObjectURL(new Blob([document.getElementById("log").textContent], {type: "text/plain"}));
    const anchor = document.createElement("a");
    anchor.href = url; anchor.download = "docker-backup-activity.log";
    document.body.append(anchor); anchor.click(); anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast("Log export requested. Check your browser downloads.");
  };
  document.querySelectorAll("[data-icon]").forEach(element => { element.innerHTML = svg(element.dataset.icon); });
  window.addEventListener("hashchange", () => {
    const tab = location.hash.slice(1);
    if (pages[tab] && tab !== activeTab()) switchTab(tab);
  });
  switchTab(pages[location.hash.slice(1)] ? location.hash.slice(1) : "overview");
})();
