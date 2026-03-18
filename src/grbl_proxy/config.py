"""Configuration loading and validation for grbl-proxy."""

from __future__ import annotations

import glob
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("~/.grbl-proxy/config.yaml").expanduser()


@dataclass
class SerialConfig:
    port: str = "auto"
    baud: int = 115200
    dtr: bool = False
    reconnect_interval: float = 5.0


@dataclass
class TcpConfig:
    host: str = "0.0.0.0"
    port: int = 8899


@dataclass
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass
class AutoDetectConfig:
    enabled: bool = False
    line_burst: int = 10
    window_ms: int = 500
    motion_ratio: float = 0.8


@dataclass
class JobConfig:
    storage_dir: str = "~/.grbl-proxy/jobs"
    max_history: int = 20
    start_marker: str = "G4 P0.0"
    end_marker: str = "G4 P0.0"
    auto_detect: AutoDetectConfig = field(default_factory=AutoDetectConfig)


@dataclass
class MachineConfig:
    name: str = "GRBL Machine"
    work_area: list = field(default_factory=lambda: [400, 415])
    status_poll_hz: int = 4


@dataclass
class Config:
    serial: SerialConfig = field(default_factory=SerialConfig)
    tcp: TcpConfig = field(default_factory=TcpConfig)
    web: WebConfig = field(default_factory=WebConfig)
    job: JobConfig = field(default_factory=JobConfig)
    machine: MachineConfig = field(default_factory=MachineConfig)


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base, returning a new dict."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _dict_to_config(data: dict) -> Config:
    serial_data = data.get("serial", {})
    tcp_data = data.get("tcp", {})
    web_data = data.get("web", {})
    job_data = data.get("job", {})
    machine_data = data.get("machine", {})

    auto_detect_data = job_data.pop("auto_detect", {})
    auto_detect = AutoDetectConfig(**{k: v for k, v in auto_detect_data.items()
                                      if k in AutoDetectConfig.__dataclass_fields__})

    return Config(
        serial=SerialConfig(**{k: v for k, v in serial_data.items()
                               if k in SerialConfig.__dataclass_fields__}),
        tcp=TcpConfig(**{k: v for k, v in tcp_data.items()
                         if k in TcpConfig.__dataclass_fields__}),
        web=WebConfig(**{k: v for k, v in web_data.items()
                         if k in WebConfig.__dataclass_fields__}),
        job=JobConfig(
            **{k: v for k, v in job_data.items()
               if k in JobConfig.__dataclass_fields__ and k != "auto_detect"},
            auto_detect=auto_detect,
        ),
        machine=MachineConfig(**{k: v for k, v in machine_data.items()
                                 if k in MachineConfig.__dataclass_fields__}),
    )


def load_config(path: Path | None = None) -> Config:
    """Load config from YAML file, falling back to defaults for missing keys."""
    config_path = path or DEFAULT_CONFIG_PATH

    if not config_path.exists():
        logger.info("No config file found at %s, using defaults", config_path)
        return Config()

    try:
        with open(config_path) as f:
            user_data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML in config file {config_path}: {e}") from e
    except OSError as e:
        raise ValueError(f"Cannot read config file {config_path}: {e}") from e

    return _dict_to_config(user_data)


def resolve_serial_port(config: SerialConfig) -> str:
    """Resolve 'auto' serial port to an actual device path."""
    if config.port != "auto":
        return config.port

    candidates = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))

    if len(candidates) == 1:
        logger.info("Auto-detected serial port: %s", candidates[0])
        return candidates[0]

    if len(candidates) > 1:
        chosen = candidates[-1]
        logger.warning(
            "Multiple serial ports found %s, using %s. Set serial.port explicitly to override.",
            candidates,
            chosen,
        )
        return chosen

    fallback = "/dev/ttyUSB0"
    logger.warning("No serial ports found via auto-detect, falling back to %s", fallback)
    return fallback
