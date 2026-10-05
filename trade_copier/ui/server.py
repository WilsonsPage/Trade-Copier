"""Local control panel: a small web server on 127.0.0.1 that edits config.yaml
and starts/stops the copier.

    python -m trade_copier ui        (then open http://127.0.0.1:8765)

It only listens on this computer and rejects requests from other web sites,
because it can change your trading settings and start the copier.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

import yaml

from ..config import ConfigError, load_config, parse_config
from ..status import clear_statuses, read_statuses

STATIC = Path(__file__).parent / "static"
EXAMPLE = Path(__file__).resolve().parents[2] / "config.example.yaml"  # repo root
STATUS_FRESH_SECONDS = 10.0
PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def _child_env() -> dict:
    """Environment for child processes, able to import trade_copier from any working folder."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(PACKAGE_ROOT), env.get("PYTHONPATH")]))
    env["PYTHONIOENCODING"] = "utf-8"
    return env


YAML_HEADER = (
    "# Trade Copier configuration, saved by the control panel.\n"
    "# See config.example.yaml for an explanation of every setting.\n"
)


class CopierProcess:
    """The copier, run as a child process so the panel can start and stop it."""

    def __init__(self, config_path: Path, workdir: Path):
        self.config_path = config_path
        self.workdir = workdir
        self.proc: Optional[subprocess.Popen] = None
        self.output: deque = deque(maxlen=400)
        self.started_at = 0.0
        self.lock = threading.Lock()

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        with self.lock:
            if self.running():
                return
            self.output.clear()
            cmd = [sys.executable, "-u", "-m", "trade_copier", "run", "-c", str(self.config_path)]
            kwargs = {}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            self.proc = subprocess.Popen(
                cmd, cwd=self.workdir, env=_child_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", **kwargs,
            )
            self.started_at = time.time()
            threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            self.output.append(line.rstrip("\n"))

    def stop(self, timeout: float = 20.0) -> None:
        with self.lock:
            proc = self.proc
            if proc is None or proc.poll() is not None:
                return
            try:
                if os.name == "nt":
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    os.killpg(proc.pid, signal.SIGINT)
                proc.wait(timeout=timeout)
            except (subprocess.TimeoutExpired, OSError):
                proc.kill()
                proc.wait(timeout=5)

    def info(self) -> dict:
        if self.proc is None:
            return {"running": False, "exit_code": None}
        code = self.proc.poll()
        return {"running": code is None, "exit_code": code, "pid": self.proc.pid, "started_at": self.started_at}


