import asyncio
from copy import deepcopy
import sqlite3

import pytest

from backend.auth import ApiError
from backend.relationships import Relationships, migrate_relationships
from backend.runtime import Runtime
from backend.storage import Store
from contracts.relationships import (CustomerChange, ObjectMappingChange, PersonMappingChange,
                                     VehicleUserChange)
from simulator.world import initial_world, public_state
from test_business import rig as business_rig, notice


@pytest.fixture
def rig(tmp_path):
    path = tmp_path / "relations.sqlite3"
    store = Store(path)
    migrate_relationships(store.db)
    world = initial_world(2)
    store.commit(world, Runtime.event(world))
    try:
        yield store, Relationships(store.db), world
    finally:
        store.close()


def run(rel, key, action, payload, fn):
    return rel.execute("demo-operator", key, action, payload, fn)


def test_migration_is_versioned_preserves_seed_and_restart(tmp_path):
    path = tmp_path / "reopen.sqlite3"
    store = Store(path)
    try:
        with store.db:
            store.db.execute("UPDATE memberships SET revoked_at='2026-10-01T00:00:00Z' WHERE user_id='demo-driver'")
        migrate_relationships(store.db)
        migrate_relationships(store.db)
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 6
        assert store.db.execute("SELECT revoked_at FROM memberships WHERE user_id='demo-driver'").fetchone()[0]
        assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        store.close()
    # Reopening preserves the migration and never restores revoked grants.
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM users").fetchone()[0] == 4
        assert db.execute("SELECT count(*) FROM relationship_audit").fetchone()[0] == 0


