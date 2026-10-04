"""Opt-in HTTPS origin policy with loopback peers; no TLS/proxy deployment."""
import json
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
import pytest

from backend.app import COOKIE, Settings, create_app
from simulator.world import FACILITY

ORIGIN = "https://team-demo.invalid"
LOCAL_ORIGIN = "http://127.0.0.1:8000"
LOGIN = {"username": "demo-operator", "password": "parking-demo-only"}
RUN = {"facility_id": FACILITY, "fixture_ref": "s1a-foundation-v1", "seed": 42,
       "config_ref": "foundation-v1"}


@pytest.fixture
def demo(tmp_path):
    app = create_app(Settings(database=tmp_path / "demo.sqlite3", test_control=True,
                              team_demo_origin=ORIGIN, background_ticks=False))
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 41000)) as client:
        yield client, app


def login(client, username="demo-operator"):
    response = client.post("/api/v1/auth/session", headers={"Origin": ORIGIN},
                           json={**LOGIN, "username": username})
    assert response.status_code == 200, response.text
    me = client.get("/api/v1/me")
    assert me.status_code == 200
    return response, {"Origin": ORIGIN, "X-CSRF-Token": me.json()["csrf_token"]}


def cookie_attributes(response, *, secure):
    cookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    morsel = cookie[COOKIE]
    assert bool(morsel["secure"]) is secure
    assert morsel["httponly"] and morsel["samesite"].lower() == "strict"
    return morsel


@pytest.mark.parametrize("invalid", [
    "", " ", " https://team-demo.invalid", "https://team-demo.invalid ",
    "http://team-demo.invalid", "//team-demo.invalid", "https://", "https://team-demo.invalid/",
    "https://team-demo.invalid/path", "https://team-demo.invalid?", "https://team-demo.invalid?x=1",
    "https://team-demo.invalid#", "https://team-demo.invalid#section", "https://user@team-demo.invalid",
    "https://user:password@team-demo.invalid", "https://*.invalid", "https://team_demo.invalid",
    "https://.invalid", "https://team..invalid", "https://team-demo.invalid.",
    "https://-team.invalid", "https://team-.invalid", "https://" + "a" * 64 + ".invalid",
    "https://" + ".".join(["a" * 63] * 4), "https://팀.invalid", "https://team%2edemo.invalid",
    "https://team-demo.invalid\\path", "https://team-demo.invalid\n", "https://team-demo.invalid\t",
    "https://127.0.0.1", "https://127.1", "https://2130706433", "https://0x7f000001",
    "https://[::1]", "https://team-demo.invalid:", "https://team-demo.invalid:0",
    "https://team-demo.invalid:65536", "https://team-demo.invalid:-1", "https://team-demo.invalid:+443",
    "https://team-demo.invalid:4.43", "https://team-demo.invalid:123456", "https://team-demo.invalid:abc", 123,
])
def test_invalid_origin_fails_before_runtime(tmp_path, invalid):
    database = tmp_path / "never-opened.sqlite3"
    with pytest.raises(ValueError, match="HTTPS DNS origin"):
        create_app(Settings(database=database, test_control=True, team_demo_origin=invalid))
    assert not database.exists()


@pytest.mark.parametrize("input_origin,serialized", [
    (ORIGIN, ORIGIN), ("HTTPS://TEAM-DEMO.INVALID:443", ORIGIN),
    ("https://team-demo.invalid:0443", ORIGIN), ("https://team-demo.invalid:8443", ORIGIN + ":8443"),
    ("https://team-demo.invalid:1", ORIGIN + ":1"), ("https://team-demo.invalid:65535", ORIGIN + ":65535"),
    ("https://a", "https://a"), ("https://xn--bcher-kva.invalid", "https://xn--bcher-kva.invalid"),
])
def test_origin_serialization_and_exact_port(tmp_path, input_origin, serialized):
    settings = Settings(database=tmp_path / "port.sqlite3", test_control=True,
                        team_demo_origin=input_origin, background_ticks=False)
    assert settings.team_demo_origin == serialized
    with TestClient(create_app(settings), base_url=serialized, client=("127.0.0.1", 41000)) as client:
        assert client.get("/health/live").status_code == 200
        assert client.post("/api/v1/auth/session", headers={"Origin": serialized}, json=LOGIN).status_code == 200
        other_port = f"https://{urlsplit(serialized).hostname}:444"
        assert client.post("/api/v1/auth/session", headers={"Origin": other_port}, json=LOGIN).status_code == 403


@pytest.mark.parametrize("test_control", [False, 1, "true"])
def test_demo_requires_explicit_boolean_test_controls(test_control):
    with pytest.raises(ValueError, match="test_control=True"):
        Settings(test_control=test_control, team_demo_origin=ORIGIN)


