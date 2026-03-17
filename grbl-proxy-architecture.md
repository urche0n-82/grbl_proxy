# GRBL Laser Proxy — Architecture Specification

A Raspberry Pi-based intermediary that sits between LightBurn and a GRBL laser (Creality Falcon 2 Pro), enabling full-file job buffering, disconnect-safe execution, and remote monitoring.

---

## 1. Design goals

1. **LightBurn-native**: LightBurn connects to the proxy as a standard GRBL-over-TCP device — no plugins, no export-and-upload workflow. The user hits "Start" in LightBurn and the job runs.
2. **Disconnect-safe**: Once a job is buffered, it executes to completion regardless of whether LightBurn remains connected. WiFi drops, laptop sleeps — the laser keeps cutting.
3. **Remotely observable**: A web dashboard shows job progress, machine state, and provides pause/resume/cancel controls.
4. **Transparent in idle**: When no job is running, the proxy is invisible — LightBurn commands pass through to GRBL and responses come back in real time. Jogging, homing, framing, and console commands all work normally.

---

## 2. System components

### 2.1 TCP server (LightBurn-facing)

- Listens on a configurable port (default `23` to match what LightBurn expects for GRBL-over-TCP, or `8899` which some devices use).
- Accepts a single connection at a time. If LightBurn reconnects, the previous socket is cleanly dropped and replaced.
- Speaks raw GRBL text protocol — no framing, no binary headers. Each line is `\n`-terminated.
- In LightBurn, the user creates a manual GRBL device with connection type "Ethernet/TCP" pointing to the Pi's IP and the configured port.

### 2.2 Proxy core (state machine)

The central coordinator. Maintains the current proxy state and routes data between the TCP server, serial port, job buffer, and web server. Five states:

| State | LightBurn commands | Serial port | Behavior |
|---|---|---|---|
| **Disconnected** | N/A | Idle, periodic `?` polling | Waiting for TCP connection |
| **Passthrough** | Relayed to serial | Active, bidirectional | Transparent bridge — LB ↔ GRBL |
| **Buffering** | Captured to disk | Holds, `ok` spoofed to LB | Accumulating G-code file |
| **Executing** | Status queries answered synthetically | Streaming from buffer | Job running independently |
| **Paused** | Status queries show paused state | Feed hold (`!`) sent | Waiting for resume |

### 2.3 Job buffer

- G-code is written to a temporary file on disk (not held entirely in RAM) so arbitrarily large files work.
- Located at a configurable path, default `~/.grbl-proxy/jobs/current.gcode`.
- Metadata tracked alongside: original filename, total line count, start time, estimated duration.

### 2.4 GRBL streamer

- Handles the actual serial communication with the Falcon 2 Pro's ESP32-S2 board.
- Uses the **character-counting** streaming protocol (not simple send-response) for maximum throughput:
  - Maintains a count of bytes in GRBL's 128-byte RX buffer.
  - Sends the next line only when there's room: `buffer_available >= len(next_line)`.
  - On each `ok` from GRBL, subtracts the byte count of the corresponding sent line.
- Polls GRBL status via `?` at ~4Hz during execution for position, state, and buffer status.
- Handles `error:N` responses by logging the error, pausing execution, and exposing the error through the web API.

### 2.5 Serial connection

- `/dev/ttyUSB0` or `/dev/ttyACM0` (auto-detected, configurable override).
- 115200 baud, 8N1.
- **Critical**: Disable DTR on open (`dsrdtr=False`, `rtscts=False` in pyserial) to prevent the ESP32-S2 from resetting every time the proxy starts or the serial port is re-opened.
- Implements a reconnection loop — if USB is disconnected and reconnected, the proxy detects the new device and re-establishes the serial link.

### 2.6 Web server

- FastAPI application served via uvicorn on port `8080`.
- REST API for machine state, job progress, and control commands.
- WebSocket endpoint for real-time status streaming to the dashboard.
- Static HTML/JS dashboard (single-page app) served from the same process.

---

## 3. Protocol handling — the details

### 3.1 GRBL protocol primer

GRBL 1.1 (which the Falcon 2 Pro runs) uses a simple line-based text protocol:

- **Commands sent to GRBL**: G-code lines (`G0 X10 Y20\n`), real-time commands (`?`, `!`, `~`, `\x18`), and system commands (`$$`, `$H`, `$X`).
- **Responses from GRBL**: `ok` (command accepted), `error:N` (command rejected), and status reports `<Idle|MPos:0.000,0.000,0.000|FS:0,0|WCO:0.000,0.000,0.000>`.
- Real-time commands (`?`, `!`, `~`, Ctrl-X) are single characters that can be sent at any time — they don't go into the line buffer and don't consume an `ok`.

### 3.2 Passthrough mode