class Panel:
    def __init__(self, config_path: Path, workdir: Path):
        self.config_path = config_path
        self.workdir = workdir
        self.copier = CopierProcess(config_path, workdir)

    # -- config -------------------------------------------------------
    def read_raw(self) -> tuple:
        path = self.config_path if self.config_path.exists() else EXAMPLE
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        return raw, self.config_path.exists()

    def settings(self) -> dict:
        try:
            raw, _ = self.read_raw()
            s = raw.get("settings") or {}
        except (OSError, yaml.YAMLError):
            s = {}
        return {"state_dir": s.get("state_dir", "state"), "log_dir": s.get("log_dir", "logs")}

    def save(self, raw: dict) -> None:
        parse_config(raw, expand_env=False)  # raises ConfigError with a readable message
        if self.config_path.exists():
            shutil.copyfile(self.config_path, self.config_path.with_suffix(self.config_path.suffix + ".bak"))
        tmp = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        tmp.write_text(YAML_HEADER + yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
        os.replace(tmp, self.config_path)

    # -- status -------------------------------------------------------
    def path(self, p: str) -> Path:
        q = Path(p)
        return q if q.is_absolute() else self.workdir / q

    def status(self) -> dict:
        s = self.settings()
        now = time.time()
        accounts = []
        for st in read_statuses(str(self.path(s["state_dir"]))):
            st["fresh"] = now - st.get("updated", 0) < STATUS_FRESH_SECONDS
            accounts.append(st)
        proc = self.copier.info()
        elsewhere = not proc["running"] and any(a["fresh"] and a.get("state") != "stopped" for a in accounts)
        return {
            "process": proc,
            "running_elsewhere": elsewhere,
            "accounts": accounts,
            "log": self.log_lines(s["log_dir"]),
            "output": list(self.copier.output)[-60:],
        }

    def log_lines(self, log_dir: str, limit: int = 300) -> list:
        if not log_dir:
            return list(self.copier.output)[-limit:]
        folder = self.path(log_dir)
        lines = []
        if folder.is_dir():
            for f in folder.glob("*.log"):
                try:
                    with f.open("r", encoding="utf-8", errors="replace") as fh:
                        lines.extend(deque(fh, maxlen=limit))
                except OSError:
                    continue
        lines = [ln.rstrip("\n") for ln in lines if ln[:4].isdigit()]
        lines.sort(key=lambda ln: ln[:23])
        return lines[-limit:]

    def start(self) -> None:
        if self.copier.running():
            return
        if not self.config_path.exists():
            raise ConfigError("save your settings first")
        load_config(self.config_path)  # full check, including password environment variables
        clear_statuses(str(self.path(self.settings()["state_dir"])))
        self.copier.start()

    def check(self) -> str:
        if self.copier.running():
            raise ConfigError("stop the copier before running the connection check")
        if not self.config_path.exists():
            raise ConfigError("save your settings first")
        res = subprocess.run(
            [sys.executable, "-m", "trade_copier", "check", "-c", str(self.config_path)],
            cwd=self.workdir, env=_child_env(), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
        )
        return (res.stdout + res.stderr).strip()


def make_handler(panel: Panel, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "TradeCopier"

        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        def _send(self, code: int, body, ctype: str = "application/json") -> None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _host_ok(self) -> bool:
            # Blocks DNS-rebinding: only answer requests addressed to this machine.
            return self.headers.get("Host", "") in allowed_hosts

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, {"error": "forbidden"})
            if self.path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if self.path == "/api/config":
                try:
                    raw, exists = panel.read_raw()
                except (OSError, yaml.YAMLError) as exc:
                    return self._send(500, {"error": f"could not read config: {exc}"})
                return self._send(200, {"config": raw, "exists": exists, "path": str(panel.config_path)})
            if self.path == "/api/status":
                return self._send(200, panel.status())
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._host_ok():
                return self._send(403, {"error": "forbidden"})
            # Other web sites can't send JSON here without a CORS preflight, which we never approve.
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._send(415, {"error": "expected JSON"})
            origin = self.headers.get("Origin")
            if origin and origin.split("://", 1)[-1] not in allowed_hosts:
                return self._send(403, {"error": "forbidden"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._send(400, {"error": "invalid JSON"})
            try:
                if self.path == "/api/config":
                    panel.save(body.get("config"))
                    return self._send(200, {"ok": True, "running": panel.copier.running()})
                if self.path == "/api/validate":
                    parse_config(body.get("config"), expand_env=False)
                    return self._send(200, {"ok": True})
                if self.path == "/api/start":
                    panel.start()
                    return self._send(200, {"ok": True})
                if self.path == "/api/stop":
                    panel.copier.stop()
                    return self._send(200, {"ok": True})
                if self.path == "/api/restart":
                    panel.copier.stop()
                    panel.start()
                    return self._send(200, {"ok": True})
                if self.path == "/api/check":
                    return self._send(200, {"ok": True, "output": panel.check()})
            except ConfigError as exc:
                return self._send(400, {"error": str(exc)})
            except subprocess.TimeoutExpired:
                return self._send(504, {"error": "the check took too long; is a terminal stuck on a login prompt?"})
            return self._send(404, {"error": "not found"})

    return Handler


def serve(config_path: str, port: int = 8765, open_browser: bool = True) -> None:
    workdir = Path.cwd()
    panel = Panel(Path(config_path).resolve(), workdir)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(panel, port))
    url = f"http://127.0.0.1:{port}"
    print(f"Trade Copier control panel: {url}")
    print("Keep this window open while you use the panel. Press Ctrl+C to quit.")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("Shutting down...")
        panel.copier.stop()
        server.server_close()