def test_default_local_host_origin_and_cookie_are_preserved(tmp_path):
    app = create_app(Settings(database=tmp_path / "local.sqlite3", background_ticks=False))
    with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 41000)) as client:
        assert client.get("/health/live", headers={"Host": "team-demo.invalid"}).status_code == 400
        rejected = client.post("/api/v1/auth/session", headers={"Origin": ORIGIN}, json=LOGIN)
        assert rejected.status_code == 403 and rejected.json()["error"]["code"] == "ORIGIN_REJECTED"
        for origin in (LOCAL_ORIGIN, "http://localhost:8000"):
            response = client.post("/api/v1/auth/session", headers={"Origin": origin}, json=LOGIN)
            assert response.status_code == 200
            cookie_attributes(response, secure=False)
        assert client.get("/devtools").status_code == 404


def test_demo_exact_host_and_local_health(demo):
    client, _ = demo
    assert client.get("/health/live").status_code == 200
    for host in ("other.invalid", "sub.team-demo.invalid", "team-demo.invalid.evil.invalid", "www.team-demo.invalid"):
        assert client.get("/health/live", headers={"Host": host}, follow_redirects=False).status_code == 400
    for host in ("localhost", "127.0.0.1", "testserver"):
        assert client.get("/health/live", headers={"Host": host}).status_code == 200


def test_www_host_does_not_redirect_an_unapproved_host(tmp_path):
    app = create_app(Settings(database=tmp_path / "www.sqlite3", test_control=True,
                              team_demo_origin="https://www.team-demo.invalid", background_ticks=False))
    with TestClient(app, base_url="https://www.team-demo.invalid", client=("127.0.0.1", 41000)) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/live", headers={"Host": "team-demo.invalid"}, follow_redirects=False).status_code == 400


def test_demo_login_me_mutation_csrf_logout_and_secure_cookie(demo):
    client, app = demo
    response, headers = login(client)
    cookie_attributes(response, secure=True)
    token = client.cookies.get(COOKIE)
    assert client.get("/api/v1/me").json()["facility_roles"][0]["roles"] == ["test_operator"]
    for origin in (LOCAL_ORIGIN, "http://localhost:8000", "http://testserver", "https://other.invalid", ORIGIN + "/", ORIGIN + ":443"):
        denied = client.post("/api/v1/test/runs", headers={**headers, "Origin": origin, "Idempotency-Key": "bad-origin"}, json=RUN)
        assert denied.status_code == 403 and denied.json()["error"]["code"] == "ORIGIN_REJECTED"
    for csrf in (None, "wrong"):
        mutation_headers = {"Origin": ORIGIN, "Idempotency-Key": "bad-csrf"}
        if csrf is not None:
            mutation_headers["X-CSRF-Token"] = csrf
        denied = client.post("/api/v1/test/runs", headers=mutation_headers, json=RUN)
        assert denied.status_code == 403 and denied.json()["error"]["code"] == "CSRF_REJECTED"
    assert app.state.runtime.world is None
    created = client.post("/api/v1/test/runs", headers={**headers, "Idempotency-Key": "create"}, json=RUN)
    assert created.status_code == 201
    assert client.delete("/api/v1/auth/session", headers={"Origin": ORIGIN}).status_code == 403
    logout = client.delete("/api/v1/auth/session", headers=headers)
    assert logout.status_code == 204
    assert cookie_attributes(logout, secure=True)["max-age"] == "0"
    assert client.get("/api/v1/me").status_code == 401
    assert app.state.auth.lookup(token) is None
    repeated = client.delete("/api/v1/auth/session", headers={"Origin": ORIGIN})
    assert repeated.status_code == 204
    cookie_attributes(repeated, secure=True)


@pytest.mark.parametrize("origin", [None, LOCAL_ORIGIN, "http://localhost:8000", "https://other.invalid"])
def test_demo_login_rejects_missing_or_other_origin(demo, origin):
    client, _ = demo
    response = client.post("/api/v1/auth/session", headers={} if origin is None else {"Origin": origin}, json=LOGIN)
    assert response.status_code == 403 and response.json()["error"]["code"] == "ORIGIN_REJECTED"
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("peer", ["192.0.2.10", "100.64.0.10"])
def test_forwarded_headers_cannot_turn_a_remote_peer_into_loopback(tmp_path, peer):
    app = create_app(Settings(database=tmp_path / "remote.sqlite3", test_control=True,
                              team_demo_origin=ORIGIN, background_ticks=False))
    with TestClient(app, base_url=ORIGIN, client=(peer, 41000)) as client:
        forged = {"Origin": ORIGIN, "X-Forwarded-For": "127.0.0.1", "X-Forwarded-Proto": "https",
                  "X-Forwarded-Host": "localhost", "Forwarded": "for=127.0.0.1;proto=https;host=localhost"}
        for response in (client.get("/health/live", headers=forged),
                         client.post("/api/v1/auth/session", headers=forged, json=LOGIN)):
            assert response.status_code == 403 and response.json()["error"]["code"] == "LOCAL_ONLY"