In this mode the proxy is a transparent bidirectional relay:

```
LightBurn → [TCP] → Proxy → [Serial] → GRBL
LightBurn ← [TCP] ← Proxy ← [Serial] ← GRBL
```

Every byte from LightBurn is forwarded to serial. Every byte from serial is forwarded back to LightBurn. The proxy additionally:

- Snoops on `?` responses to maintain its own copy of machine state (position, status, feed rate).
- Watches for the transition signal that indicates a job is starting (see §3.3).

### 3.3 Detecting job start (passthrough → buffering transition)

This is the trickiest design decision. LightBurn doesn't send a "begin job" command — it just starts sending G-code lines faster. We need a heuristic to distinguish interactive commands from a job stream.

**Approach: Rapid-fire detection with motion command ratio.**

When LightBurn starts a job, it sends a burst of lines with very short inter-line gaps (<50ms). Interactive use has gaps of hundreds of milliseconds to seconds. The proxy tracks:

- Inter-line arrival rate (moving average over last 10 lines).
- Ratio of motion commands (`G0`, `G1`, `G2`, `G3`, `M3`, `M4`, `M5`, `S`) vs. query commands (`?`, `$$`).

Transition to Buffering when:
- More than 10 lines arrive within 500ms, **AND**
- At least 80% are motion/laser commands.

**Alternative approach: Explicit trigger.**

Rather than heuristic detection, LightBurn's "Start G-code" field (configured per-device) can include a custom comment that acts as a signal:

```gcode
; PROXY_JOB_START
G28.1  ; or whatever preamble
```

The proxy watches for `; PROXY_JOB_START` and switches to Buffering. Similarly, `; PROXY_JOB_END` at the end. This is more reliable and recommended if you're the only user.

**Hybrid approach (recommended):**

Use the explicit comment trigger as the primary mechanism, with the rapid-fire heuristic as a fallback so it still works if someone forgets the comment or uses a different machine profile.

### 3.4 Buffering mode

Once triggered:

1. The proxy stops forwarding G-code lines to serial.
2. Each incoming G-code line is written to the job file on disk.
3. The proxy immediately responds `ok` to LightBurn for each line (spoofed — GRBL never sees these lines yet). This makes LightBurn think the commands are being processed and it continues sending at full speed.
4. Real-time commands (`?`) are still handled — the proxy responds with the last known status (position from before the job, state shown as `Run`).
5. When LightBurn finishes sending, it typically either:
   - Sends an `M5` (laser off) + `M2` or `M30` (program end), or
   - Simply stops sending for a prolonged period (>2 seconds with no new lines).
6. On detecting job-end, the proxy transitions to Executing.

**Buffer integrity**: The proxy counts lines written and computes a running checksum. If the TCP connection drops mid-buffer, the incomplete job is discarded and the proxy returns to Passthrough (or Disconnected).

### 3.5 Executing mode

The GRBL streamer reads lines from the buffered file and streams them to serial using character-counting flow control:

```python
RX_BUFFER_SIZE = 128
buffer_used = 0
line_lengths = deque()  # tracks byte count of each in-flight line

for line in job_file:
    line_bytes = len(line.encode()) + 1  # +1 for \n
    
    # Wait until there's room
    while buffer_used + line_bytes > RX_BUFFER_SIZE:
        response = serial.readline()  # blocks until ok/error
        buffer_used -= line_lengths.popleft()
    
    serial.write((line + '\n').encode())
    buffer_used += line_bytes
    line_lengths.append(line_bytes)
```

During execution:

- The proxy polls GRBL with `?` at ~4Hz to get real-time position, feed rate, and machine state.
- If LightBurn is still connected, `?` queries from LightBurn are answered with synthesized status responses reflecting actual machine position but with the progress info the proxy tracks (current line / total lines).
- If LightBurn disconnects, execution continues. The web dashboard shows live progress.
- Pause (`!`), resume (`~`), and cancel (Ctrl-X / soft reset) can come from the web dashboard or from LightBurn if connected.

### 3.6 LightBurn reconnection during execution

If LightBurn connects while a job is running:

1. TCP connection is accepted.
2. Any `?` queries get real-time status from the running job.
3. Interactive commands (`$$`, jog, etc.) are queued or rejected with a synthetic `error:9` (busy) — GRBL can't process them mid-job anyway.
4. LightBurn will show the machine as "Busy" or "Running" based on the status response, which is correct behavior.

### 3.7 Handling GRBL alarms and errors during execution

- **`error:N` on a line**: Log the error, pause execution, expose via web API. Allow the operator to skip the line or cancel the job.
- **Alarm state** (e.g., limit switch hit): Serial shows `ALARM:N`. The proxy enters Error state, stops streaming, exposes the alarm via web. The operator can issue `$X` (alarm clear) or `$H` (re-home) via the web dashboard to recover.