def test_migration_failure_rolls_back_v5_additions(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.storage.migrate_relationships", lambda db: None)
    store = Store(tmp_path / "rollback.sqlite3")
    try:
        with store.db:
            store.db.execute("CREATE TABLE relation_versions (collision INTEGER)")
        with pytest.raises(sqlite3.OperationalError):
            migrate_relationships(store.db)
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert not store.db.execute("SELECT 1 FROM sqlite_master WHERE name='person_mappings'").fetchone()
        assert store.db.execute("SELECT count(*) FROM users").fetchone()[0] == 4
    finally:
        store.close()


def test_customer_vehicle_lifecycle_idempotency_and_rollback(rig):
    store, rel, _ = rig
    customer = run(rel, "c1", "customer.create", {"alias": "가상 새 고객"},
                   lambda: rel.customer_create("demo-operator", "가상 새 고객", "시험 등록"))
    assert run(rel, "c1", "customer.create", {"alias": "가상 새 고객"},
               lambda: 1) == customer
    with pytest.raises(ApiError) as conflict:
        run(rel, "c1", "customer.create", {"alias": "다른 고객"}, lambda: 1)
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
    vehicle = run(rel, "v1", "vehicle.create", {"alias": "가상 C"},
                  lambda: rel.vehicle_create("demo-operator", "가상 C", "시험 등록"))
    linked = run(rel, "link", "vehicle.user", {"user": customer["user_id"]},
                 lambda: rel.vehicle_user_change("demo-operator", vehicle["registered_vehicle_id"],
                     VehicleUserChange(expected_version=0, user_id=customer["user_id"], reason="인계")))
    assert linked["resource_version"] == 1
    with pytest.raises(ApiError):
        run(rel, "bad", "customer.change", {}, lambda: rel.customer_change("demo-operator", customer["user_id"],
            CustomerChange(expected_version=9, active=False, reason="오래된 화면")))
    assert rel.version("customer", customer["user_id"]) == 0
    done = run(rel, "disable", "customer.change", {}, lambda: rel.customer_change("demo-operator", customer["user_id"],
        CustomerChange(expected_version=0, active=False, reason="퇴장")))
    assert not done["active"]
    assert store.db.execute("SELECT valid_until FROM vehicle_users WHERE user_id=?", (customer["user_id"],)).fetchone()[0]
    assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_vehicle_assignment_history_revokes_current_access(rig):
    store, rel, world = rig
    assert {o["object_id"] for o in store.registry.project("demo-driver", public_state(world), 0)["snapshot"]["objects"]} == {"obj-car-02"}
    changed = run(rel, "transfer", "vehicle.user", {}, lambda: rel.vehicle_user_change("demo-operator", "veh-demo-02",
        VehicleUserChange(expected_version=0, user_id="demo-driver-2", reason="합성 이전")))
    assert changed["user_id"] == "demo-driver-2"
    assert store.registry.project("demo-driver", public_state(world), 0)["snapshot"]["objects"] == []
    historic = deepcopy(public_state(world))
    historic["snapshot"]["observed_at"] = "2020-01-01T00:00:00Z"
    assert {o["object_id"] for o in store.registry.project("demo-driver-2", historic, 0)["snapshot"]["objects"]} == {"obj-car-01"}
    assert len([row for row in store.db.execute("SELECT * FROM vehicle_users WHERE registered_vehicle_id='veh-demo-02'")]) == 2


def test_vehicle_mapping_requires_current_vehicle_observation_and_review(rig):
    store, rel, world = rig
    world = deepcopy(world)
    world["sim_time_ms"] = 100
    uncertain = ObjectMappingChange(run_id=world["run_id"], expected_version=0,
        registered_vehicle_id="veh-demo-02", mapping_status="uncertain", mapping_source="reviewed", reason="검토 중")
    result = run(rel, "uncertain", "object", uncertain.model_dump(),
                 lambda: rel.object_change("demo-operator", "obj-car-02", uncertain, world))
    assert result["mapping_status"] == "uncertain"
    assert store.registry.project("demo-driver", public_state(world), 100)["snapshot"]["objects"] == []
    world["sim_time_ms"] = 200
    world["observation"]["sim_time_ms"] = 200
    verified = ObjectMappingChange(run_id=world["run_id"], expected_version=1,
        registered_vehicle_id="veh-demo-02", mapping_status="verified", mapping_source="reviewed", reason="수동 확인")
    run(rel, "verified", "object", verified.model_dump(),
        lambda: rel.object_change("demo-operator", "obj-car-02", verified, world))
    assert len(store.registry.project("demo-driver", public_state(world), 200)["snapshot"]["objects"]) == 1
    with pytest.raises(ApiError) as wrong:
        rel.object_change("demo-operator", "obj-ped-01", verified, world)
    assert wrong.value.code == "OBJECT_UNOBSERVED"


def test_person_mapping_is_history_only_without_vehicle_authority(rig):
    store, rel, world = rig
    before = store.registry.scope_stamp("demo-driver")
    person = PersonMappingChange(run_id=world["run_id"], expected_version=0,
        user_id="demo-driver", status="verified", source="reviewed", reason="합성 관계 수동 검토")
    created = run(rel, "person", "person", person.model_dump(),
                  lambda: rel.person_change("demo-operator", "obj-person-01", person, world))
    assert created["status"] == "verified"
    assert store.registry.scope_stamp("demo-driver") == before
    assert {o["object_id"] for o in store.registry.project("demo-driver", public_state(world), 0)["snapshot"]["objects"]} == {"obj-car-02"}
    assert store.db.execute("SELECT reviewed_by FROM person_mappings WHERE mapping_id=?",
                            (created["mapping_id"],)).fetchone()[0] == "demo-operator"
    world["sim_time_ms"] = 100
    uncertain = PersonMappingChange(run_id=world["run_id"], expected_version=1,
        user_id="demo-driver", status="uncertain", source="reviewed", reason="확인 취소")
    run(rel, "uncertain-person", "person", uncertain.model_dump(),
        lambda: rel.person_change("demo-operator", "obj-person-01", uncertain, world))
    assert store.registry.scope_stamp("demo-driver") == before
    world["sim_time_ms"] = 200
    unmapped = PersonMappingChange(run_id=world["run_id"], expected_version=2,
        user_id=None, status="unmapped", source="reviewed", reason="연결 해제")
    run(rel, "unmap-person", "person", unmapped.model_dump(),
        lambda: rel.person_change("demo-operator", "obj-person-01", unmapped, world))
    assert store.registry.scope_stamp("demo-driver") == before
    assert {o["object_id"] for o in store.registry.project("demo-driver", public_state(world), 200)["snapshot"]["objects"]} == {"obj-car-02"}
    with pytest.raises(sqlite3.IntegrityError), store.db:
        store.db.execute("INSERT INTO person_mappings VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("overlap", "fac-demo-01", world["run_id"], "obj-person-01", "demo-driver-2",
             "proposed", "demo_config", None, None, "충돌", 0, None))


