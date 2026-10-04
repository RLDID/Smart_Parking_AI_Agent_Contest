from copy import deepcopy

import pytest

from backend.auth import ApiError
from contracts.relationships import ObjectMappingChange, PersonMappingChange
from test_relationships import rig, run


@pytest.mark.parametrize("quality", ["partial", "missing", "uncertain", "stale", "recovery"])
def test_verified_person_relation_rejects_incomplete_current_observation(rig, quality):
    store, rel, original = rig
    world = deepcopy(original)
    target = next(o for o in world["observation"]["objects"] if o["object_id"] == "obj-person-01")
    if quality == "partial":
        world["observation"]["coverage"] = "partial"
    elif quality == "missing":
        target["quality"]["missing_fields"] = ["heading_deg"]
    elif quality == "uncertain":
        target["quality"]["uncertainty_m"] = 5
    elif quality == "stale":
        world["sim_time_ms"] = 1001
    else:
        world["recovery_required"] = True
    body = PersonMappingChange(run_id=world["run_id"], expected_version=0,
        user_id="demo-driver", status="verified", source="reviewed", reason="수동 확인")
    with pytest.raises(ApiError) as error:
        run(rel, "bad-review", "person", body.model_dump(), lambda: rel.person_change("demo-operator", "obj-person-01", body, world))
    assert error.value.code == "OBJECT_UNCERTAIN"
    assert not store.db.execute("SELECT 1 FROM person_mappings").fetchone()


@pytest.mark.parametrize("quality", ["partial", "missing", "uncertain", "stale", "recovery"])
def test_verified_vehicle_relation_rejects_incomplete_current_observation(rig, quality):
    store, rel, original = rig
    world = deepcopy(original)
    target = next(o for o in world["observation"]["objects"] if o["object_id"] == "obj-car-02")
    if quality == "partial":
        world["observation"]["coverage"] = "partial"
    elif quality == "missing":
        target["quality"]["missing_fields"] = ["heading_deg"]
    elif quality == "uncertain":
        target["quality"]["uncertainty_m"] = 5
    elif quality == "stale":
        world["sim_time_ms"] = 1001
    else:
        world["recovery_required"] = True
    body = ObjectMappingChange(run_id=world["run_id"], expected_version=0,
        registered_vehicle_id="veh-demo-02", mapping_status="verified", mapping_source="reviewed", reason="수동 확인")
    with pytest.raises(ApiError) as error:
        run(rel, "bad-vehicle-review", "object", body.model_dump(),
            lambda: rel.object_change("demo-operator", "obj-car-02", body, world))
    assert error.value.code == "OBJECT_UNCERTAIN"
    assert rel.version("object_mapping", world["run_id"] + ":obj-car-02") == 0


def test_unlink_can_revoke_relation_after_track_disappears_without_restoring_authority(rig):
    store, rel, world = rig
    body = PersonMappingChange(run_id=world["run_id"], expected_version=0,
        user_id="demo-driver", status="verified", source="reviewed", reason="수동 확인")
    run(rel, "review", "person", body.model_dump(), lambda: rel.person_change("demo-operator", "obj-person-01", body, world))
    world["sim_time_ms"] = 200
    world["observation"]["objects"] = []
    world["observation"]["coverage"] = "unavailable"
    unlink = PersonMappingChange(run_id=world["run_id"], expected_version=1,
        status="unmapped", source="reviewed", reason="연결 종료")
    result = run(rel, "unlink", "person", unlink.model_dump(), lambda: rel.person_change("demo-operator", "obj-person-01", unlink, world))
    assert result["status"] == "unmapped"
    assert store.db.execute("SELECT mapping_status FROM person_mappings WHERE valid_to_sim_time_ms IS NULL").fetchone()[0] == "unmapped"
