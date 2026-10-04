"""Business migration tests use only disposable synthetic SQLite files."""
import sqlite3

import pytest
from pydantic import ValidationError

from backend.business_schema import migrate_business
from backend.storage import Store
from contracts.business import (BusinessContext, FollowupInput, IncidentInput,
                                NotifyVehicle, ResponseInput)
from simulator.world import FACILITY, initial_world


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    # Build the actual preceding schema, then test the migration independently
    # of Store's automatic current-version opening.
    with monkeypatch.context() as patch:
        patch.setattr("backend.storage.migrate_business", lambda db: None)
        store = Store(tmp_path / "business.sqlite3")
    world = initial_world(1)
    store.commit(world, None)
    try:
        yield store.db, world["run_id"]
    finally:
        store.close()


def test_contract_rejects_extra_and_invalid_action_inputs():
    context = dict(facility_id=FACILITY, run_id="r1", based_on_state_version=1, policy_version=1)
    with pytest.raises(ValidationError):
        BusinessContext(**context, secret="must fail")
    with pytest.raises(ValidationError):
        NotifyVehicle(**context, incident_id="i1", recipient_ref="v1", contact_sequence=1,
                      expected_resource_version=1, template_args={"zone_label": "aisle", "phone": "hidden"})
    with pytest.raises(ValidationError):
        IncidentInput(**context, incident_id="i1", primary_object_id="obj", status="resolved",
                      impacts=[{"type": "aisle_obstruction", "zone_id": "z"}], evidence_ids=["o1"],
                      reason_summary="clear")
    with pytest.raises(ValidationError):
        FollowupInput(**context, incident_id="i1", clock="sim", due_sim_time_ms=1,
                      due_at="2026-09-30T00:00:00Z", condition="spatial_recheck", max_attempts=1)
    with pytest.raises(ValidationError):
        ResponseInput(client_request_id="c1", response="resolved")


def test_migration_preserves_v3_and_rejects_cross_run_evidence(seeded):
    db, run_id = seeded
    assert db.execute("PRAGMA user_version").fetchone()[0] == 3
    migrate_business(db)
    assert db.execute("PRAGMA user_version").fetchone()[0] == 4
    assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
    migrate_business(db)  # idempotent open
    db.execute("INSERT INTO observation_evidence(observation_id,facility_id,run_id,state_version,sim_time_ms,payload_json,digest) VALUES (?,?,?,?,?,?,?)",
               ("o1", FACILITY, run_id, 1, 100, "{}", "sha256:x"))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO observation_evidence(observation_id,facility_id,run_id,state_version,sim_time_ms,payload_json,digest) VALUES (?,?,?,?,?,?,?)",
                   ("o2", FACILITY, "other-run", 1, 100, "{}", "sha256:x"))
    assert db.execute("SELECT count(*) FROM observation_evidence").fetchone()[0] == 1


def test_unique_keys_and_append_only(seeded):
    db, run_id = seeded
    migrate_business(db)
    db.execute("INSERT INTO observation_evidence(observation_id,facility_id,run_id,state_version,sim_time_ms,payload_json,digest) VALUES (?,?,?,?,?,?,?)",
               ("o1", FACILITY, run_id, 1, 100, "{}", "sha256:x"))
    # A policy reference is required even when a caller tries to fabricate an incident.
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO incidents(incident_id,facility_id,run_id,status,primary_object_id,dedup_key,policy_version,reason_summary) VALUES (?,?,?,?,?,?,?,?)",
                   ("i1", FACILITY, run_id, "active", "obj", "same", 999, "blocked"))
    db.execute("INSERT INTO knowledge_releases(facility_id,knowledge_release_id,manifest_digest,index_version,index_digest,index_file,created_at) VALUES (?,?,?,?,?,?,?)",
               (FACILITY, "release-test", "sha256:m", "keyword-v1", "sha256:i", "test-index", "2026-09-30T00:00:00Z"))
    db.execute("INSERT INTO policies(facility_id,policy_version,knowledge_release_id,effective_at,content_json) VALUES (?,?,?,?,?)",
               (FACILITY, 1, "release-test", "2026-09-30T00:00:00Z", "{}"))
    incident = "INSERT INTO incidents(incident_id,facility_id,run_id,status,primary_object_id,dedup_key,policy_version,reason_summary) VALUES (?,?,?,?,?,?,?,?)"
    db.execute(incident, ("i1", FACILITY, run_id, "active", "obj", "same", 1, "blocked"))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(incident, ("i2", FACILITY, run_id, "monitoring", "obj", "same", 1, "blocked"))
    db.execute("UPDATE incidents SET status='resolved' WHERE incident_id='i1'")
    db.execute(incident, ("i2", FACILITY, run_id, "active", "obj", "same", 1, "recurred"))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO incident_evidence(facility_id,run_id,incident_id,observation_id,analysis_version,metrics_json,purpose) VALUES (?,?,?,?,?,?,?)",
                   (FACILITY, "other-run", "i2", "o1", "v1", "{}", "detection"))
    db.execute("INSERT INTO incident_evidence(facility_id,run_id,incident_id,observation_id,analysis_version,metrics_json,purpose) VALUES (?,?,?,?,?,?,?)",
               (FACILITY, run_id, "i2", "o1", "v1", "{}", "detection"))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM incident_evidence WHERE incident_id='i2'")
    db.execute("INSERT INTO audit_events(audit_id,facility_id,run_id,actor_ref,action,target_ref,outcome,reason_code,occurred_at,correlation_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
               ("a1", FACILITY, run_id, "system", "test", "o1", "recorded", "test", "2026-09-30T00:00:00Z", "c1"))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE audit_events SET outcome='changed' WHERE audit_id='a1'")