def test_person_conflict_boundary_and_run_change_preserve_history(rig):
    store, rel, original = rig
    world = deepcopy(original)
    another = deepcopy(next(o for o in world["observation"]["objects"] if o["object_id"] == "obj-person-01"))
    another["object_id"] = "obj-person-02"
    world["observation"]["objects"].append(another)
    first = PersonMappingChange(run_id=world["run_id"], expected_version=0,
        user_id="demo-driver", status="proposed", source="demo_config", reason="합성 후보")
    run(rel, "first-person", "person", first.model_dump(),
        lambda: rel.person_change("demo-operator", "obj-person-01", first, world))
    competing = first.model_copy(update={"expected_version": 0})
    with pytest.raises(ApiError) as conflict:
        run(rel, "competing-person", "person", competing.model_dump(),
            lambda: rel.person_change("demo-operator", "obj-person-02", competing, world))
    assert conflict.value.code == "MAPPING_CONFLICT"
    assert rel.version("person_mapping", world["run_id"] + ":obj-person-02") == 0
    world["sim_time_ms"] = 100
    unlink = PersonMappingChange(run_id=world["run_id"], expected_version=1,
        status="unmapped", source="reviewed", reason="후보 해제")
    run(rel, "unlink-first", "person", unlink.model_dump(),
        lambda: rel.person_change("demo-operator", "obj-person-01", unlink, world))
    run(rel, "second-person", "person", competing.model_dump(),
        lambda: rel.person_change("demo-operator", "obj-person-02", competing, world))
    rows = store.db.execute("SELECT object_id,valid_from_sim_time_ms,valid_to_sim_time_ms "
        "FROM person_mappings WHERE user_id='demo-driver' ORDER BY valid_from_sim_time_ms").fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [
        ("obj-person-01", 0, 100), ("obj-person-02", 100, None)]
    other_run = competing.model_copy(update={"run_id": "run-other", "expected_version": 0})
    with pytest.raises(ApiError) as changed:
        run(rel, "other-run", "person", other_run.model_dump(),
            lambda: rel.person_change("demo-operator", "obj-person-02", other_run, world))
    assert changed.value.code == "RUN_CHANGED"


