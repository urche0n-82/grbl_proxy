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

function isStatusResponse(text) {
  // GRBL status reports: <Idle|MPos:...> or <Run|...> etc.
  return text.trimStart().startsWith("<") && text.trimEnd().endsWith(">");
}

function hideStatusEnabled() {
  return $("hide-status").checked;
}

async function loadConsole() {
  try {
    const resp = await fetch("/api/console?n=100");
    const lines = await resp.json();
    const el = $("console-log");
    el.innerHTML = "";
    lines.forEach(appendConsoleLine);
  } catch (e) {
    console.warn("console load error", e);
  }
}

function appendConsoleLine(entry) {
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

// Allow Enter key in console input
$("console-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") sendConsole();
});

// Re-render console immediately when the toggle changes
$("hide-status").addEventListener("change", loadConsole);

// Pause auto-scroll when user scrolls up
$("console-log").addEventListener("scroll", (e) => {
  const el = e.target;
  consoleAutoScroll = el.scrollHeight - el.scrollTop <= el.clientHeight + 10;
});

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

connectWS();
loadConsole();

// Refresh console log periodically (WebSocket handles machine state)
setInterval(loadConsole, 5000);
