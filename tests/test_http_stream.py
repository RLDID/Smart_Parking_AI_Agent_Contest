"""Real HTTP/SSE integration on an isolated loopback port and temporary database."""
from contextlib import contextmanager
import json
import socket
import threading
import time

import httpx
import uvicorn

from backend.app import Settings, create_app


@contextmanager
def server(tmp_path):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    address = f"http://127.0.0.1:{sock.getsockname()[1]}"
    app = create_app(Settings(database=tmp_path/"network.sqlite3", test_control=True,
                              origins=(address,)))
    service = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False,
                                          proxy_headers=False))
    thread = threading.Thread(target=service.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not service.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert service.started
        yield address
    finally:
        service.should_exit = True
        thread.join(timeout=5)
        sock.close()
        assert not thread.is_alive(), "HTTP test server did not stop"


def next_event(lines):
    for line in lines:
        if line.startswith("data: "):
            return json.loads(line[6:])
    return None


def test_real_sse_resume_run_reset_and_logout(tmp_path):
    with server(tmp_path) as address, httpx.Client(base_url=address, timeout=5) as client:
        auth = client.post("/api/v1/auth/session", headers={"Origin": address},
                           json={"username": "demo-operator", "password": "parking-demo-only"})
        assert auth.status_code == 200
        csrf = client.get("/api/v1/me").json()["csrf_token"]
        headers = {"Origin": address, "X-CSRF-Token": csrf}
        body = {"facility_id": "fac-demo-01", "fixture_ref": "s1a-foundation-v1",
                "seed": 1, "config_ref": "foundation-v1"}
        created = client.post("/api/v1/test/runs", json=body,
                              headers={**headers, "Idempotency-Key": "run"}).json()
        run_id = created["run_id"]
        path = f"/api/v1/facilities/fac-demo-01/events?run_id={run_id}"
        with client.stream("GET", path) as stream:
            assert stream.status_code == 200
            assert stream.headers["content-type"].startswith("text/event-stream")
            lines = stream.iter_lines()
            first = next_event(lines)
            assert first["type"] == "state.snapshot"
            stepped = client.post(f"/api/v1/test/runs/{run_id}/control", json={"action": "step"},
                                  headers={**headers, "Idempotency-Key": "step"})
            assert stepped.status_code == 200
            change = next_event(lines)
            assert change["event_id"] != first["event_id"]
            assert change["payload"]["applied_sim_time_ms"] == 100
        with client.stream("GET", path, headers={"Last-Event-ID": first["event_id"]}) as stream:
            assert next_event(stream.iter_lines())["event_id"] == change["event_id"]
        # A disconnect after reset but before snapshot must not acknowledge the
        # snapshot's cursor. EventSource uses an empty id to clear Last-Event-ID.
        last_id = "expired"
        with client.stream("GET", path, headers={"Last-Event-ID": last_id}) as stream:
            for line in stream.iter_lines():
                if line.startswith("id:"):
                    last_id = line[3:].strip()
                if line.startswith("data:"):
                    assert json.loads(line[5:])["type"] == "reset_required"
                    break
        assert last_id == ""
        with client.stream("GET", path) as stream:
            assert next_event(stream.iter_lines())["type"] == "state.snapshot"
        with client.stream("GET", path, headers={"Last-Event-ID": "expired"}) as stream:
            lines = stream.iter_lines()
            assert next_event(lines)["type"] == "reset_required"
            assert next_event(lines)["type"] == "state.snapshot"
            new_run = client.post("/api/v1/test/runs", json=body,
                                  headers={**headers, "Idempotency-Key": "new-run"}).json()
            reset = next_event(lines)
            assert reset["type"] == "reset_required" and reset["run_id"] == new_run["run_id"]
            assert next_event(lines)["type"] == "state.snapshot"
            assert client.delete("/api/v1/auth/session", headers=headers).status_code == 204
            assert next_event(lines) == {"reason": "access_changed_or_unavailable"}
            assert next_event(lines) is None  # Server closes the already-open stream.
