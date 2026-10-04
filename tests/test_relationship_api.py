from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient

from backend.app import Settings, create_app
from backend.auth import ApiError
from simulator.world import FACILITY
from test_foundation import RUN


ORIGIN = "http://testserver"
BASE = f"/api/v1/facilities/{FACILITY}/relationships"


@pytest.fixture
def client(tmp_path):
    settings = Settings(database=tmp_path / "world.sqlite3", test_control=True,
                        origins=(ORIGIN,), background_ticks=False)
    app = create_app(settings)

    with TestClient(app) as client:
        require = app.state.auth.require
        def observed_require(*args, **kwargs):
            session = require(*args, **kwargs)
            if getattr(app.state, "relationship_first_auth", None) is not None:
                app.state.relationship_first_auth.set()
            return session
        app.state.auth.require = observed_require
        yield client


def login(client, username="demo-operator"):
    assert client.post("/api/v1/auth/session", headers={"Origin": ORIGIN},
                       json={"username": username, "password": "parking-demo-only"}).status_code == 200
    return {"Origin": ORIGIN, "X-CSRF-Token": client.get("/api/v1/me").json()["csrf_token"]}


def mutate(client, method, path, data, headers, key):
    return client.request(method, BASE + path, json=data, headers={**headers, "Idempotency-Key": key})


def test_manager_crud_permissions_csrf_idempotency_and_alias_text(client):
    assert client.get(BASE).status_code == 401
    headers = login(client)
    assert client.get(BASE).status_code == 200
    assert client.get("/devtools/relationships").status_code == 200
    assert client.get("/devtools/relationships.js").status_code == 200
    assert mutate(client, "POST", "/customers", {"display_alias": "새 고객", "reason": "시험"},
                  {"Origin": ORIGIN}, "no-csrf").status_code == 403
    assert mutate(client, "POST", "/customers", {"display_alias": "새 고객", "reason": "시험"},
                  headers, "").status_code == 400
    alias = "<img src=x onerror=alert(1)>"
    created = mutate(client, "POST", "/customers", {"display_alias": alias, "reason": "가상 등록"},
                     headers, "customer-1")
    assert created.status_code == 201
    customer = created.json()["user_id"]
    assert created.json()["display_alias"] == alias
    assert "password" not in created.text
    again = mutate(client, "POST", "/customers", {"display_alias": alias, "reason": "가상 등록"},
                   headers, "customer-1")
    assert again.json() == created.json()
    assert mutate(client, "POST", "/customers", {"display_alias": "다른 값", "reason": "가상 등록"},
                  headers, "customer-1").status_code == 409
    assert any(row["user_id"] == customer and row["display_alias"] == alias for row in client.get(BASE).json()["customers"])
    assert mutate(client, "PATCH", "/customers/" + customer,
                  {"expected_version": 0, "reason": "변경", "display_alias": "가상 C"}, headers, "change").status_code == 200
    assert mutate(client, "PATCH", "/customers/" + customer,
                  {"expected_version": 0, "reason": "오래된 탭", "active": False}, headers, "stale").status_code == 409
    assert mutate(client, "PATCH", "/customers/" + customer,
                  {"expected_version": 1, "reason": "비활성", "active": False}, headers, "disable").json()["active"] is False
    assert client.post("/api/v1/auth/session", headers={"Origin": ORIGIN},
        json={"username": customer, "password": "parking-demo-only"}).status_code == 401
    login(client, "demo-owner")
    # An identical key from another authorized actor is a separate request.
    owner = mutate(client, "POST", "/customers", {"display_alias": alias, "reason": "가상 등록"},
                   {"Origin": ORIGIN, "X-CSRF-Token": client.get("/api/v1/me").json()["csrf_token"]},
                   "customer-1")
    assert owner.status_code == 201 and owner.json()["user_id"] != customer


