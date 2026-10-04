"""Read-only views; observed parking geometry never assigns or reserves a bay.

Stationarity uses the existing synthetic operating settings. A bay additionally
requires zero outside depth/area of the uncertainty envelope throughout a
contiguous stationary hold, and unique containment among declared bays. The
1e-9 epsilon is geometry-core rounding, not a parking tolerance or new policy.
Only public observation history and current/historical verified grants enter
the decision. No fixture, nearest-bay, actor state or evaluation truth is used.
"""
from datetime import datetime, timezone

from fastapi import Request

from backend.auth import ApiError
from contracts.parking_assessment import ParkingAssessmentSettings
from contracts.vehicle_views import OwnParkingMap, VehicleLocation, VehicleLocations
from simulator.parking_assessment import assess_parking
from simulator.s1c_bay_geometry import analyze_bay_footprint
from simulator.world import MAP

EPSILON = 1e-9


def _mapped_objects(registry, username, vehicle_id, frame, current_sim):
    allowed = registry.allowed_objects(username, frame["run_id"], frame["sim_time_ms"],
                                       current_sim, frame["observed_at"])
    rows = registry.db.execute("""SELECT DISTINCT object_id FROM object_mappings
        WHERE facility_id=? AND run_id=? AND registered_vehicle_id=? AND mapping_status='verified'
        AND valid_from_sim_time_ms<=? AND valid_from_sim_time_ms<=?
        AND (valid_to_sim_time_ms IS NULL OR
             (valid_to_sim_time_ms>? AND valid_to_sim_time_ms>?))""",
        (frame["facility_id"], frame["run_id"], vehicle_id, frame["sim_time_ms"],
         current_sim, frame["sim_time_ms"], current_sim)).fetchall()
    return {row[0] for row in rows if row[0] in allowed}


def _whole(geometry):
    return (geometry.support_status == "supported"
            and geometry.max_depth_upper_bound_m is not None
            and geometry.max_depth_upper_bound_m <= EPSILON
            and geometry.outside_area_upper_bound_m2 is not None
            and geometry.outside_area_upper_bound_m2 <= EPSILON
            and all(area.upper_bound_m2 <= EPSILON
                    for area in geometry.adjacent_bay_overlap_m2.values()))


def _location(runtime, username, vehicle, frame, now):
    world, registry = runtime.world, runtime.store.registry
    mapped = _mapped_objects(registry, username, vehicle["registered_vehicle_id"],
                             frame, world["sim_time_ms"])
    result = dict(registered_vehicle_id=vehicle["registered_vehicle_id"],
                  display_alias=vehicle["display_alias"], object_id=None, position=None,
                  size=None, heading_deg=None, quality=None, observed_bay_id=None,
                  location_status="unknown", location_reason="no_verified_relationship")
    if len(mapped) != 1:
        if mapped:
            result["location_reason"] = "ambiguous_relationship"
        return VehicleLocation(**result)
    object_id = next(iter(mapped))
    result["object_id"] = object_id
    targets = [obj for obj in frame["objects"] if obj["object_id"] == object_id]
    if len(targets) != 1:
        result["location_reason"] = "target_not_observed"
        return VehicleLocation(**result)
    result["quality"] = targets[0]["quality"]
    settings = ParkingAssessmentSettings.model_validate(runtime.operating_analysis.settings["parking"])
    # Retain all ordering/quality resets, but never accumulate a hold across
    # frames not authorised for this same registered vehicle.
    history = []
    for entry in (world.get("observation_history") or [frame])[-settings.history_limit:]:
        if _mapped_objects(registry, username, vehicle["registered_vehicle_id"],
                           entry, world["sim_time_ms"]) != {object_id}:
            history.clear()
        else:
            history.append(entry)
    if not history or history[-1] != frame:
        history = [frame]
    common = dict(expected_run_id=world["run_id"], expected_state_version=frame["state_version"],
                  current_sim_time_ms=world["sim_time_ms"], run_status=world["run_status"],
                  recovery_required=world["recovery_required"],
                  observation_ready=bool(world.get("observation_history")), now=now)
    bays = [bay["zone_id"] for bay in MAP["parking_bays"]]
    # All declared bays are measured; never pick a nearest/fixture-selected bay.
    analyses = [assess_parking(MAP, history, object_id=object_id, bay_id=bay,
                              settings=settings, **common) for bay in bays]
    usable = [analysis for analysis in analyses if analysis.support_status == "supported"]
    if not usable:
        reasons = sorted({reason for analysis in analyses for reason in analysis.reasons})
        result["evidence_reasons"] = reasons
        result["location_reason"] = (
            "run_not_ready" if "run_not_ready" in reasons else
            "stale_observation" if "stale_observation" in reasons else
            "observation_unavailable" if frame["coverage"] == "unavailable" else
            "unsupported_geometry" if any(a.support_status == "unsupported_geometry" for a in analyses)
            else "observation_insufficient")
        return VehicleLocation(**result)
    target = targets[0]
    result.update(position=target["position"], size=target["size"], heading_deg=target["heading_deg"],
                  location_status="observed_position")
    contained = [analysis for analysis in usable if _whole(analysis.geometry)]
    if len(contained) != 1:
        result["location_reason"] = "ambiguous_bay" if len(contained) > 1 else "outside_or_intruding_bay"
        return VehicleLocation(**result)
    selected = contained[0]
    result.update(evidence_observation_ids=selected.observation_ids,
                  evidence_reasons=selected.reasons,
                  stationary_duration_ms=selected.stationary_duration_ms)
    if selected.motion_state != "stationary_candidate":
        result["location_reason"] = {"moving": "moving", "unknown": "uncertain_stationarity"}.get(
            selected.motion_state, "insufficient_history")
        return VehicleLocation(**result)
    # A stationary centroid alone is insufficient: the entire uncertainty
    # envelope must have remained in this bay for the same required hold.
    suffix = []
    for entry in reversed(history):
        geometry = analyze_bay_footprint(MAP, entry, object_id=object_id, bay_id=selected.bay_id,
            settings=settings.geometry, expected_run_id=world["run_id"],
            expected_state_version=entry["state_version"], current_sim_time_ms=entry["sim_time_ms"],
            run_status="paused", recovery_required=False, observation_ready=True,
            now=datetime.fromisoformat(entry["received_at"].replace("Z", "+00:00")))
        if not _whole(geometry) or (suffix and not
                0 < suffix[-1]["sim_time_ms"] - entry["sim_time_ms"] <= settings.max_sample_gap_ms):
            break
        suffix.append(entry)
    if len(suffix) < 2 or frame["sim_time_ms"] - suffix[-1]["sim_time_ms"] < settings.stationary_ms:
        result["location_reason"] = "insufficient_history"
    else:
        result.update(observed_bay_id=selected.bay_id, location_status="observed_bay",
                      location_reason="stationary_whole_footprint_in_unique_bay",
                      evidence_observation_ids=[entry["observation_id"] for entry in reversed(suffix)])
    return VehicleLocation(**result)


