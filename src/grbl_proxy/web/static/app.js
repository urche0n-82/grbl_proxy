// grbl-proxy dashboard — vanilla JS, no build tooling

const $ = (id) => document.getElementById(id);

let selectedFileStem = null;

// ---------------------------------------------------------------------------
// WebSocket
// ---------------------------------------------------------------------------

let ws = null;
let wsRetryTimer = null;

function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}/ws/status`;
  ws = new WebSocket(url);

  ws.onopen = () => {
    $("connection-indicator").className = "indicator online";
    clearTimeout(wsRetryTimer);
  };

  ws.onmessage = (evt) => {
    try {
      applySnapshot(JSON.parse(evt.data));
    } catch (e) {
      console.warn("Bad WS message", e);
    }
  };

  ws.onclose = ws.onerror = () => {
    $("connection-indicator").className = "indicator offline";
    wsRetryTimer = setTimeout(connectWS, 3000);
  };
}

// ---------------------------------------------------------------------------
// State rendering
// ---------------------------------------------------------------------------

function applySnapshot(s) {
  $("proxy-state").textContent = s.proxy_state ?? "—";
  $("grbl-state").textContent  = s.grbl_state  ?? "—";

  const serialEl = $("serial-connected");
  if (s.serial_connected != null) {
    serialEl.textContent = s.serial_connected ? "Connected" : "Disconnected";
    serialEl.className = "value " + (s.serial_connected ? "serial-ok" : "serial-off");
  } else {
    serialEl.textContent = "—";
    serialEl.className = "value";
  }

  if (s.mpos_x != null) {
    $("position").textContent =
      `${s.mpos_x.toFixed(3)},  ${s.mpos_y.toFixed(3)},  ${s.mpos_z.toFixed(3)}`;
  } else {
    $("position").textContent = "—";
  }

  if (s.feed != null) {
    $("feed-spindle").textContent = `${s.feed} mm/min  /  ${s.spindle ?? 0}`;
  } else {
    $("feed-spindle").textContent = "—";
  }

  // Job progress
  const pct = s.job_progress_pct ?? 0;
  $("progress-fill").style.width = pct + "%";
  $("progress-pct").textContent  = pct.toFixed(1) + "%";

  if (s.job_lines_sent != null && s.job_total_lines != null) {
    $("job-lines").textContent = `${s.job_lines_sent} / ${s.job_total_lines}`;
  } else {
    $("job-lines").textContent = "—";
  }

  if (s.job_elapsed_s != null) {
    $("job-elapsed").textContent = formatDuration(s.job_elapsed_s);
  } else {
    $("job-elapsed").textContent = "—";
  }

  // Swap Job controls based on state
  const executing = s.proxy_state === "Executing";
  const paused    = s.proxy_state === "Paused";
  const active    = executing || paused;
  updateJobControls(active, executing, paused);

  // Machine buttons — only usable when serial is connected and no job running
  const serialOk  = !!s.serial_connected;
  const alarm     = s.grbl_state === "Alarm";
  $("btn-home").disabled         = !serialOk || active;
  $("btn-cancel-alarm").disabled = !serialOk || !alarm;
}

// Delegated listener on #job-controls — survives innerHTML swaps
document.getElementById("job-controls").addEventListener("click", (e) => {
  const id = e.target.id;
  if (id === "btn-run")    runQueued();
  if (id === "btn-pause")  jobAction("pause");
  if (id === "btn-resume") jobAction("resume");
  if (id === "btn-cancel") jobAction("cancel");
});

function updateJobControls(active, executing, paused) {
  const row = $("job-controls");
  if (active) {
    // Show Pause / Resume / Cancel; hide Run
    row.innerHTML =
      `<button id="btn-pause"  class="btn btn-warning">Pause</button>` +
      `<button id="btn-resume" class="btn btn-success">Resume</button>` +
      `<button id="btn-cancel" class="btn btn-danger">Cancel</button>`;
    $("btn-pause").disabled  = !executing;
    $("btn-resume").disabled = !paused;
    $("btn-cancel").disabled = false;
  } else {
    // Show Run button; enabled only if a file is queued
    const hasQueued = selectedFileStem !== null;
    row.innerHTML =
      `<button id="btn-run" class="btn btn-success"${hasQueued ? "" : " disabled"}>Run</button>`;
  }
}

function formatDuration(secs) {
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

// ---------------------------------------------------------------------------
// Job controls
// ---------------------------------------------------------------------------

async function jobAction(action) {
  try {
    const resp = await fetch(`/api/job/${action}`, { method: "POST" });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      console.warn(`${action} failed:`, body.detail ?? resp.status);
    }
  } catch (e) {
    console.error("jobAction error", e);
  }
}

// ---------------------------------------------------------------------------
// Console
// ---------------------------------------------------------------------------

let consoleAutoScroll = true;
let lastConsoleTimestamp = 0;

function isStatusResponse(text) {
  // GRBL status reports: <Idle|MPos:...> or <Run|...> etc.
  return text.trimStart().startsWith("<") && text.trimEnd().endsWith(">");
}

function hideStatusEnabled() {
  return $("hide-status").checked;
}

async function loadConsole() {
  // Initial load: fetch last 100 entries and render from scratch
  try {
    const resp = await fetch("/api/console?n=100");
    const lines = await resp.json();
    const el = $("console-log");
    el.innerHTML = "";
    lastConsoleTimestamp = 0;
    lines.forEach(appendConsoleLine);
  } catch (e) {
    console.warn("console load error", e);
  }
}

async function pollConsole() {
  // Incremental poll: fetch all entries, append only ones newer than last seen
  try {
    const resp = await fetch("/api/console?n=200");
    const lines = await resp.json();
    lines.forEach((entry) => {
      if (entry.t > lastConsoleTimestamp) {
        appendConsoleLine(entry);
      }
    });
  } catch (e) {
    console.warn("console poll error", e);
  }
}

function appendConsoleLine(entry) {
  if (entry.t > lastConsoleTimestamp) lastConsoleTimestamp = entry.t;
  if (hideStatusEnabled() && entry.dir === "rx" && isStatusResponse(entry.text)) return;

  const el = $("console-log");
  const div = document.createElement("div");
  div.className = "log-line";

  const t = new Date(entry.t * 1000);
  const ts = `${String(t.getHours()).padStart(2,"0")}:${String(t.getMinutes()).padStart(2,"0")}:${String(t.getSeconds()).padStart(2,"0")}`;

  div.innerHTML =
    `<span class="log-time">${ts}</span>` +
    `<span class="log-${entry.dir}">${escapeHtml(entry.text)}</span>`;

  el.appendChild(div);
  if (consoleAutoScroll) el.scrollTop = el.scrollHeight;
}

function escapeHtml(str) {
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

async function sendCommand(cmd) {
  try {
    await fetch("/api/console", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command: cmd }),
    });
  } catch (e) {
    console.error("sendCommand error", e);
  }
}

async function sendConsole() {
  const input = $("console-input");
  const command = input.value.trim();
  if (!command) return;
  input.value = "";
  try {
    await fetch("/api/console", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command }),
    });
  } catch (e) {
    console.error("sendConsole error", e);
  }
}

$("btn-home").addEventListener("click",         () => sendCommand("$H"));
$("btn-cancel-alarm").addEventListener("click", () => sendCommand("$X"));

$("btn-send").addEventListener("click", sendConsole);

// Allow Enter key in console input
$("console-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") sendConsole();
});

// Re-render console from scratch when the toggle changes (filter affects all lines)
$("hide-status").addEventListener("change", loadConsole);

// Pause auto-scroll when user scrolls up
$("console-log").addEventListener("scroll", (e) => {
  const el = e.target;
  consoleAutoScroll = el.scrollHeight - el.scrollTop <= el.clientHeight + 10;
});

// ---------------------------------------------------------------------------
// Run queued file
// ---------------------------------------------------------------------------

async function runQueued() {
  try {
    const resp = await fetch("/api/job/start", { method: "POST" });
    if (resp.ok) {
      setJobStatus("Job started.");
      selectedFileStem = null;
      await loadFiles();
    } else {
      const body = await resp.json().catch(() => ({}));
      setJobStatus(`Start failed: ${body.detail ?? resp.status}`);
    }
  } catch (e) {
    setJobStatus(`Start error: ${e.message}`);
  }
}

function setJobStatus(msg) {
  const el = $("job-status");
  if (el) el.textContent = msg;
}

// ---------------------------------------------------------------------------
// Files widget
// ---------------------------------------------------------------------------

// + button opens the hidden file input; selecting a file auto-uploads
$("btn-add-file").addEventListener("click", () => $("gcode-file").click());
$("gcode-file").addEventListener("change", async () => {
  const file = $("gcode-file").files[0];
  if (!file) return;
  const formData = new FormData();
  formData.append("file", file);
  // Reset so the same file can be re-selected later
  $("gcode-file").value = "";
  try {
    const resp = await fetch("/api/job", { method: "POST", body: formData });
    if (resp.ok) {
      const data = await resp.json();
      // Stage as "uploaded" — treat it like selecting a file from the list
      selectedFileStem = "uploaded";
      setJobFilename(data.filename ?? file.name);
      updateRunButton();
      setJobStatus(`Uploaded: ${file.name} (${data.line_count} lines)`);
      await loadFiles();
    } else {
      const body = await resp.json().catch(() => ({}));
      setJobStatus(`Upload failed: ${body.detail ?? resp.status}`);
    }
  } catch (e) {
    setJobStatus(`Upload error: ${e.message}`);
  }
});

function setJobFilename(name) {
  const el = $("job-filename");
  if (el) { el.textContent = name ?? "—"; el.title = name ?? ""; }
}

function updateRunButton() {
  // Only relevant when not executing/paused — controls are managed by updateJobControls otherwise
  const btn = $("btn-run");
  if (btn) btn.disabled = selectedFileStem === null;
}

function formatBytes(n) {
  if (n == null) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

async function loadFiles() {
  try {
    const resp = await fetch("/api/files");
    const files = await resp.json();
    renderFiles(files);
  } catch (e) {
    console.warn("files load error", e);
  }
}

function renderFiles(files) {
  const ul = $("files-list");
  if (!files.length) {
    ul.innerHTML = '<li class="files-empty">No files stored yet.</li>';
    return;
  }
  ul.innerHTML = "";
  for (const f of files) {
    const li = document.createElement("li");
    li.className = "file-item" + (f.stem === selectedFileStem ? " selected" : "");
    li.dataset.stem = f.stem;

    const lines = f.line_count != null ? `${f.line_count} lines` : formatBytes(f.size_bytes);
    li.innerHTML =
      `<span class="file-name" title="${escapeHtml(f.display_name)}">${escapeHtml(f.display_name)}</span>` +
      `<span class="file-meta">${escapeHtml(lines)}</span>` +
      `<button class="btn-trash" title="Delete" data-stem="${escapeHtml(f.stem)}">🗑</button>`;

    li.addEventListener("click", (e) => {
      if (e.target.classList.contains("btn-trash")) return;
      selectFile(f.stem, f.display_name);
    });
    li.querySelector(".btn-trash").addEventListener("click", (e) => {
      e.stopPropagation();
      deleteFile(f.stem, f.display_name);
    });

    ul.appendChild(li);
  }
}

async function selectFile(stem, displayName) {
  try {
    const resp = await fetch(`/api/files/${encodeURIComponent(stem)}/select`, { method: "POST" });
    if (resp.ok) {
      selectedFileStem = stem;
      setJobFilename(displayName);
      updateRunButton();
      setJobStatus("");
      await loadFiles();  // re-render to update selected highlight
    } else {
      const body = await resp.json().catch(() => ({}));
      console.warn("select failed:", body.detail ?? resp.status);
    }
  } catch (e) {
    console.error("selectFile error", e);
  }
}

async function deleteFile(stem, displayName) {
  if (!confirm(`Delete "${displayName}"?`)) return;
  try {
    const resp = await fetch(`/api/files/${encodeURIComponent(stem)}`, { method: "DELETE" });
    if (resp.ok) {
      if (stem === selectedFileStem) {
        selectedFileStem = null;
        setJobFilename(null);
        updateRunButton();
      }
      await loadFiles();
    } else {
      const body = await resp.json().catch(() => ({}));
      console.warn("delete failed:", body.detail ?? resp.status);
    }
  } catch (e) {
    console.error("deleteFile error", e);
  }
}

// ---------------------------------------------------------------------------
// Job History
// ---------------------------------------------------------------------------

async function loadHistory() {
  try {
    const resp = await fetch("/api/jobs");
    const jobs = await resp.json();
    const tbody = $("history-body");
    if (!jobs.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="history-empty">No completed jobs yet.</td></tr>';
      return;
    }
    tbody.innerHTML = "";
    for (const job of jobs) {
      const dt = new Date(job.start_time * 1000);
      const dateStr = dt.toLocaleDateString() + " " + dt.toLocaleTimeString();
      const dur = formatDuration(job.duration_s ?? 0);
      const source = job.source === "upload" && job.original_filename
        ? escapeHtml(job.original_filename)
        : (job.source ?? "lightburn");
      // Extract timestamp stem from path (e.g. "20250321_143022")
      const stem = (job.path ?? "").replace(/.*\//, "").replace(/\.gcode$/, "");
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td class="mono">${escapeHtml(dateStr)}</td>` +
        `<td>${source}</td>` +
        `<td class="mono">${job.line_count ?? "—"}</td>` +
        `<td class="mono">${dur}</td>` +
        `<td><a href="/api/jobs/${encodeURIComponent(stem)}/download" class="dl-link">↓</a></td>`;
      tbody.appendChild(tr);
    }
  } catch (e) {
    console.warn("history load error", e);
  }
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

connectWS();
loadConsole();
loadHistory();
loadFiles();

// Poll for new console entries and append only the new ones
setInterval(pollConsole, 1000);
// Refresh history every 30s
setInterval(loadHistory, 30000);
// Refresh file list every 10s
setInterval(loadFiles, 10000);
