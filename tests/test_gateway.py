"""IB Gateway manager: detection, auto-launch, install guards — no gateway,
no network, nothing actually launched."""

import socket
from pathlib import Path

from fastapi.testclient import TestClient

import stock_trader.gateway as gw
from stock_trader.gateway import GatewayManager, port_open
from stock_trader.server import create_app

from conftest import clean_settings


def test_port_open_detects_listener_and_closed_port():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    host, port = srv.getsockname()
    try:
        assert port_open(host, port)
    finally:
        srv.close()
    assert not port_open("127.0.0.1", port)


def test_status_shape(monkeypatch):
    monkeypatch.setattr(gw, "find_gateway", lambda: Path("/fake/ibgateway"))
    monkeypatch.setattr(gw, "port_open", lambda *a, **k: False)
    st = GatewayManager(clean_settings(provider="ibkr")).status()
    assert st["installed"] and not st["running"] and st["port"] == 4002
    assert "login_help" in st and not st["installing"]


def test_start_refuses_when_not_installed(monkeypatch):
    launched = []
    monkeypatch.setattr(gw, "find_gateway", lambda: None)
    monkeypatch.setattr(gw, "port_open", lambda *a, **k: False)
    monkeypatch.setattr(gw, "launch", lambda p: launched.append(p))
    out = GatewayManager(clean_settings(provider="ibkr")).start()
    assert "not installed" in out["message"] and launched == []


def test_start_launches_when_installed(monkeypatch):
    launched = []
    monkeypatch.setattr(gw, "find_gateway", lambda: Path("/fake/ibgateway"))
    monkeypatch.setattr(gw, "port_open", lambda *a, **k: False)
    monkeypatch.setattr(gw, "launch", lambda p: launched.append(p))
    out = GatewayManager(clean_settings(provider="ibkr")).start()
    assert launched == [Path("/fake/ibgateway")]
    assert "login window" in out["message"]


def test_ensure_launches_once_and_only_for_ibkr(monkeypatch):
    launched = []
    monkeypatch.setattr(gw, "find_gateway", lambda: Path("/fake/ibgateway"))
    monkeypatch.setattr(gw, "port_open", lambda *a, **k: False)
    monkeypatch.setattr(gw, "launch", lambda p: launched.append(p))

    GatewayManager(clean_settings(provider="sim")).ensure()
    assert launched == []                       # non-IBKR providers: no-op

    mgr = GatewayManager(clean_settings(provider="ibkr"))
    mgr.ensure()
    mgr.ensure()                                # closed login window is not
    assert len(launched) == 1                   # re-opened forever

    # already running -> no launch
    monkeypatch.setattr(gw, "port_open", lambda *a, **k: True)
    GatewayManager(clean_settings(provider="ibkr")).ensure()
    assert len(launched) == 1


def test_install_refuses_when_already_installed(monkeypatch):
    monkeypatch.setattr(gw, "find_gateway", lambda: Path("/fake/ibgateway"))
    monkeypatch.setattr(gw, "port_open", lambda *a, **k: False)
    out = GatewayManager(clean_settings(provider="ibkr")).install()
    assert "already installed" in out["message"]


def test_gateway_api_endpoints(monkeypatch):
    monkeypatch.setattr(gw, "find_gateway", lambda: None)
    monkeypatch.setattr(gw, "port_open", lambda *a, **k: False)
    client = TestClient(create_app(clean_settings(provider="ibkr")),
                        raise_server_exceptions=False)
    g = client.get("/api/gateway").json()
    assert g["installed"] is False and g["running"] is False
    assert client.get("/api/status").json()["gateway"]["running"] is False
    r = client.post("/api/gateway/start")
    assert "not installed" in r.json()["message"]
    # non-IBKR providers report no gateway block in status
    c2 = TestClient(create_app(clean_settings(provider="sim")),
                    raise_server_exceptions=False)
    assert c2.get("/api/status").json()["gateway"] is None