def test_new_customer_vehicle_and_revoked_link_survive_reopen(tmp_path):
    path = tmp_path / "relationship-restart.sqlite3"
    store = Store(path)
    try:
        rel = Relationships(store.db)
        customer = run(rel, "restart-customer", "customer.create", {"alias": "재시작 고객"},
            lambda: rel.customer_create("demo-operator", "재시작 고객", "등록"))
        vehicle = run(rel, "restart-vehicle", "vehicle.create", {"alias": "재시작 차량"},
            lambda: rel.vehicle_create("demo-operator", "재시작 차량", "등록"))
        vehicle_id = vehicle["registered_vehicle_id"]
        user_id = customer["user_id"]
        link = VehicleUserChange(expected_version=0, user_id=user_id, reason="연결")
        run(rel, "restart-link", "vehicle.user", link.model_dump(),
            lambda: rel.vehicle_user_change("demo-operator", vehicle_id, link))
        unlink = VehicleUserChange(expected_version=1, user_id=None, reason="해제")
        run(rel, "restart-unlink", "vehicle.user", unlink.model_dump(),
            lambda: rel.vehicle_user_change("demo-operator", vehicle_id, unlink))
    finally:
        store.close()
    reopened = Store(path)
    try:
        rel = Relationships(reopened.db)
        assert rel.version("vehicle_user", vehicle_id) == 2
        assert rel.execute("demo-operator", "restart-link", "vehicle.user", link.model_dump(),
            lambda: None)["user_id"] == user_id
        assert rel.execute("demo-operator", "restart-unlink", "vehicle.user", unlink.model_dump(),
            lambda: None)["user_id"] is None
        assert reopened.registry.vehicles(user_id) == []
        history = reopened.db.execute("SELECT valid_from,valid_until FROM vehicle_users "
            "WHERE registered_vehicle_id=? AND user_id=?", (vehicle_id, user_id)).fetchone()
        assert history and history[1] is not None and history[1] > history[0]
        assert reopened.db.execute("SELECT count(*) FROM relationship_audit WHERE target_ref=?",
            (vehicle_id,)).fetchone()[0] == 3
    finally:
        reopened.close()


def test_relationship_change_before_pending_dispatch_blocks_old_recipient(business_rig):
    args, accepted = notice(business_rig)
    assert accepted["status"] == "accepted"
    rel = Relationships(business_rig.db)
    body = VehicleUserChange(expected_version=0, user_id="demo-driver-2", reason="차주 변경")
    run(rel, "dispatch-transfer", "vehicle.user", body.model_dump(),
        lambda: rel.vehicle_user_change("demo-operator", "veh-demo-02", body))
    assert asyncio.run(business_rig.runtime.business.deliver_one()) is False
    assert business_rig.db.execute("SELECT count(*) FROM delivery_attempts").fetchone()[0] == 0
    notification = business_rig.db.execute("SELECT delivery_status FROM notifications "
        "WHERE notification_id=?", (accepted["result"]["notification_id"],)).fetchone()
    assert notification[0] == "failed"
    assert business_rig.runtime.business.notifications("demo-driver")["items"] == []
    assert business_rig.runtime.business.notifications("demo-driver-2")["items"] == []


def test_proposed_person_link_survives_restart_without_vehicle_authority(tmp_path):
    path = tmp_path / "person-restart.sqlite3"
    store = Store(path)
    world = initial_world(2)
    store.commit(world, Runtime.event(world))
    before = store.registry.scope_stamp("demo-driver")
    try:
        rel = Relationships(store.db)
        body = PersonMappingChange(run_id=world["run_id"], expected_version=0,
            user_id="demo-driver", status="proposed", source="demo_config", reason="합성 후보")
        created = run(rel, "person-proposed", "person", body.model_dump(),
            lambda: rel.person_change("demo-operator", "obj-person-01", body, world))
        assert store.registry.scope_stamp("demo-driver") == before
    finally:
        store.close()
    reopened = Store(path)
    try:
        row = reopened.db.execute("SELECT mapping_status,mapping_source,reviewed_by,valid_to_sim_time_ms "
            "FROM person_mappings WHERE mapping_id=?", (created["mapping_id"],)).fetchone()
        assert tuple(row) == ("proposed", "demo_config", None, None)
        assert reopened.registry.scope_stamp("demo-driver") == before
        assert {o["object_id"] for o in reopened.registry.project(
            "demo-driver", public_state(world), 0)["snapshot"]["objects"]} == {"obj-car-02"}
        new_world = initial_world(3)
        changed = body.model_copy(update={"run_id": new_world["run_id"], "expected_version": 1})
        with pytest.raises(ApiError) as error:
            run(Relationships(reopened.db), "person-other-run", "person", changed.model_dump(),
                lambda: Relationships(reopened.db).person_change("demo-operator", "obj-person-01", changed, world))
        assert error.value.code == "RUN_CHANGED"
    finally:
        reopened.close()