def test_forwarded_host_proto_and_cors_do_not_bypass_origin_or_cookie(demo):
    client, _ = demo
    forged = {"X-Forwarded-For": "127.0.0.1", "X-Forwarded-Host": "team-demo.invalid", "X-Forwarded-Proto": "https"}
    assert client.get("/health/live", headers={**forged, "Host": "other.invalid"}).status_code == 400
    denied = client.post("/api/v1/auth/session", headers={**forged, "Origin": "http://team-demo.invalid"}, json=LOGIN)
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "ORIGIN_REJECTED"
    preflight = client.options("/api/v1/auth/session", headers={"Origin": "https://other.invalid",
                               "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in preflight.headers
    login(client)
    # The real URL controls the cookie jar; spoofing forwarded proto cannot send a Secure cookie over HTTP.
    assert client.get("http://team-demo.invalid/api/v1/me", headers=forged).status_code == 401


@pytest.mark.parametrize("username,role", [("demo-owner", "owner"), ("demo-driver", "driver")])
def test_demo_preserves_roles_and_authenticated_sse_revocation(demo, username, role):
    client, app = demo
    path = f"/api/v1/facilities/{FACILITY}/events?run_id=uncreated"
    assert client.get(path).status_code == 401
    _, headers = login(client)
    run = client.post("/api/v1/test/runs", headers={**headers, "Idempotency-Key": "run"}, json=RUN).json()
    _, user_headers = login(client, username)
    assert client.get("/api/v1/me").json()["facility_roles"][0]["roles"] == [role]
    forbidden = client.post("/api/v1/test/runs", headers={**user_headers, "Idempotency-Key": "forbidden"}, json=RUN)
    assert forbidden.status_code == 403 and forbidden.json()["error"]["code"] == "FORBIDDEN"
    runtime = app.state.runtime
    original = runtime.stream_batch
    calls = 0

    async def revoke_after_first_batch(cursor, run_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            app.state.auth.sessions.clear()
        return await original(cursor, run_id)

    runtime.stream_batch = revoke_after_first_batch
    response = client.get(f"/api/v1/facilities/{FACILITY}/events?run_id={run['run_id']}")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
    messages = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert messages[0]["type"] == "state.snapshot"
    if role == "driver":
        payload = messages[0]["payload"]
        assert payload["view_scope"] == "own_vehicles"
        assert {obj["object_id"] for obj in payload["snapshot"]["objects"]} == {"obj-car-02"}
        assert payload["snapshot"]["devices"] == []
    else:
        assert len(messages[0]["payload"]["snapshot"]["objects"]) == 3
    assert messages[-1] == {"reason": "access_changed_or_unavailable"}
    assert "event: access.revoked" in response.text
    assert client.get(f"/api/v1/facilities/{FACILITY}/events?run_id={run['run_id']}").status_code == 401


@pytest.mark.parametrize("origin,controls,valid", [(ORIGIN, "true", True), ("", "true", False),
                                                  ("http://team-demo.invalid", "true", False), (ORIGIN, "false", False)])
def test_default_factory_passes_environment_and_fails_fast(tmp_path, monkeypatch, origin, controls, valid):
    monkeypatch.setenv("PARKING_TEAM_DEMO_ORIGIN", origin)
    monkeypatch.setenv("TEST_CONTROL_ENABLED", controls)
    monkeypatch.setenv("PARKING_LOCAL_DATABASE", str(tmp_path / "factory.sqlite3"))
    monkeypatch.setenv("PARKING_LOCAL_PORT", "18081")
    monkeypatch.delenv("PARKING_LIVE_CONFIG", raising=False)
    if not valid:
        with pytest.raises(ValueError):
            create_app()
        assert not (tmp_path / "factory.sqlite3").exists()
        return
    with TestClient(create_app(), base_url=ORIGIN, client=("127.0.0.1", 41000)) as client:
        response, _ = login(client)
        cookie_attributes(response, secure=True)
        assert client.post("/api/v1/auth/session", headers={"Origin": "http://127.0.0.1:18081"}, json=LOGIN).status_code == 403


def test_factory_without_demo_environment_retains_local_origin(tmp_path, monkeypatch):
    monkeypatch.delenv("PARKING_TEAM_DEMO_ORIGIN", raising=False)
    monkeypatch.delenv("PARKING_LIVE_CONFIG", raising=False)
    monkeypatch.setenv("TEST_CONTROL_ENABLED", "false")
    monkeypatch.setenv("PARKING_LOCAL_PORT", "18081")
    monkeypatch.setenv("PARKING_LOCAL_DATABASE", str(tmp_path / "local-factory.sqlite3"))
    with TestClient(create_app(), base_url="http://127.0.0.1:18081", client=("127.0.0.1", 41000)) as client:
        response = client.post("/api/v1/auth/session", headers={"Origin": "http://127.0.0.1:18081"}, json=LOGIN)
        assert response.status_code == 200
        cookie_attributes(response, secure=False)
        assert client.get("/health/live", headers={"Host": "team-demo.invalid"}).status_code == 400
