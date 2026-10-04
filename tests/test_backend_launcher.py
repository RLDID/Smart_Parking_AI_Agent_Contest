"""CLI opt-in wiring without binding a socket or starting a runtime."""
import os
from pathlib import Path
import runpy
import sys

import pytest
import uvicorn


LAUNCHER = Path(__file__).resolve().parents[1] / "scripts/run_backend.py"


@pytest.mark.parametrize("demo_origin", [None, "https://team-demo.invalid"])
def test_launcher_keeps_loopback_transport_for_demo(monkeypatch, demo_origin):
    for name in ("TEST_CONTROL_ENABLED", "PARKING_TEAM_DEMO_ORIGIN", "PARKING_LOCAL_PORT"):
        monkeypatch.delenv(name, raising=False)
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    argv = [str(LAUNCHER), "--dev-controls", "--port", "18080"]
    if demo_origin:
        argv += ["--team-demo-origin", demo_origin]
    monkeypatch.setattr(sys, "argv", argv)
    runpy.run_path(str(LAUNCHER), run_name="__main__")
    assert os.environ.get("PARKING_TEAM_DEMO_ORIGIN") == demo_origin
    assert os.environ["TEST_CONTROL_ENABLED"] == "true"
    assert os.environ["PARKING_LOCAL_PORT"] == "18080"
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ("backend.app:create_app",)
    assert kwargs["factory"] is True
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 18080
    assert kwargs["workers"] == 1
    assert kwargs["proxy_headers"] is False


def test_launcher_requires_explicit_demo_controls(monkeypatch):
    monkeypatch.delenv("PARKING_TEAM_DEMO_ORIGIN", raising=False)
    monkeypatch.setattr(sys, "argv", [str(LAUNCHER), "--team-demo-origin", "https://team-demo.invalid"])
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: pytest.fail("server must not start"))
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(LAUNCHER), run_name="__main__")
    assert exc.value.code == 2
    assert "PARKING_TEAM_DEMO_ORIGIN" not in os.environ
