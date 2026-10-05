"""Small JSON status files the copier writes so the control panel can show live state."""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path


def status_dir(state_dir: str) -> Path:
    return Path(state_dir) / "status"


def write_status(state_dir: str, name: str, data: dict) -> None:
    folder = status_dir(state_dir)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({**data, "name": name, "updated": time.time()}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass  # status is best effort; never let it disturb copying


def read_statuses(state_dir: str) -> list:
    folder = status_dir(state_dir)
    out = []
    if folder.is_dir():
        for path in sorted(folder.glob("*.json")):
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
    return out


def clear_statuses(state_dir: str) -> None:
    folder = status_dir(state_dir)
    if folder.is_dir():
        for path in folder.glob("*.json"):
            try:
                path.unlink()
            except OSError:
                pass


def _raise_interrupt(*_):
    raise KeyboardInterrupt


def install_stop_signals() -> None:
    """Treat Ctrl+Break (sent by the control panel on Windows) and SIGTERM like Ctrl+C."""
    for name in ("SIGBREAK", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _raise_interrupt)
            except (ValueError, OSError):
                pass