---

## 4. Web dashboard and API

### 4.1 REST API

| Endpoint | Method | Description |
|---|---|---|
| `/api/status` | GET | Machine state, position, speeds, proxy state |
| `/api/job` | GET | Current job info: filename, progress, ETA |
| `/api/job` | POST | Upload a G-code file directly (bypass LightBurn) |
| `/api/job/start` | POST | Start an uploaded job |
| `/api/job/pause` | POST | Send feed hold to GRBL |
| `/api/job/resume` | POST | Send cycle resume to GRBL |
| `/api/job/cancel` | POST | Soft reset + clear job buffer |
| `/api/console` | POST | Send an arbitrary GRBL command |
| `/api/console` | GET | Recent console log (last N lines) |
| `/api/settings` | GET | Proxy configuration |

### 4.2 WebSocket

- Endpoint: `/ws/status`
- Pushes JSON status updates at ~4Hz during execution, ~1Hz during idle.
- Payload includes: machine state, position (MPos + WPos), feed rate, spindle/laser power, job progress (line/total, percentage, elapsed time, ETA), proxy state.

### 4.3 Dashboard features

- Real-time position display and simple 2D toolpath visualization (plot the G-code and highlight current position).
- Job progress bar with ETA.
- Pause / Resume / Cancel buttons.
- Console panel for sending manual GRBL commands.
- Job history (last 10 jobs with timestamps and outcomes).
- Optional: webcam feed via MJPEG stream from a Pi camera or USB webcam.

---

## 5. Technology choices

| Component | Technology | Rationale |
|---|---|---|
| Language | Python 3.11+ | pyserial, asyncio, rich ecosystem, quick iteration |
| Serial | pyserial + asyncio wrapper | Well-proven for GRBL communication |
| TCP server | asyncio streams | Lightweight, native, no framework overhead |
| Web framework | FastAPI + uvicorn | Async-native, WebSocket support, auto-generated API docs |
| Dashboard | Vanilla JS + HTML | No build tooling on Pi, minimal overhead |
| Process manager | systemd | Auto-start on boot, restart on crash, journal logging |
| Config | YAML file | `~/.grbl-proxy/config.yaml` |

### 5.1 Python project structure

```
grbl-proxy/
├── pyproject.toml
├── config.yaml.example
├── src/
│   └── grbl_proxy/
│       ├── __init__.py
│       ├── main.py              # Entry point, wires everything together
│       ├── config.py            # Config loading and validation
│       ├── serial_conn.py       # Serial port management + reconnection
│       ├── grbl_protocol.py     # GRBL message parsing and generation
│       ├── streamer.py          # Character-counting GRBL streamer
│       ├── proxy_core.py        # State machine + routing logic
│       ├── tcp_server.py        # LightBurn-facing TCP server
│       ├── job_buffer.py        # File-backed job buffer + metadata
│       ├── web/
│       │   ├── app.py           # FastAPI application
│       │   ├── routes.py        # REST endpoints
│       │   ├── websocket.py     # WebSocket handler
│       │   └── static/          # Dashboard HTML/JS/CSS
│       └── util/
│           ├── gcode_parser.py  # G-code line classification
│           └── logging.py       # Structured logging setup
├── tests/
│   ├── test_grbl_protocol.py
│   ├── test_streamer.py
│   ├── test_proxy_core.py
│   └── mock_grbl.py            # Simulated GRBL for testing without hardware
└── systemd/
    └── grbl-proxy.service
```

---

## 6. Configuration

```yaml
# ~/.grbl-proxy/config.yaml

serial:
  port: auto              # auto-detect, or /dev/ttyUSB0
  baud: 115200
  dtr: false              # prevent ESP32-S2 reset on connect
  reconnect_interval: 5   # seconds between reconnection attempts

tcp:
  host: 0.0.0.0
  port: 23                # LightBurn default for GRBL TCP
  # Alternative: 8899 if port 23 conflicts (needs root or setcap)

web:
  host: 0.0.0.0
  port: 8080

job:
  storage_dir: ~/.grbl-proxy/jobs
  max_history: 20
  start_marker: "; PROXY_JOB_START"
  end_marker: "; PROXY_JOB_END"
  auto_detect:
    enabled: true          # fall back to rapid-fire heuristic
    line_burst: 10         # lines within window to trigger
    window_ms: 500
    motion_ratio: 0.8

machine:
  name: "Falcon 2 Pro"
  work_area: [400, 415]   # mm
  status_poll_hz: 4
```

---

## 7. Deployment

### 7.1 Raspberry Pi setup

