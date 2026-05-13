# GRBL Proxy

> **WARNING: This software controls laser cutting hardware. Use at your own risk.** The authors accept no liability for damage to equipment, materials, or injury caused by use of this software. Never leave a laser running unattended. Always power off the laser when not in use.

A Raspberry Pi proxy that sits between [LightBurn](https://lightburnsoftware.com/) and a GRBL laser cutter (tested on the Creality Falcon 2 Pro / ESP32-S2).

**LightBurn connects to the proxy exactly as it would connect to the laser directly** — no plugins, no export workflow. Hit Start in LightBurn as normal. Once the job is buffered, it runs to completion on the Pi even if your laptop sleeps, WiFi drops, or LightBurn is closed.

## Features

- **Transparent passthrough** — jogging, homing, framing, and console commands all work normally when idle
- **Disconnect-safe jobs** — once a job is buffered to disk it runs to completion, independent of the LightBurn connection
- **Web dashboard** — real-time machine position, job progress, pause/resume/cancel, file manager, and console from any browser on the network
- **Standalone operation** — upload G-code files directly from the dashboard and run them without LightBurn connected
- **File manager** — browse, select, and delete stored G-code files from the dashboard
- **Job history** — completed jobs are archived with metadata; the dashboard lists past runs with duration, line count, and a download link
- **Webcam feed** — optional live MJPEG webcam stream embedded in the dashboard (requires `mjpg-streamer` on the Pi)
- **Idle GRBL polling** — machine state and position are visible in the dashboard even when LightBurn is not connected
- **Auto-reconnect** — if the USB cable to the laser is unplugged, the proxy reconnects automatically
- **REST API** — full machine control and status over HTTP; interactive docs at `/api/docs`

## Requirements

- Raspberry Pi (3B+, 4, 5, or Zero 2 W) running Raspberry Pi OS
- Python 3.11 or later
- USB connection to a GRBL 1.1 laser controller
- LightBurn on any machine on the same network

---

## Installation

### 1. Clone the repository

On the Raspberry Pi:

```bash
git clone https://github.com/urche0n-82/grbl_proxy.git ~/grbl-proxy
cd ~/grbl-proxy
```

### 2. Run the installer

```bash
bash install.sh
```

The installer handles everything in one step:

- Installs required system packages (`python3-venv`, `libcap2-bin`)
- Adds your user to the `dialout` group for serial port access
- Creates a Python virtual environment and installs grbl-proxy into it
- Copies `config.yaml.example` to `~/.grbl-proxy/config.yaml`
- Grants the venv Python the `cap_net_bind_service` capability so it can listen on port 23
- Installs and enables the `grbl-proxy` systemd service

It is safe to re-run — steps that are already done are skipped.

### 3. Review configuration

```bash
nano ~/.grbl-proxy/config.yaml
```

The defaults work for most setups. The fields you are most likely to need to change:

```yaml
serial:
  port: auto        # or set explicitly: /dev/ttyUSB0, /dev/ttyACM0, etc.

machine:
  name: "Falcon 2 Pro"
  work_area: [400, 415]   # width x height in mm — match your machine
```

### 4. Log out and back in (if prompted)

If the installer added you to the `dialout` group, you must log out and back in before the group membership takes effect. The systemd service is already configured correctly — this step only matters if you plan to run the proxy manually as your user.

### 5. Start the service

```bash
sudo systemctl start grbl-proxy
```

Check that it started cleanly:

```bash
sudo systemctl status grbl-proxy
journalctl -u grbl-proxy -f
```

The service is enabled to start automatically on boot.

---

## LightBurn setup

### Add the device

1. Open LightBurn → **Laser** panel → **Devices**
2. Click **Create Manually**
3. Device type: **GRBL**
4. Connection: **Ethernet/TCP**
5. IP address: your Pi's IP address (e.g. `192.168.1.123`)

7. Work area: match your machine (e.g. `400 × 415 mm` for the Falcon 2 Pro)
8. Click **Finish**, then select the new device

> **Finding your Pi's IP address:** run `hostname -I` on the Pi, or check your router's DHCP table. A static IP is recommended so the address never changes — set this in your router or in `/etc/dhcpcd.conf` on the Pi.

> **Changing the Port:** Lightburn defaults to Port 23 during the device setup wizard. Once the device is set up, you can change the port in **Laser Tools -> Device Settings -> Basic Settings** and look for the **Network Port** setting.

### Configure the job buffer start marker

The proxy uses a G-code marker sent at the start of a job to know when to begin buffering. Configure it in LightBurn under **Laser Tools → Device Settings → Gcode**:

- **Start G-code:** `G4 P0.0`
- **End G-code:** *(leave blank)*

`G4 P0.0` is a zero-duration dwell — it is harmless to GRBL and does not move the laser, but it tells grbl-proxy to start buffering the job. The End G-code field can be left blank because LightBurn automatically sends `M30` at the end of every job, which the proxy uses as the end-of-job signal.  If you have a need to signal end-gcode with some other command, it can be set here.

> Without the Start G-code marker, the proxy will not know when a job begins and will pass all G-code straight through to the laser without buffering. The job will run normally, but it will not be disconnect-safe.

### Verify the connection

Click **Connect** in LightBurn. The status bar should show the device as connected. Send `$I` from the LightBurn console — you should see GRBL's version string in the response.

---

## Web dashboard

Open a browser on any device on the same network:

```
http://<pi-ip>:8080
```

The dashboard provides:

| Widget | What it shows / does |
|---|---|
| **Machine Status** | Proxy state, GRBL state (Idle / Run / Hold / Alarm), serial connection badge, X/Y/Z position, feed rate, spindle power. Home and Cancel Alarm buttons. |
| **Job** | Active job progress bar, lines sent / total, elapsed time. Run, Pause / Resume, and Cancel buttons. Shows the selected file name. |
| **Files** | Lists all stored G-code files. Click a file to select it for the next run. `+` button to upload a new file. Trash icon to delete. Uploaded files retain their original filename. |
| **Webcam** | Live MJPEG feed from a USB webcam (if configured). Collapsible; pauses the stream when collapsed to save bandwidth. |
| **Console** | Scrolling log of recent serial I/O. Command input field to send GRBL commands directly. Toggle to hide `?` status-report noise. |
| **Job History** | Table of completed jobs: date, source (LightBurn or upload), line count, duration, and a download link for the G-code file. |

The dashboard updates in real time via WebSocket — 4 times per second during a job, once per second when idle.

---

## Configuration reference

The full config file with all defaults:

```yaml
# ~/.grbl-proxy/config.yaml

serial:
  port: auto                  # auto-detect, or explicit path e.g. /dev/ttyACM0
  baud: 115200
  dtr: false                  # CRITICAL: must be false for ESP32-S2 controllers
  reconnect_interval: 5       # seconds between reconnect attempts

tcp:
  host: 0.0.0.0
  port: 23                    # LightBurn defaults to port 23 for GRBL Ethernet devices

web:
  host: 0.0.0.0
  port: 8080

job:
  storage_dir: ~/.grbl-proxy/jobs
  max_history: 20             # oldest files are deleted when this limit is exceeded
  start_marker: "G4 P0.0"    # must match LightBurn's Start G-code field
  end_marker: ""              # leave blank — LightBurn sends M30 to end the job

machine:
  name: "GRBL Machine"
  work_area: [400, 415]       # mm [width, height]
  status_poll_hz: 4           # how often to poll GRBL when LightBurn is not connected

webcam:
  enabled: false
  stream_url: ""              # e.g. "http://10.0.8.141:8081/?action=stream"
```

Only include the fields you want to change — everything else falls back to the defaults shown above.

---

## Webcam setup (optional)

The dashboard can display a live feed from a USB webcam connected to the Pi. The proxy does not capture video itself — it uses [`mjpg-streamer`](https://github.com/jacksonliam/mjpg-streamer) as a separate process to serve the camera as an MJPEG HTTP stream.

### Install mjpg-streamer

```bash
sudo apt install mjpg-streamer
```

If it is not available via apt, build from source:

```bash
sudo apt install cmake libjpeg9-dev
git clone https://github.com/jacksonliam/mjpg-streamer.git
cd mjpg-streamer/mjpg-streamer-experimental
make
sudo make install
```

### Run mjpg-streamer

```bash
mjpg_streamer \
  -i "input_uvc.so -d /dev/video0 -r 640x480 -f 15" \
  -o "output_http.so -p 8081 -w /usr/share/mjpg-streamer/www"
```

The stream is then available at `http://<pi-ip>:8081/?action=stream`. You can open this URL directly in any browser to verify the camera is working before wiring it into the dashboard.

To start mjpg-streamer automatically on boot, create a systemd service or add it to `/etc/rc.local`.

### Enable the webcam widget

Add to `~/.grbl-proxy/config.yaml`:

```yaml
webcam:
  enabled: true
  stream_url: "http://10.0.8.141:8081/?action=stream"
```

Restart grbl-proxy (`sudo systemctl restart grbl-proxy`). The Webcam card will appear in the dashboard. The stream only loads when the widget is expanded — collapsing it disconnects the stream to save bandwidth. The collapsed/expanded state is remembered between page loads.

The same `stream_url` can be opened directly in a browser, VLC, or OBS on your Mac.

---

## Running manually (for testing)

```bash
cd ~/grbl-proxy
.venv/bin/grbl-proxy --debug
```

Verify the GRBL connection with netcat:

```bash
nc <pi-ip> 23
```

Type `?` and press Enter — you should get a GRBL status response like `<Idle|MPos:0.000,0.000,0.000|FS:0,0>`.

### CLI options

```
grbl-proxy --help

usage: grbl-proxy [-h] [--config CONFIG] [--debug]

options:
  -h, --help            show this help message and exit
  --config, -c CONFIG   path to config.yaml (default: ~/.grbl-proxy/config.yaml)
  --debug, -d           enable debug logging
```

---

## REST API

| Endpoint | Method | Description |
|---|---|---|
| `/api/status` | GET | Full machine and proxy state snapshot |
| `/api/job` | GET | Job progress (lines sent, total, percentage, elapsed time) |
| `/api/job` | POST | Upload a G-code file (multipart form, field `file`) |
| `/api/job/start` | POST | Start the uploaded file — valid in Passthrough or Disconnected state |
| `/api/job/pause` | POST | Send feed hold — valid in Executing state |
| `/api/job/resume` | POST | Send cycle resume — valid in Paused state |
| `/api/job/cancel` | POST | Soft reset + cancel — valid in Executing or Paused state |
| `/api/files` | GET | List all stored G-code files with name, size, and line count |
| `/api/files/{stem}/select` | POST | Stage an existing file as the next job to run |
| `/api/files/{stem}` | DELETE | Delete a stored G-code file |
| `/api/jobs` | GET | Completed job history, newest first |
| `/api/jobs/{stem}/download` | GET | Download a completed job's G-code file |
| `/api/console` | GET | Recent serial console log (`?n=50` to control count) |
| `/api/console` | POST | Send a GRBL command (`{"command": "$I"}`) |
| `/api/webcam` | GET | Webcam config (`{enabled, stream_url}`) |
| `/api/settings` | GET | Full proxy configuration |
| `/ws/status` | WebSocket | Real-time status push — 4 Hz during execution, 1 Hz at idle |

Control endpoints return HTTP 409 with a reason string if the command is not valid in the current state.

Interactive API docs: `http://<pi-ip>:8080/api/docs`

---

## Troubleshooting

**Serial port not found**
Run `ls /dev/ttyUSB* /dev/ttyACM*` with the laser connected. Set the result explicitly in `serial.port` in the config.

**Permission denied on serial port**
Ensure your user is in the `dialout` group: `sudo usermod -aG dialout $USER`, then log out and back in. The installer does this automatically.

**Laser resets every time the proxy starts or reconnects**
This is DTR/RTS being asserted on the serial port, which triggers the ESP32-S2 bootloader. Ensure `serial.dtr: false` is set in your config. The installer-generated config already has this set correctly.

**LightBurn shows "disconnected" immediately**
Check the proxy is running: `sudo systemctl status grbl-proxy`. Check it is listening on port 23: `sudo ss -tlnp | grep 23`. Verify the Pi's IP and port in the LightBurn device settings.

**Job starts in LightBurn but the proxy doesn't buffer it**
The Start G-code marker is missing or wrong. In LightBurn under **Edit → Device Settings → Additional Settings**, set **Start G-code** to exactly `G4 P0.0` — this must match `job.start_marker` in your config.

**Web dashboard not reachable**
Confirm the service is running: `journalctl -u grbl-proxy -f`. Check that port 8080 is not blocked: `sudo ufw status`. The web port is configurable via `web.port` in `config.yaml`.

**Proxy stays in Executing state after a job finishes**
Check `journalctl -u grbl-proxy` for GRBL error or alarm messages. The proxy will stay in Executing state if GRBL returns an error during the job — use the **Cancel Alarm** button in the dashboard (or send `$X` from the console) to clear the alarm and return to idle.

**Reconnect loop filling logs**
Reconnect polling at the `INFO` level only appears when the proxy is actively attempting to open the port. The "waiting for port to appear" messages are at `DEBUG` level and only visible with `--debug`.

---

## Development

```bash
pip install -e ".[dev]"
pytest
```

All tests run without hardware using an in-process GRBL mock — no serial port or laser required.

---

## Project structure

```
grbl-proxy/
├── src/grbl_proxy/
│   ├── main.py              # entry point, CLI, wiring
│   ├── config.py            # YAML config loading
│   ├── serial_conn.py       # serial port management + auto-reconnect
│   ├── grbl_protocol.py     # GRBL 1.1 message parser
│   ├── tcp_server.py        # TCP server + bidirectional relay
│   ├── proxy_core.py        # state machine: Passthrough/Buffering/Executing/Paused/Error
│   ├── job_buffer.py        # disk-based G-code buffer + job history
│   ├── streamer.py          # character-counting GRBL streamer
│   └── web/
│       ├── app.py           # FastAPI application factory
│       ├── routes.py        # REST endpoints + WebSocket push
│       ├── status.py        # ProxyStatus / ProxyControl facades
│       ├── console_log.py   # serial I/O ring buffer
│       └── static/          # dashboard HTML/JS/CSS
├── tests/
├── systemd/
│   └── grbl-proxy.service
├── config.yaml.example
└── install.sh
```

---

## License

MIT