def test_driver_denied_and_new_customer_can_only_read_own_vehicles(client):
    headers = login(client)
    created = mutate(client, "POST", "/customers", {"display_alias": "가상 C", "reason": "등록"},
                     headers, "c").json()
    vehicle = mutate(client, "POST", "/vehicles", {"display_alias": "가상 C차", "reason": "등록"},
                     headers, "v").json()
    assigned = mutate(client, "PUT", "/vehicles/" + vehicle["registered_vehicle_id"] + "/customer",
        {"expected_version": 0, "user_id": created["user_id"], "reason": "차주 연결"}, headers, "link")
    assert assigned.status_code == 200
    login(client, created["user_id"])
    assert client.get(BASE).status_code == 403
    assert mutate(client, "POST", "/vehicles", {"display_alias": "금지", "reason": "금지"},
                  {"Origin": ORIGIN, "X-CSRF-Token": client.get("/api/v1/me").json()["csrf_token"]}, "forbidden").status_code == 403
    vehicles = client.get(f"/api/v1/me/vehicles?facility_id={FACILITY}").json()["vehicles"]
    assert [row["registered_vehicle_id"] for row in vehicles] == [vehicle["registered_vehicle_id"]]


def test_relation_mapping_changes_and_current_recipient_recheck(client):
    headers = login(client)
    run = client.post("/api/v1/test/runs", headers={**headers, "Idempotency-Key": "new-run"}, json=RUN).json()
    runtime = client.app.state.runtime
    async def advance():
        async with runtime.lock:
            runtime.world["sim_time_ms"] = 100
            runtime.world["observation"]["sim_time_ms"] = 100
    client.portal.call(advance)
    body = {"run_id": run["run_id"], "expected_version": 0, "registered_vehicle_id": "veh-demo-02",
            "mapping_status": "uncertain", "mapping_source": "reviewed", "reason": "검토 중"}
    result = mutate(client, "PUT", "/vehicle-objects/obj-car-02", body, headers, "uncertain")
    assert result.status_code == 200, result.text
    with pytest.raises(ApiError) as unverified:
        client.portal.call(runtime.business.resolve_recipient, "obj-car-02")
    assert unverified.value.code == "RECIPIENT_UNVERIFIED"
    person = {"run_id": run["run_id"], "expected_version": 0, "user_id": "demo-driver",
              "status": "verified", "source": "reviewed", "reason": "가상 검토"}
    response = mutate(client, "PUT", "/person-objects/obj-person-01", person, headers, "person")
    assert response.status_code == 200, response.text
    with pytest.raises(ApiError):
        client.portal.call(runtime.business.resolve_recipient, "obj-car-02")
    assert mutate(client, "PUT", "/person-objects/obj-car-02", person, headers, "wrong-type").status_code == 409
    assert mutate(client, "PUT", "/person-objects/obj-person-01", person, headers, "stale").status_code == 409


def test_disable_customer_expires_existing_session_and_recipient(client):
    operator = login(client)
    run = client.post("/api/v1/test/runs", headers={**operator, "Idempotency-Key": "run"}, json=RUN).json()
    customer = mutate(client, "POST", "/customers", {"display_alias": "가상 고객", "reason": "등록"},
                      operator, "customer").json()["user_id"]
    assert mutate(client, "PUT", "/vehicles/veh-demo-02/customer",
        {"expected_version": 0, "user_id": customer, "reason": "연결"}, operator, "transfer").status_code == 200
    login(client, customer)
    assert client.get(f"/api/v1/me/vehicles?facility_id={FACILITY}").status_code == 200
    customer_token = client.cookies.get("parking_session")
    operator = login(client)
    assert mutate(client, "PATCH", "/customers/" + customer,
        {"expected_version": 0, "active": False, "reason": "비활성"}, operator, "disable").status_code == 200
    client.cookies.set("parking_session", customer_token)
    assert client.get("/api/v1/me").status_code == 401
    with pytest.raises(ApiError) as recipient:
        client.portal.call(client.app.state.runtime.business.resolve_recipient, "obj-car-02")
    assert recipient.value.code == "RECIPIENT_UNVERIFIED"


