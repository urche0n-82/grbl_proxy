// grbl-proxy dashboard — vanilla JS, no build tooling

const $ = (id) => document.getElementById(id);

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

  // Enable/disable control buttons based on state
  const executing = s.proxy_state === "Executing";
  const paused    = s.proxy_state === "Paused";
  $("btn-pause").disabled  = !executing;
  $("btn-resume").disabled = !paused;
  $("btn-cancel").disabled = !(executing || paused);
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

// Button event listeners
$("btn-pause").addEventListener("click",  () => jobAction("pause"));
$("btn-resume").addEventListener("click", () => jobAction("resume"));
$("btn-cancel").addEventListener("click", () => jobAction("cancel"));
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
// Upload & Run
// ---------------------------------------------------------------------------

async function uploadFile() {
  const fileInput = $("gcode-file");
  const file = fileInput.files[0];
  if (!file) {
    $("upload-status").textContent = "Select a file first.";
    return;
  }
  const formData = new FormData();
  formData.append("file", file);
  try {
    const resp = await fetch("/api/job", { method: "POST", body: formData });
    if (resp.ok) {
      const data = await resp.json();
      $("upload-status").textContent = `Uploaded: ${file.name} (${data.line_count} lines)`;
      $("btn-run").disabled = false;
      $("btn-run").dataset.filename = file.name;
    } else {
      const body = await resp.json().catch(() => ({}));
      $("upload-status").textContent = `Upload failed: ${body.detail ?? resp.status}`;
    }
  } catch (e) {
    $("upload-status").textContent = `Upload error: ${e.message}`;
  }
}

async function runUploaded() {
  try {
    const resp = await fetch("/api/job/start", { method: "POST" });
    if (resp.ok) {
      $("upload-status").textContent = "Job started.";
      $("btn-run").disabled = true;
    } else {
      const body = await resp.json().catch(() => ({}));
      $("upload-status").textContent = `Start failed: ${body.detail ?? resp.status}`;
    }
  } catch (e) {
    $("upload-status").textContent = `Start error: ${e.message}`;
  }
}

$("btn-upload").addEventListener("click", uploadFile);
$("btn-run").addEventListener("click", runUploaded);

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

// Poll for new console entries and append only the new ones
setInterval(pollConsole, 1000);
// Refresh history every 30s
setInterval(loadHistory, 30000);