def _current(request, authenticate, initial, registry, stamp=None):
    current = authenticate(request)
    if current is not initial or (stamp is not None and registry.scope_stamp(current.username) != stamp):
        raise ApiError(403, "ACCESS_CHANGED", "차량 조회 권한이 변경되었습니다. 다시 조회하세요.")
    return current


def install_vehicle_view_routes(app, authenticate, facility_check, settings):
    @app.get("/api/v1/me/vehicle-locations", response_model=VehicleLocations)
    async def vehicle_locations(request: Request, facility_id: str, run_id: str):
        initial = authenticate(request)
        facility_check(facility_id)
        runtime = app.state.runtime
        async with runtime.lock:
            registry = runtime.store.registry
            current = _current(request, authenticate, initial, registry)
            if runtime.failure:
                raise ApiError(503, "STATE_UNAVAILABLE", "최신 차량 위치를 확인할 수 없습니다.")
            runtime.ensure_run(run_id)
            stamp = registry.scope_stamp(current.username)
            world, frame = runtime.world, runtime.world["observation"]
            now = datetime.now(timezone.utc)
            vehicles = [_location(runtime, current.username, vehicle, frame, now)
                        for vehicle in registry.vehicles(current.username)]
            result = VehicleLocations(
                **{key: frame[key] for key in ("facility_id", "run_id", "map_version", "observation_id",
                                              "state_version", "sim_time_ms", "observed_at", "received_at", "coverage")},
                run_status=world["run_status"], recovery_required=world["recovery_required"],
                applied_state_version=world["state_version"], applied_sim_time_ms=world["sim_time_ms"],
                settings_version=runtime.operating_analysis.settings.get("version"), vehicles=vehicles)
            _current(request, authenticate, initial, registry, stamp)
            return result

    @app.get("/api/v1/me/parking-map", response_model=OwnParkingMap)
    async def parking_map(request: Request, facility_id: str, map_version: str | None = None):
        initial = authenticate(request)
        facility_check(facility_id)
        runtime = app.state.runtime
        async with runtime.lock:
            registry = runtime.store.registry
            current = _current(request, authenticate, initial, registry)
            stamp = registry.scope_stamp(current.username)
            if map_version is not None and map_version != MAP["map_version"]:
                raise ApiError(404, "MAP_NOT_FOUND", "지원하지 않는 지도 버전입니다.")
            result = OwnParkingMap(
                **{key: MAP[key] for key in ("facility_id", "map_version", "coordinate_system", "bounds")},
                zones=[{key: zone[key] for key in ("zone_id", "type", "polygon")} for zone in MAP["zones"]],
                parking_bays=[{"zone_id": bay["zone_id"]} for bay in MAP["parking_bays"]])
            _current(request, authenticate, initial, registry, stamp)
            return result