@pytest.mark.parametrize("change", ["transfer", "vehicle_disabled"])
def test_relationship_api_revocation_invalidates_driver_state_and_cached_answer(client, change):
    operator = login(client)
    created = client.post("/api/v1/test/runs", headers={**operator, "Idempotency-Key": "run"}, json=RUN).json()
    run_id = created["run_id"]
    login(client, "demo-driver")
    query_path = "/api/v1/test/agent/queries"
    query_body = {"run_id": run_id, "goal": "my_vehicle"}
    driver = {"Origin": ORIGIN, "X-CSRF-Token": client.get("/api/v1/me").json()["csrf_token"],
              "Idempotency-Key": "cached-vehicle"}
    initial = client.post(query_path, json=query_body, headers=driver)
    assert initial.status_code == 200, initial.text
    assert "obj-car-02" in initial.text
    operator_token, operator_session = client.portal.call(
        client.app.state.auth.login, "demo-operator", "parking-demo-only", "second-session")
    operator = {"Origin": ORIGIN, "Cookie": "parking_session=" + operator_token,
                "X-CSRF-Token": operator_session.csrf}
    if change == "transfer":
        result = mutate(client, "PUT", "/vehicles/veh-demo-02/customer",
            {"expected_version": 0, "user_id": "demo-driver-2", "reason": "차주 변경"}, operator, "revoke")
    else:
        result = mutate(client, "PATCH", "/vehicles/veh-demo-02",
            {"expected_version": 0, "active": False, "reason": "등록 비활성"}, operator, "revoke")
    assert result.status_code == 200, result.text
    state = client.get(f"/api/v1/facilities/{FACILITY}/state?run_id={run_id}")
    assert state.status_code == 200 and state.json()["snapshot"]["objects"] == []
    assert client.get(f"/api/v1/me/vehicles?facility_id={FACILITY}").json()["vehicles"] == []
    stale = client.post(query_path, json=query_body, headers=driver)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "QUERY_CONTEXT_CHANGED"
    assert "obj-car-02" not in stale.text
    if change == "transfer":
        recipient = client.portal.call(client.app.state.runtime.business.resolve_recipient, "obj-car-02")
        assert recipient["user_id"] == "demo-driver-2"
    else:
        with pytest.raises(ApiError) as recipient:
            client.portal.call(client.app.state.runtime.business.resolve_recipient, "obj-car-02")
        assert recipient.value.code == "RECIPIENT_UNVERIFIED"


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_revoked_while_waiting_for_runtime_lock_cannot_read_or_write(client, method):
    headers = login(client)
    runtime = client.app.state.runtime
    before = client.portal.call(lambda: runtime.store.db.execute("SELECT count(*) FROM users").fetchone()[0])
    entered = Event()
    client.app.state.relationship_first_auth = entered
    client.portal.call(runtime.lock.acquire)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            if method == "GET":
                pending = pool.submit(client.get, BASE)
            else:
                pending = pool.submit(mutate, client, "POST", "/customers",
                    {"display_alias": "거부", "reason": "권한 철회"}, headers, "while-waiting")
            assert entered.wait(timeout=5), "Request did not reach its first authorization check"
            def revoke():
                runtime.store.db.execute("UPDATE memberships SET revoked_at='2026-10-01T00:00:00Z' "
                                         "WHERE user_id='demo-operator' AND role='test_operator'")
                runtime.store.db.commit()
            client.portal.call(revoke)
            client.portal.call(runtime.lock.release)
            response = pending.result(timeout=5)
    finally:
        if runtime.lock.locked():
            client.portal.call(runtime.lock.release)
        client.app.state.relationship_first_auth = None
    assert response.status_code == 401
    if method == "POST":
        assert client.portal.call(lambda: runtime.store.db.execute("SELECT count(*) FROM users").fetchone()[0]) == before
