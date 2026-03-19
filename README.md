# GRBL Proxy

> **Early development — untested on hardware.** This project is a work in progress. APIs and configuration may change. Use at your own risk.

A Raspberry Pi-based proxy that sits between [LightBurn](https://lightburnsoftware.com/) and a GRBL laser cutter (tested on the Creality Falcon 2 Pro), enabling disconnect-safe job execution and remote monitoring.

**LightBurn connects to the proxy exactly as it would to a direct GRBL device** — no plugins, no export workflow. Hit Start in LightBurn and walk away. If your laptop sleeps or WiFi drops, the job keeps running.

## Features

- **Transparent passthrough** — jogging, homing, framing, and console commands all work normally when idle
- **Disconnect-safe jobs** — once a job is buffered, it executes to completion regardless of the LightBurn connection (Phase 2+)
- **Web dashboard** — real-time progress, pause/resume/cancel from a browser (Phase 4+)
- **Auto-reconnect** — if the USB cable to the laser drops, the proxy reconnects automatically
- **Single TCP client** — if LightBurn reconnects, the old socket is cleanly replaced

## Requirements

- Raspberry Pi (3B+, 4, 5, or Zero 2 W) running Raspberry Pi OS
- Python 3.11 or later
- USB connection to a GRBL 1.1 laser controller (e.g. Creality Falcon 2 Pro / ESP32-S2)
- LightBurn on any machine on the same network

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/urche0n-82/grbl_proxy.git ~/grbl-proxy
cd ~/grbl-proxy
```

### 2. Run the installer

```bash
bash install.sh
```

The installer handles everything: system packages, Python virtual environment, config file, port 23 capability grant, and systemd service registration. It is safe to re-run — steps that are already complete are skipped.

After it finishes, review your config if needed:

```bash
nano ~/.grbl-proxy/config.yaml
```

The only field you may need to change is `serial.port` if auto-detection picks the wrong device:

```yaml
serial:
  port: auto        # or /dev/ttyUSB0, /dev/ttyACM0, etc.
```

If the installer added you to the `dialout` group, **log out and back in** before starting the service.

## Running

### As a systemd service (auto-start on boot)

```bash
sudo systemctl start grbl-proxy
```

Check status and logs:

```bash
sudo systemctl status grbl-proxy
journalctl -u grbl-proxy -f
```

### Manually (for testing)

```bash
.venv/bin/grbl-proxy --debug
```

Type `?` into a netcat session to verify the connection:

```bash
nc <pi-ip> 23
```

You should get a GRBL status response like `<Idle|MPos:0.000,0.000,0.000|FS:0,0>`.

## LightBurn device setup

1. Open LightBurn → **Devices** → **Create Manually**
2. Select **GRBL**
3. Connection type: **Ethernet/TCP**
4. IP address: your Pi's static IP on the local network
5. Port: `23`
6. Work area: `400 × 415 mm` (adjust for your machine)
7. Click **Finish**

To enable disconnect-safe job buffering (Phase 2+), add these under **Edit → Device Settings → Additional Settings**:

- **Start G-code**: `G4 P0.0`
- **End G-code**: `G4 P0.0`

The proxy uses this dwell command as a job boundary marker. It is harmless to GRBL and does not move the laser.

## Development

### Install with dev dependencies

```bash
pip install -e ".[dev]"
```

### Run tests

```bash
pytest
```

All tests run without hardware using an in-process GRBL mock — no serial port required.

```
99 passed in 3.18s
```

### CLI options

```
grbl-proxy --help

usage: grbl-proxy [-h] [--config CONFIG] [--debug]

GRBL Laser Proxy

options:
  -h, --help            show this help message and exit
  --config, -c CONFIG   Path to config.yaml (default: ~/.grbl-proxy/config.yaml)
  --debug, -d           Enable debug logging
```

## Project structure

```
grbl-proxy/
├── src/grbl_proxy/
│   ├── main.py           # Entry point, CLI, wiring
│   ├── config.py         # YAML config loading
│   ├── serial_conn.py    # Serial port management + auto-reconnect
│   ├── grbl_protocol.py  # GRBL 1.1 message parser
│   ├── tcp_server.py     # TCP server + bidirectional relay
│   ├── proxy_core.py     # State machine: Passthrough/Buffering/Executing/Paused/Error
│   ├── job_buffer.py     # Disk-based G-code buffer
│   └── streamer.py       # Character-counting GRBL streamer
├── tests/
│   ├── mock_grbl.py      # In-process GRBL mock for testing
│   ├── test_phase1.py    # Passthrough relay tests
│   ├── test_phase2.py    # Job detection and buffering tests
│   └── test_phase3.py    # Streamer and execution tests
├── systemd/
│   └── grbl-proxy.service
└── config.yaml.example
```

## Troubleshooting

**Serial port not found**
Run `ls /dev/ttyUSB* /dev/ttyACM*` with the laser connected. Set the result explicitly in `serial.port`.

**Permission denied on serial port**
Ensure your user is in the `dialout` group: `sudo usermod -aG dialout $USER` then log out/in.

**LightBurn shows "disconnected" immediately**
Check that the proxy is running and listening: `sudo ss -tlnp | grep 8899`. Verify the Pi's IP and port in the LightBurn device settings.

**Laser resets every time the proxy starts**
This means DTR is not being disabled. Ensure `serial.dtr: false` is set in your config — this is critical for ESP32-S2 based controllers.

**Proxy loses connection to laser mid-job**
The reconnect loop will restore the connection automatically within `serial.reconnect_interval` seconds (default 5s). Check `journalctl -u grbl-proxy -f` for reconnect events.

## Roadmap

### Phase 1 ✅ — Transparent passthrough relay

All LightBurn commands pass through to GRBL unmodified and all GRBL responses flow back. Jogging, homing, framing, laser framing, and console commands work exactly as if LightBurn were connected directly. Serial reconnect runs in the background — if the USB cable drops, the proxy reconnects automatically and LightBurn never notices. A single TCP client is enforced: if LightBurn reconnects, the previous socket is cleanly replaced.

### Phase 2 ✅ — Job detection and disk buffering

When LightBurn starts a job, the proxy intercepts the G-code stream and writes it to a file on disk before any line reaches the laser. Job boundaries are identified by a configurable start marker (default `G4 P0.0`) sent from LightBurn's device start G-code, and an end marker or terminal G-code command (`M2`/`M30`). While buffering, every LightBurn line gets a synthetic `ok` reply so LightBurn's internal send queue drains normally. Status queries (`?`) return a synthetic `<Run|...>` response so LightBurn displays a running job. If LightBurn disconnects mid-buffer, the incomplete file is discarded. An idle timeout finalises the buffer if the end marker never arrives.

**LightBurn device setup for Phase 2:**
Add these to the device's G-code start/end sequence under **Edit → Device Settings → Additional Settings**:
- **Start G-code**: `G4 P0.0`
- **End G-code**: `G4 P0.0`

### Phase 3 ✅ — Character-counting GRBL streamer (disconnect-safe execution)

Once a job is fully buffered, the proxy takes ownership of the serial port and streams the G-code file directly to GRBL using the character-counting flow-control protocol (GRBL's 128-byte RX buffer). LightBurn plays no further role in execution — the job runs to completion whether or not LightBurn stays connected.

**Behaviour during execution:**
- LightBurn can disconnect and reconnect freely — the job is unaffected.
- Status queries (`?`) return a synthetic `<Run|...>` response derived from the last known machine position.
- Interactive commands (`$$`, jog moves, etc.) are rejected with `error:9` (busy).
- Feed hold (`!`) pauses the streamer; cycle resume (`~`) continues it. Both are forwarded to GRBL.
- Soft reset (`Ctrl-X`) cancels the job and transitions to Error state.
- On a GRBL `error:N` or `ALARM:N` response, execution stops and the proxy enters Error state.
- In Error state all commands are rejected with `error:9` until the operator sends `$X` (alarm clear) or `$H` (re-home), which forwards the command to GRBL and returns the proxy to Passthrough.
- On successful completion the proxy returns silently to Passthrough, ready for the next job.

### Phase 4 — Web dashboard _(planned)_

A lightweight browser UI served from the Pi, providing:

- Live machine state, position, feed rate, and job progress (lines sent / total)
- Pause, resume, and cancel controls that work independently of LightBurn
- REST API (`/api/status`, `/api/job`, `/api/job/pause`, `/api/job/resume`, `/api/job/cancel`, `/api/console`)
- Ability to upload a G-code file and start a job directly without LightBurn
- Recent console log (last N GRBL messages)

### Phase 5 — Polish and observability _(planned)_

- Job history: completed job log with filename, duration, line count, and outcome
- Alarm recovery workflow: guided `$X` / `$H` from the dashboard after a fault
- Webcam integration: optional MJPEG stream from a Pi camera or USB webcam embedded in the dashboard
- Proxy configuration UI: edit `config.yaml` settings from the browser

## License

MIT