Any Pi with USB and WiFi works. A Pi 3B+ or Pi Zero 2 W is sufficient — the proxy is not CPU-intensive. A Pi 4 or 5 gives headroom for the webcam stream.

```bash
# Install system dependencies
sudo apt update && sudo apt install -y python3-pip python3-venv

# Clone and install
git clone <repo> ~/grbl-proxy
cd ~/grbl-proxy
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Copy and edit config
cp config.yaml.example ~/.grbl-proxy/config.yaml
nano ~/.grbl-proxy/config.yaml

# Install systemd service
sudo cp systemd/grbl-proxy.service /etc/systemd/system/
sudo systemctl enable grbl-proxy
sudo systemctl start grbl-proxy
```

### 7.2 Port 23 without root

Port 23 requires elevated privileges. Options:
1. Use `setcap` to grant the Python binary the capability: `sudo setcap 'cap_net_bind_service=+ep' $(readlink -f .venv/bin/python3)`
2. Use a higher port (e.g., 8899) and configure LightBurn with that port.
3. Use iptables to redirect 23 → a high port.

### 7.3 LightBurn device setup

1. In LightBurn → Devices → Create Manually.
2. Select **GRBL**.
3. Connection type: **Ethernet/TCP**.
4. IP: the Pi's static IP on your LAN.
5. Port: 23 (or configured port).
6. Set work area to 400×415mm.
7. In the device's Start G-code field, add: `; PROXY_JOB_START`
8. In the End G-code field, add: `; PROXY_JOB_END`

---

## 8. Development phases

### Phase 1 — Serial bridge (1-2 days)

Build the transparent passthrough proxy: TCP server + serial connection + bidirectional relay. At this stage it's functionally equivalent to ser2net but in Python, which gives you the foundation. Test that LightBurn can connect, home, jog, and frame through the proxy.

### Phase 2 — Job detection and buffering (2-3 days)

Add the state machine, job detection (comment markers + heuristic), and buffering. Test that LightBurn can "run" a job and the proxy captures it to disk while spoofing `ok` responses.

### Phase 3 — GRBL streamer (2-3 days)

Implement the character-counting streamer. Execute buffered jobs. Verify that cutting works correctly by running a simple test pattern. Confirm that disconnecting LightBurn mid-job does not stop execution.

### Phase 4 — Web dashboard (3-4 days)

Build the FastAPI app, REST API, WebSocket status stream, and basic HTML dashboard with progress display and pause/resume/cancel controls.

### Phase 5 — Polish and edge cases (ongoing)

- Reconnection handling (serial disconnect, TCP reconnect mid-job).
- Alarm recovery via web dashboard.
- Job history and log viewer.
- Optional webcam integration.
- Optional: direct G-code upload via web (skip LightBurn entirely for repeat jobs).

---

## 9. Risk areas and mitigations

**Spoofed `ok` timing**: LightBurn expects `ok` responses at a pace consistent with GRBL's actual processing speed. If the proxy responds too fast, LightBurn's progress bar and time estimate will be wildly wrong (it'll show "done" in seconds). Mitigation: Add a small configurable delay (5-20ms) per spoofed `ok` to simulate realistic GRBL response timing, or let LightBurn finish fast and show job status on the web dashboard instead.

**Job detection false positives**: A macro or complex interactive sequence might trigger the rapid-fire heuristic. Mitigation: Use the explicit comment marker as the primary trigger, heuristic only as fallback, and make the thresholds configurable.

**Serial buffer overflow**: If the streamer miscounts bytes, GRBL's 128-byte buffer overflows and lines get corrupted. Mitigation: Conservative buffer tracking (assume +1 for \n, track exact byte counts not character counts), and validate against GRBL's `?` response which includes buffer fill info in newer builds.

**USB disconnect during job**: The ESP32-S2 USB-CDC connection can drop if there's electrical noise from the stepper drivers. Mitigation: The serial connection module implements auto-reconnection with a grace period. If reconnection happens within a configurable timeout (default 10s), the streamer resumes from the last acknowledged line. If it exceeds the timeout, the job is aborted and the user is notified via the web dashboard.

**Concurrent access**: Only one TCP client (LightBurn instance) should be connected at a time. The proxy enforces this by closing the previous connection when a new one arrives, with a log warning.

---

## 10. Future extensions

- **Camera-based fire detection**: Pi camera + simple frame-differencing to detect unexpected brightness, auto-pause the job and alert.
- **Job queue**: Upload multiple files, execute them in sequence.
- **Material library**: Store per-material speed/power settings accessible from the web dashboard.
- **OctoPrint plugin compatibility**: Expose a subset of the OctoPrint API so generic monitoring tools work with it.
- **mDNS/Bonjour discovery**: Advertise the proxy as `_grbl._tcp` so LightBurn could potentially auto-discover it.
