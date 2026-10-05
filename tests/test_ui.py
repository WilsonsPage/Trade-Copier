import http.client
import json
import os
import threading
import time
from http.server import ThreadingHTTPServer

import pytest
import yaml

from trade_copier.ui.server import Panel, make_handler

VALID = {
    "master": {"name": "master", "terminal_path": "C:/MT5/M/terminal64.exe", "password": "${PW}"},
    "slaves": [{"name": "big", "terminal_path": "C:/MT5/A/terminal64.exe",
                "lots": {"mode": "multiplier", "multiplier": 0.5}, "reverse": True}],
    "settings": {"state_dir": "state", "log_dir": "logs"},
}


@pytest.fixture
def panel(tmp_path):
    p = Panel(tmp_path / "config.yaml", tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = server.server_address[1]
    server.RequestHandlerClass = make_handler(p, port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield p, port
    p.copier.stop()
    server.shutdown()
    server.server_close()


def call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    h = {"Host": f"127.0.0.1:{port}"}
    data = None
    if body is not None:
        data = json.dumps(body)
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    conn.request(method, path, body=data, headers=h)
    res = conn.getresponse()
    raw = res.read()
    try:
        return res.status, json.loads(raw)
    except ValueError:
        return res.status, raw.decode()


def test_serves_page_and_example_config_when_none_saved(panel):
    p, port = panel
    status, html = call(port, "GET", "/")
    assert status == 200 and "Trade Copier" in html
    status, data = call(port, "GET", "/api/config")
    assert status == 200 and data["exists"] is False
    assert data["config"]["master"]["name"] == "master"


def test_save_writes_yaml_and_keeps_env_references(panel):
    p, port = panel
    status, data = call(port, "POST", "/api/config", {"config": VALID})
    assert status == 200, data
    saved = yaml.safe_load(p.config_path.read_text())
    assert saved["master"]["password"] == "${PW}"
    assert saved["slaves"][0]["reverse"] is True
    status, data = call(port, "POST", "/api/config", {"config": VALID})
    assert (p.config_path.parent / "config.yaml.bak").exists()


def test_invalid_config_is_rejected_with_reason(panel):
    p, port = panel
    bad = json.loads(json.dumps(VALID))
    bad["slaves"][0]["lots"]["mode"] = "nonsense"
    status, data = call(port, "POST", "/api/config", {"config": bad})
    assert status == 400 and "lots.mode" in data["error"]
    assert not p.config_path.exists()


def test_rejects_other_hosts_and_non_json(panel):
    p, port = panel
    assert call(port, "GET", "/api/config", headers={"Host": "evil.example:80"})[0] == 403
    status, _ = call(port, "POST", "/api/stop", headers={"Content-Type": "text/plain"})
    assert status == 415
    status, _ = call(port, "POST", "/api/stop", {}, headers={"Origin": "http://evil.example"})
    assert status == 403


def test_start_requires_saved_config_and_passwords(panel, monkeypatch):
    p, port = panel
    status, data = call(port, "POST", "/api/start", {})
    assert status == 400 and "save" in data["error"]
    call(port, "POST", "/api/config", {"config": VALID})
    monkeypatch.delenv("PW", raising=False)
    status, data = call(port, "POST", "/api/start", {})
    assert status == 400 and "PW" in data["error"]


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX signals to stop the child")
def test_start_and_stop_copier_process(panel, monkeypatch):
    p, port = panel
    monkeypatch.setenv("PW", "x")
    call(port, "POST", "/api/config", {"config": VALID})
    status, data = call(port, "POST", "/api/start", {})
    assert status == 200, data
    deadline = time.time() + 15
    while time.time() < deadline:
        st = call(port, "GET", "/api/status")[1]
        if any(a.get("role") == "master" for a in st["accounts"]):
            break
        time.sleep(0.2)
    assert st["process"]["running"]
    assert {a["name"] for a in st["accounts"]} >= {"master"}
    status, _ = call(port, "POST", "/api/stop", {})
    st = call(port, "GET", "/api/status")[1]
    assert st["process"]["running"] is False
    assert st["process"]["exit_code"] == 0