def test_failed_migration_rolls_back_schema_and_version(seeded):
    db, _ = seeded
    db.execute("CREATE TABLE incidents(dummy TEXT)")
    db.commit()
    with pytest.raises(sqlite3.OperationalError):
        migrate_business(db)
    assert db.execute("PRAGMA user_version").fetchone()[0] == 3
    assert db.execute("SELECT name FROM sqlite_master WHERE name='observation_evidence'").fetchone() is None
    assert db.execute("SELECT name FROM sqlite_master WHERE name='incidents'").fetchone() is not None


def test_execution_and_notification_uniqueness(seeded):
    db, run_id = seeded
    migrate_business(db)
    db.execute("INSERT INTO knowledge_releases(facility_id,knowledge_release_id,manifest_digest,index_version,index_digest,index_file,created_at) VALUES (?,?,?,?,?,?,?)",
               (FACILITY, "release-test", "sha256:m", "keyword-v1", "sha256:i", "test-index", "2026-09-30T00:00:00Z"))
    db.execute("INSERT INTO policies(facility_id,policy_version,knowledge_release_id,effective_at,content_json) VALUES (?,?,?,?,?)",
               (FACILITY, 1, "release-test", "2026-09-30T00:00:00Z", "{}"))
    db.execute("INSERT INTO incidents(incident_id,facility_id,run_id,status,primary_object_id,dedup_key,policy_version,reason_summary) VALUES (?,?,?,?,?,?,?,?)",
               ("i1", FACILITY, run_id, "active", "obj-car-02", "block", 1, "blocked"))
    execution = """INSERT INTO executions(execution_id,facility_id,run_id,incident_id,tool_name,target_ref,requester_ref,idempotency_key,payload_hash,payload_json,status,based_on_state_version,policy_version,mode)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
    common = (FACILITY, run_id, "i1", "notify_vehicle_user", "veh-demo-02", "demo-operator")
    db.execute(execution, ("e1", *common, "key1", "h1", "{}", "accepted", 1, 1, "synthetic_demo"))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(execution, ("e2", *common, "key1", "h2", "{}", "accepted", 1, 1, "synthetic_demo"))
    db.execute(execution, ("e2", *common, "key2", "h2", "{}", "accepted", 1, 1, "synthetic_demo"))
    notification = """INSERT INTO notifications(notification_id,facility_id,run_id,execution_id,incident_id,context_key,registered_vehicle_id,recipient_user_id,purpose,contact_sequence,delivery_status,message_template,message_json,mode)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
    notice = (FACILITY, run_id, "i1", "incident:i1", "veh-demo-02", "demo-driver", "move_request", 1, "queued", "move_request_v1", "{}", "synthetic_demo")
    db.execute(notification, ("n1", FACILITY, run_id, "e1", *notice[2:]))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(notification, ("n2", FACILITY, run_id, "e2", *notice[2:]))
