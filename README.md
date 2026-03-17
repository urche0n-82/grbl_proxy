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
git clone https://github.com/your-repo/grbl-proxy.git ~/grbl-proxy
cd ~/grbl-proxy
```

### 2. Create a virtual environment and install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 3. Set up configuration

```bash
mkdir -p ~/.grbl-proxy
cp config.yaml.example ~/.grbl-proxy/config.yaml
nano ~/.grbl-proxy/config.yaml
```

The defaults work for most setups. The only field you may need to change is `serial.port` if auto-detection picks the wrong device:

```yaml
serial:
  port: auto        # or /dev/ttyUSB0, /dev/ttyACM0, etc.
  baud: 115200
  dtr: false        # CRITICAL: keeps ESP32-S2 from resetting on connect

tcp:
  host: 0.0.0.0
  port: 8899        # port LightBurn will connect to
```

### 4. Add your user to the `dialout` group (for serial port access)

```bash
sudo usermod -aG dialout $USER
# Log out and back in for this to take effect
```

## Running

### Manually (for testing)

```bash
source .venv/bin/activate
grbl-proxy --debug
```

You should see output like:

```
2024-01-15T10:23:01 INFO     grbl_proxy.main: Starting grbl-proxy
2024-01-15T10:23:01 INFO     grbl_proxy.main:   Serial port : /dev/ttyUSB0 @ 115200 baud
2024-01-15T10:23:01 INFO     grbl_proxy.main:   TCP server  : 0.0.0.0:8899
2024-01-15T10:23:03 INFO     grbl_proxy.serial_conn: Serial port /dev/ttyUSB0 opened at 115200 baud
2024-01-15T10:23:03 INFO     grbl_proxy.tcp_server: TCP server listening on [('0.0.0.0', 8899)]
2024-01-15T10:23:03 INFO     grbl_proxy.main: grbl-proxy running. Press Ctrl-C to stop.
```

Test the connection with `nc` (netcat):

```bash
nc <pi-ip> 8899
```

Type `?` and press Enter — you should get a GRBL status response like `<Idle|MPos:0.000,0.000,0.000|FS:0,0>`.

### As a systemd service (auto-start on boot)

```bash
sudo cp systemd/grbl-proxy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable grbl-proxy
sudo systemctl start grbl-proxy
```

Check status and logs:

```bash
sudo systemctl status grbl-proxy
journalctl -u grbl-proxy -f
```

## LightBurn device setup

1. Open LightBurn → **Devices** → **Create Manually**
2. Select **GRBL**
3. Connection type: **Ethernet/TCP**
4. IP address: your Pi's static IP on the local network
5. Port: `8899` (or `23` if configured)
6. Work area: `400 × 415 mm` (adjust for your machine)
7. Click **Finish**

To enable disconnect-safe job buffering (Phase 2+), add these to the device's G-code preamble/postamble:

- **Start G-code**: `; PROXY_JOB_START`
- **End G-code**: `; PROXY_JOB_END`

## Port 23 (LightBurn default)

LightBurn defaults to port 23 for GRBL-over-TCP. Port 23 requires root or a Linux capability grant. The easiest approach:

```bash
sudo setcap 'cap_net_bind_service=+ep' $(readlink -f .venv/bin/python3)
```

Then set `tcp.port: 23` in your config. Alternatively, stay on port 8899 and set LightBurn to match.

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
35 passed in 0.22s
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
│   └── tcp_server.py     # TCP server + bidirectional relay
├── tests/
│   ├── mock_grbl.py      # In-process GRBL mock for testing
│   └── test_phase1.py    # Test suite (no hardware needed)
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

- **Phase 1** ✅ — Transparent TCP↔serial passthrough relay
- **Phase 2** — Job detection and disk-based buffering
- **Phase 3** — Character-counting GRBL streamer (disconnect-safe execution)
- **Phase 4** — Web dashboard with real-time progress and pause/resume/cancel
- **Phase 5** — Polish: alarm recovery, job history, webcam integration

## License

MIT
