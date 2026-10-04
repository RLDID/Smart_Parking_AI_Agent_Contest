"""SIM-0 synthetic world behavior and device checkpoint adapter.

Fixture routes and reactions are internal experimental controls, never
observation fields, Agent answers, evaluation truth, or physical equipment.
The caller owns authentication, notification delivery, run locking and writes.
"""

from copy import deepcopy
from math import ceil, cos, hypot, pi, radians, sin
import re

from shapely.geometry import Point, box
from shapely.ops import unary_union

from contracts.devices import DeviceCommand, DeviceResult, DeviceState
from contracts.models import Position
from simulator.devices import initial_devices, operate_devices
from simulator.s2_motion import _contact_fraction
from simulator.world import CONFIG, MAP, TICK_MS, digest, footprint, swept_translation, utc_now


FIXTURE_REFS = frozenset({
    "s1a-foundation-v1", "s1b-blocked-v1", "s1b-clear-v1",
    "s1c-overlap-v1", "s1c-contained-v1", "s2-crossing-v1",
    "s2-offset-v1", "s2-occluded-v1", "s3-closing-v1",
    "s3-gate-obstacle-v1",
})
SIM0_CONFIG = {"version": "sim0-v1", "tick_ms": TICK_MS, "observation_ms": 200,
               "movement_step_m": 0.08, "rotation_step_deg": 2.0,
               "car_speed_mps": 2.0, "maneuver_speed_mps": 0.8,
               "person_speed_mps": 1.2, "s2_offset_start_ms": 3000}
# Synthetic experiment settings. They are not adopted parking or safety rules.
DEVICE_EXPERIMENT_SETTINGS = {"broadcast_cooldown_s": 30,
                              "gate_transition_ms": 500,
                              "allowed_messages": ["closing_notice", "safety_notice",
                                                   "no_litter_notice"]}
MAX_ACTIONS = 128
MAX_DELAY_MS = 60_000


def _vehicle(actor_id, object_id, x, y, heading):
    return {"actor_id": actor_id, "object_id": object_id,
            "object_type": "vehicle", "x": x, "y": y,
            "length_m": 4.6, "width_m": 1.8, "heading_deg": heading}


def _person(actor_id, object_id, x, y, heading=90.0):
    return {"actor_id": actor_id, "object_id": object_id,
            "object_type": "pedestrian", "x": x, "y": y,
            "length_m": 0.6, "width_m": 0.6, "heading_deg": heading}


def _fixture_actors(ref):
    if ref.startswith("s1b-"):
        bx = 9.5 if ref == "s1b-blocked-v1" else 27.0
        return [_vehicle("internal-a", "obj-car-01", 9.5, 26.5, 90),
                _vehicle("internal-b", "obj-car-02", bx, 21.7, 0)]
    if ref.startswith("s1c-"):
        # The legacy A pose would overlap the intrusion B pose. This fixture
        # contains only B and the independent pedestrian to keep real geometry.
        if ref == "s1c-overlap-v1":
            return [_vehicle("internal-b", "obj-car-02", 11.0, 26.5, 90)]
        return [_vehicle("internal-b", "obj-car-02", 9.5, 26.5, 94)]
    if ref.startswith("s2-"):
        return [_vehicle("internal-v", "obj-car-s2-v", 16.0, 20.0, 0),
                _person("internal-p", "obj-person-s2-p", 22.0, 16.4)]
    if ref.startswith("s3-"):
        inbound_y = 1.0 if ref == "s3-gate-obstacle-v1" else -3.0
        return [_vehicle("internal-u", "obj-car-s3-u", 29.5, inbound_y, 90),
                _vehicle("internal-w", "obj-car-s3-w", 32.5, 9.0, 270)]
    raise ValueError("Unsupported SIM-0 fixture")


def initialize_environment(world, fixture_ref):
    """Mutate a freshly allocated world before its first public observation."""
    if fixture_ref not in FIXTURE_REFS:
        raise ValueError("Unsupported fixture_ref")
    world["fixture_ref"] = fixture_ref
    world["action_queue"] = []
    world["movement_blocked"] = False
    world["device_state"] = initial_devices(
        MAP, utc_now(), **DEVICE_EXPERIMENT_SETTINGS).model_dump(mode="json")
    world["recorded_epoch_utc"] = world["device_state"]["now_utc"]
    world["device_faults"] = {"visual": False, "audio": False,
                              "simulated_playback": False, "gate": False}
    world["physical_contacts"] = []
    if fixture_ref != "s1a-foundation-v1":
        world["actors"] = _fixture_actors(fixture_ref)
        world["configuration_version"] = "sim0-v1"
        world["configuration_digest"] = digest(SIM0_CONFIG)
        world["behavior_policy_version"] = "sim0-v1"
        world["behavior_digest"] = digest({"version": "sim0-v1", "fixture_ref": fixture_ref,
                                           "config": SIM0_CONFIG})
        world["move_requested"] = False
    if fixture_ref.startswith("s2-"):
        world["s2_pedestrian_start_ms"] = SIM0_CONFIG["s2_offset_start_ms"] if ref_is_offset(fixture_ref) else 0
        world["s2_reaction_mode"] = "brake_on_alarm"
        world["s2_brake_delay_ms"] = 0
        world["s2_alarm_seen_ms"] = None
        world["s2_contact_at_ms"] = None
    if fixture_ref.startswith("s3-"):
        world["s3_entered"] = False
        world["s3_exited"] = False


def ref_is_offset(ref):
    return ref == "s2-offset-v1"


def validate_environment_checkpoint(world):
    ref = world.get("fixture_ref", "s1a-foundation-v1")
    if ref not in FIXTURE_REFS or world.get("map_digest") != digest(MAP):
        return False
    if ref == "s1a-foundation-v1":
        return (world.get("configuration_version") == "foundation-v1"
                and world.get("configuration_digest") == digest(CONFIG)
                and world.get("behavior_policy_version") == "straight-north-v1"
                and world.get("behavior_digest") == digest("straight-north-v1"))
    return (world.get("configuration_version") == "sim0-v1"
            and world.get("configuration_digest") == digest(SIM0_CONFIG)
            and world.get("behavior_policy_version") == "sim0-v1"
            and world.get("behavior_digest") == digest(
                {"version": "sim0-v1", "fixture_ref": ref, "config": SIM0_CONFIG})
            and isinstance(world.get("action_queue"), list)
            and isinstance(world.get("device_state"), dict))


def prepare_environment_restart(world):
    """Retain checkpoint state; never replay already accepted actions as new."""
    if not validate_environment_checkpoint(world):
        raise RuntimeError("Environment policy mismatch; explicit migration required")
    if "device_state" not in world:
        # A legacy foundation checkpoint predates synthetic device integration.
        if world.get("fixture_ref", "s1a-foundation-v1") != "s1a-foundation-v1":
            raise RuntimeError("SIM-0 device checkpoint missing")
        world["device_state"] = initial_devices(
            MAP, utc_now(), **DEVICE_EXPERIMENT_SETTINGS).model_dump(mode="json")
    DeviceState.model_validate(world["device_state"])
    world.setdefault("action_queue", [])
    world.setdefault("device_faults", {"visual": False, "audio": False,
                                       "simulated_playback": False, "gate": False})
    world.setdefault("physical_contacts", [])


def gate_observations(world, now_utc):
    state = DeviceState.model_validate(world["device_state"])
    return [{"device_id": gate.gate_id, "type": "gate",
             "resource_version": gate.resource_version,
             "entry_policy": gate.entry_policy,
             "physical_state": gate.physical_state,
             "obstacle_detected": gate.obstacle_detected,
             "fault_code": gate.last_feedback if gate.last_feedback in
             {"failed", "unknown", "safety_stop"} else None,
             "observed_at": now_utc,
             "quality": {"visibility": "visible", "uncertainty_m": 0,
                         "missing_fields": []}}
            for gate in state.gates]


def public_devices(world):
    """Project device feedback without policies, operation hashes or future input."""
    state = DeviceState.model_validate(world["device_state"])
    return {
        "run_id": world["run_id"], "state_version": world["state_version"],
        "sim_time_ms": world["sim_time_ms"], "device_version": state.version,
        "alarms": [{"zone_id": a.zone_id, "resource_version": a.resource_version,
                    "desired_active": a.desired_active, "claim_count": len(a.claims),
                    "visual": a.visual, "audio": a.audio} for a in state.alarms],
        "gates": [{"gate_id": g.gate_id, "direction": g.direction,
                   "resource_version": g.resource_version,
                   "entry_policy": g.entry_policy, "physical_state": g.physical_state,
                   "obstacle_detected": g.obstacle_detected,
                   "last_feedback": g.last_feedback} for g in state.gates],
        "broadcasts": [{"operation_id": b.operation_id, "zone_id": b.zone_id,
                        "message_id": b.message_id, "receipt": b.receipt,
                        "simulated_playback": b.simulated_playback,
                        "browser_playback": b.browser_playback} for b in state.broadcasts],
    }


def _observed_sensor(world, gate_id):
    """Current public observation is the permission evidence; unknown holds."""
    frame = world.get("observation") or {}
    age = world["sim_time_ms"] - frame.get("sim_time_ms", -10**9)
    if (world.get("recovery_required") or frame.get("coverage") != "complete"
            or not 0 <= age <= 400):
        return None
    target = next((g for g in MAP["gates"] if g["gate_id"] == gate_id), None)
    if target is None:
        return None
    segment = target["segment"]
    x0, y0 = segment[0]["x"], segment[0]["y"]
    x1, y1 = segment[1]["x"], segment[1]["y"]
    sensor = box(min(x0, x1), min(y0, y1)-0.2,
                 max(x0, x1), max(y0, y1)+0.2)
    for obj in frame["objects"]:
        if (obj["quality"]["visibility"] != "visible" or obj["quality"]["missing_fields"]
                or obj["position"] is None or obj["size"] is None or obj["heading_deg"] is None):
            return None
        actor = {"x": obj["position"]["x"], "y": obj["position"]["y"],
                 "length_m": obj["size"]["length_m"],
                 "width_m": obj["size"]["width_m"], "heading_deg": obj["heading_deg"]}
        if footprint(actor).intersects(sensor):
            return True
    return False


def apply_device_command(world, command, *, now_utc):
    """Apply a validated synthetic command; runtime must lock and commit world."""
    command = DeviceCommand.model_validate(command)
    from simulator.replay import record_input
    if world.get("replay_state") is not None:
        now_utc = world["replay_state"]["current_utc"]
    state = DeviceState.model_validate(world["device_state"])
    if state.map_digest != digest(MAP) or state.facility_id != MAP["facility_id"]:
        raise RuntimeError("Device checkpoint map mismatch")
    if command.action in {"tick", "command_gate", "gate_feedback"} and command.gate_id:
        observed = _observed_sensor(world, command.gate_id)
        if command.action == "tick" or command.target == "closed" or command.action == "gate_feedback":
            command = command.model_copy(update={"obstacle_detected": observed})
    result = operate_devices(state, command, now_utc=now_utc,
                             sim_time_ms=world["sim_time_ms"])
    if command.action in {"broadcast", "cancel_broadcast", "set_entry_policy", "command_gate"} and not any(
            item.operation_id == command.operation_id for item in state.operations):
        record_input(world, "device_command", command.model_dump(mode="json"), occurred_at=now_utc)
    world["device_state"] = result.state.model_dump(mode="json")
    # Device feedback is a state change even while simulation time is paused.
    from simulator.world import observe
    world["state_version"] += 1
    observe(world)
    return result


def _queue_action(world, *, object_id, kind, action_key, response=None, delay_ms=0):
    if (not isinstance(action_key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", action_key)
            or type(delay_ms) is not int or not 0 <= delay_ms <= MAX_DELAY_MS):
        raise ValueError("Invalid synthetic action key or delay")
    queue = world.setdefault("action_queue", [])
    intended = {"action_key": action_key, "object_id": object_id,
                "kind": kind, "response": response, "delay_ms": delay_ms}
    old = next((item for item in queue if item["action_key"] == action_key), None)
    if old:
        if any(old[key] != intended[key] for key in intended):
            raise ValueError("Synthetic action key collision")
        return _action_result(old)
    if object_id not in {a["object_id"] for a in world["actors"]}:
        raise ValueError("Unknown object in current world")
    if (kind != "response" or response == "will_move") and any(
            item["object_id"] == object_id and item["status"] in
            {"queued", "moving", "waiting_at_gate"} for item in queue):
        raise ValueError("Actor already has an active movement")
    if len(queue) >= MAX_ACTIONS:
        raise ValueError("Synthetic action history is full")
    status = "no_movement" if kind == "response" and response != "will_move" else "queued"
    action = {**intended, "apply_at_ms": world["sim_time_ms"] + delay_ms,
              "status": status, "route_index": 0, "route": None}
    queue.append(action)
    from simulator.replay import record_input
    record_input(world, "vehicle_action", {"object_id": object_id, "kind": kind,
        "action_key": action_key, "response": response, "delay_ms": delay_ms})
    return _action_result(action)


def _action_result(action):
    return {"action_key": action["action_key"], "object_id": action["object_id"],
            "response": action.get("response"), "status": action["status"]}


def queue_vehicle_response(world, object_id, response, *, action_key, delay_ms=0):
    """Accept only a backend-verified response; receipt alone never moves a car."""
    if response not in {"acknowledged", "will_move", "cannot_move", "question"}:
        raise ValueError("Unsupported synthetic response")
    ref = world.get("fixture_ref", "s1a-foundation-v1")
    if ref not in {"s1a-foundation-v1", "s1b-blocked-v1", "s1c-overlap-v1"}:
        raise ValueError("No supported response-driven movement in this fixture")
    if object_id != "obj-car-02":
        raise ValueError("Only the fixture's addressed B vehicle can respond")
    return _queue_action(world, object_id=object_id, kind="response",
                         action_key=action_key, response=response, delay_ms=delay_ms)


def queue_vehicle_departure(world, object_id, *, action_key):
    if world.get("fixture_ref") not in {"s1b-blocked-v1", "s1b-clear-v1"} or object_id != "obj-car-01":
        raise ValueError("Only the supported S1-b A departure is available")
    return _queue_action(world, object_id=object_id, kind="departure",
                         action_key=action_key)


def queue_portal_attempt(world, object_id, *, action_key):
    if (not world.get("fixture_ref", "").startswith("s3-")
            or object_id not in {"obj-car-s3-u", "obj-car-s3-w"}):
        raise ValueError("Only S3 portal actors may attempt passage")
    return _queue_action(world, object_id=object_id, kind="portal_attempt",
                         action_key=action_key)


def cancel_vehicle_action(world, action_key):
    item = next((a for a in world.get("action_queue", []) if a["action_key"] == action_key), None)
    if item is None:
        raise ValueError("Unknown synthetic action key")
    if item["status"] in {"queued", "moving", "waiting_at_gate"}:
        from simulator.replay import record_input
        record_input(world, "cancel_action", {"action_key": action_key})
        item["status"] = "cancelled"
        if (world.get("fixture_ref", "s1a-foundation-v1") == "s1a-foundation-v1"
                and world.get("legacy_auto_move_key") == action_key):
            world["move_requested"] = False
            world["legacy_auto_move_key"] = None
    return _action_result(item)


def set_synthetic_fault(world, channel, failed):
    """Explicit test-only fault input; caller must restrict it to dev control."""
    if channel not in {"visual", "audio", "simulated_playback", "gate"} or type(failed) is not bool:
        raise ValueError("Unsupported synthetic device fault")
    world["device_faults"][channel] = failed
    from simulator.replay import record_input
    record_input(world, "device_fault", {"channel": channel, "failed": failed})


def configure_s2_reaction(world, mode, *, delay_ms=0):
    """Explicit fixture control, never a choice made by a product Agent."""
    if (not world.get("fixture_ref", "").startswith("s2-")
            or mode not in {"brake_on_alarm", "no_response"}
            or type(delay_ms) is not int or not 0 <= delay_ms <= 3000):
        raise ValueError("Unsupported S2 response experiment")
    world["s2_reaction_mode"] = mode
    world["s2_brake_delay_ms"] = delay_ms
    from simulator.replay import record_input
    record_input(world, "s2_reaction", {"mode": mode, "delay_ms": delay_ms})


def _line(start, end, step=0.08):
    distance = hypot(end[0]-start[0], end[1]-start[1])
    turn = abs(end[2]-start[2])
    count = max(1, ceil(distance/step), ceil(turn/2))
    return [[start[i] + (end[i]-start[i])*n/count for i in range(3)]
            for n in range(1, count+1)]


def _route_for(world, action, actor):
    ref, object_id = world.get("fixture_ref"), action["object_id"]
    if ref in {"s1b-blocked-v1", "s1b-clear-v1"} and object_id == "obj-car-02":
        return _line((actor["x"], actor["y"], actor["heading_deg"]), (27, 21.7, 0))
    if ref == "s1c-overlap-v1" and object_id == "obj-car-02":
        start = (actor["x"], actor["y"], actor["heading_deg"])
        if start != (11.0, 26.5, 90.0):
            return None
        poses = _line(start, (11, 20, 90))
        poses += _line((11, 20, 90), (11, 20, 180))
        poses += _line((11, 20, 180), (9.5, 20, 180))
        poses += _line((9.5, 20, 180), (9.5, 20, 90))
        poses += _line((9.5, 20, 90), (9.5, 26.5, 90))
        return poses
    if ref in {"s1b-blocked-v1", "s1b-clear-v1"} and object_id == "obj-car-01":
        if (actor["x"], actor["y"], actor["heading_deg"]) != (9.5, 26.5, 90.0):
            return None
        poses = _line((9.5, 26.5, 90), (9.5, 24, 90))
        count = ceil((2*pi)/0.08)
        poses += [[13.5 + 4*cos(pi + (pi/2)*n/count),
                   24 + 4*sin(pi + (pi/2)*n/count),
                   90 + 90*n/count] for n in range(1, count+1)]
        poses += _line((13.5, 20, 180), (-2.3, 20, 180))
        return poses
    return None


def _route_area(world, actor):
    ref = world.get("fixture_ref", "")
    zones = {z["zone_id"]: box(*(
        min(p["x"] for p in z["polygon"]), min(p["y"] for p in z["polygon"]),
        max(p["x"] for p in z["polygon"]), max(p["y"] for p in z["polygon"])))
        for z in MAP["zones"]}
    if ref.startswith("s1b-") and actor["object_id"] == "obj-car-02":
        # The initial blocking pose spans the seam between the west and
        # central aisles; its full footprint needs the small connecting band.
        return box(6.5, 16, 32, 24)
    if ref.startswith("s1b-") and actor["object_id"] == "obj-car-01":
        return unary_union([box(7, 18, 32, 29), zones["aisle-west"],
                            box(-5, 18, 0, 22)])
    if ref.startswith("s1c-"):
        # The fixed low-speed repark turn sweeps just south of the west aisle.
        return box(0, 16, 32, 29)
    return None


def _swept_pose(before, after):
    """Conservatively bound continuous rotation and translation per small step."""
    if before["heading_deg"] == after["heading_deg"]:
        return swept_translation(before, after)
    origin = footprint(before)
    target = footprint(after)
    angle = radians(abs(after["heading_deg"]-before["heading_deg"]))
    half_diagonal = hypot(before["length_m"], before["width_m"])/2
    # Any interior pose differs from one endpoint by at most translation plus
    # rotational material-point displacement. Buffer slightly beyond the bound.
    error = hypot(after["x"]-before["x"], after["y"]-before["y"]) + half_diagonal*angle
    return unary_union((origin, target)).convex_hull.buffer(error + 1e-9)


def _move_route(world, action, actor):
    route = action["route"]
    index = action["route_index"]
    if index >= len(route):
        action["status"] = "completed"
        return
    x, y, heading = route[index]
    after = dict(actor, x=x, y=y, heading_deg=heading)
    area = _route_area(world, actor)
    sweep = _swept_pose(actor, after)
    blocked = (area is None or not area.covers(sweep)
               or any(sweep.intersects(footprint(other))
                      for other in world["actors"] if other is not actor))
    if blocked:
        action["status"] = "blocked"
        world["movement_blocked"] = True
        return
    actor.update(x=x, y=y, heading_deg=heading)
    action["route_index"] += 1
    if action["route_index"] == len(route):
        action["status"] = "completed"
        if actor["object_id"] == "obj-car-01" and footprint(actor).bounds[2] <= 1e-8:
            _exit_actor(world, actor, "portal-west")


def _exit_actor(world, actor, portal):
    from simulator.replay import recorded_clock
    world["pending_events"].append({"object_id": actor["object_id"],
        "event_type": "exited", "portal_id": portal,
        "sim_time_ms": world["sim_time_ms"], "observed_at": recorded_clock(world, "object_event"),
        "quality": {"visibility": "visible", "uncertainty_m": 0,
                    "missing_fields": []}})
    world["actors"].remove(actor)


def _activate_actions(world):
    active_actors = {action["object_id"] for action in world.get("action_queue", [])
                     if action["status"] in {"moving", "waiting_at_gate"}}
    for action in world.get("action_queue", []):
        if action["status"] != "queued" or action["apply_at_ms"] > world["sim_time_ms"]:
            continue
        if action["object_id"] in active_actors:
            continue
        actor = next((a for a in world["actors"] if a["object_id"] == action["object_id"]), None)
        if actor is None:
            action["status"] = "blocked"
            continue
        if action["kind"] == "portal_attempt":
            action["status"] = "moving"
        elif world.get("fixture_ref") == "s1a-foundation-v1":
            world["move_requested"] = True
            world["legacy_auto_move_key"] = action["action_key"]
            action["status"] = "moving"
        else:
            route = _route_for(world, action, actor)
            if not route:
                action["status"] = "blocked"
            else:
                action["route"] = route
                action["status"] = "moving"
        if action["status"] == "moving":
            active_actors.add(action["object_id"])


def _in_announcement_zone(zone_id, x, y):
    zone = next((z for z in MAP["zones"] if z["zone_id"] == zone_id), None)
    if zone is None:
        return False
    points = zone["polygon"]
    return box(min(p["x"] for p in points), min(p["y"] for p in points),
               max(p["x"] for p in points), max(p["y"] for p in points)).covers(Point(x, y))


def _advance_s2(world):
    if world["s2_contact_at_ms"] is not None:
        return
    vehicle = next(a for a in world["actors"] if a["object_type"] == "vehicle")
    person = next(a for a in world["actors"] if a["object_type"] == "pedestrian")
    state = DeviceState.model_validate(world["device_state"])
    # A driver only reacts to confirmed synthetic feedback in the vehicle's
    # current announcement zone. A requested but failed alarm has no effect.
    perceivable = any(
        zone.desired_active and (zone.visual == "on" or zone.audio == "on")
        and _in_announcement_zone(zone.zone_id, vehicle["x"], vehicle["y"])
        for zone in state.alarms)
    if perceivable:
        if world["s2_alarm_seen_ms"] is None:
            world["s2_alarm_seen_ms"] = world["sim_time_ms"] - TICK_MS
    brake = (world["s2_reaction_mode"] == "brake_on_alarm"
             and world["s2_alarm_seen_ms"] is not None
             and world["sim_time_ms"] >= world["s2_alarm_seen_ms"] + world["s2_brake_delay_ms"])
    v_before = Position(x=vehicle["x"], y=vehicle["y"])
    p_before = Position(x=person["x"], y=person["y"])
    vx = vehicle["x"] if brake else min(29.7, vehicle["x"] + 2*TICK_MS/1000)
    active_ms = max(0, world["sim_time_ms"] - max(world["sim_time_ms"]-TICK_MS,
                                                  world["s2_pedestrian_start_ms"]))
    py = min(29.7, person["y"] + 1.2*active_ms/1000)
    v_after = Position(x=vx, y=vehicle["y"])
    p_after = Position(x=person["x"], y=py)
    fraction = _contact_fraction(v_before, v_after, p_before, p_after)
    if fraction is not None:
        vehicle["x"] += (vx - vehicle["x"])*fraction
        person["y"] += (py - person["y"])*fraction
        contact_at = (world["sim_time_ms"] - TICK_MS) + TICK_MS*fraction
        world["s2_contact_at_ms"] = contact_at
        world["physical_contacts"].append({"sim_time_ms": contact_at,
                                           "object_ids": [vehicle["object_id"], person["object_id"]]})
    else:
        vehicle["x"] = vx
        person["y"] = py


def _enter_actor(world, actor, portal):
    from simulator.replay import recorded_clock
    world["pending_events"].append({"object_id": actor["object_id"],
        "event_type": "entered", "portal_id": portal,
        "sim_time_ms": world["sim_time_ms"], "observed_at": recorded_clock(world, "object_event"),
        "quality": {"visibility": "visible", "uncertainty_m": 0,
                    "missing_fields": []}})


def _advance_s3(world):
    state = DeviceState.model_validate(world["device_state"])
    entrance = next(g for g in state.gates if g.direction == "entry")
    exit_gate = next(g for g in state.gates if g.direction == "exit")
    moved_actors = set()
    for action in world["action_queue"]:
        if action["kind"] != "portal_attempt" or action["status"] not in {"moving", "waiting_at_gate"}:
            continue
        if action["object_id"] in moved_actors:
            continue
        moved_actors.add(action["object_id"])
        actor = next((a for a in world["actors"] if a["object_id"] == action["object_id"]), None)
        if actor is None:
            action["status"] = "completed"
            continue
        inbound = actor["object_id"] == "obj-car-s3-u"
        gate = entrance if inbound else exit_gate
        if gate.physical_state != "open" or (inbound and gate.entry_policy == "deny"):
            if inbound and actor["y"] < 0.5:
                after = dict(actor, y=min(0.5, actor["y"] + 2*TICK_MS/1000))
                if not any(swept_translation(actor, after).intersects(footprint(other))
                           for other in world["actors"] if other is not actor):
                    actor.update(after)
            action["status"] = "waiting_at_gate"
            continue
        step = 2*TICK_MS/1000 * (1 if inbound else -1)
        after = dict(actor, y=actor["y"] + step)
        corridor = box(28 if inbound else 31, -6, 31 if inbound else 34, 20)
        sweep = swept_translation(actor, after)
        if not corridor.covers(sweep) or any(
                sweep.intersects(footprint(other)) for other in world["actors"] if other is not actor):
            action["status"] = "blocked"
            world["movement_blocked"] = True
            continue
        actor.update(after)
        action["status"] = "moving"
        bounds = footprint(actor).bounds
        if inbound and not world["s3_entered"] and bounds[1] > 0:
            world["s3_entered"] = True
            _enter_actor(world, actor, "portal-entry")
        if inbound and actor["y"] >= 9:
            action["status"] = "completed"
        if not inbound and bounds[3] < 0:
            world["s3_exited"] = True
            action["status"] = "completed"
            _exit_actor(world, actor, "portal-exit")


def advance_environment(world):
    """Advance only internal fixture movement, then let world publish observations."""
    _activate_actions(world)
    ref = world.get("fixture_ref", "s1a-foundation-v1")
    if ref == "s1a-foundation-v1":
        return
    world["movement_blocked"] = False
    if ref.startswith("s2-"):
        _advance_s2(world)
        return
    if ref.startswith("s3-"):
        _advance_s3(world)
        return
    moved_actors = set()
    for action in world["action_queue"]:
        if action["status"] != "moving":
            continue
        if action["object_id"] in moved_actors:
            continue
        moved_actors.add(action["object_id"])
        actor = next((a for a in world["actors"] if a["object_id"] == action["object_id"]), None)
        if actor is None:
            action["status"] = "completed"
        else:
            _move_route(world, action, actor)


def _physical_gate_sensor(world, gate_id):
    target = next((g for g in MAP["gates"] if g["gate_id"] == gate_id), None)
    if target is None:
        return None
    p, q = target["segment"]
    sensor = box(p["x"], p["y"]-0.2, q["x"], q["y"]+0.2)
    return any(footprint(actor).intersects(sensor) for actor in world["actors"])


def _operate_checkpoint(world, action, **fields):
    from simulator.replay import recorded_clock
    world["device_op_seq"] = world.get("device_op_seq", 0) + 1
    command = DeviceCommand(action=action,
                            operation_id=f"sim0-{world['device_op_seq']}", **fields)
    state = DeviceState.model_validate(world["device_state"])
    result = operate_devices(state, command, now_utc=recorded_clock(world, "device_tick"),
                             sim_time_ms=world["sim_time_ms"])
    world["device_state"] = result.state.model_dump(mode="json")
    return result


def tick_environment_devices(world):
    """Generate bounded synthetic feedback, with a current physical stop sensor.

    Gate movement gets no permission from private truth. During an already
    moving gate transition, private collision geometry may only stop closure;
    confirmation still requires a complete current public observation.
    """
    state = DeviceState.model_validate(world["device_state"])
    for zone in list(state.alarms):
        for channel in ("visual", "audio"):
            if getattr(zone, channel) != "pending":
                continue
            _operate_checkpoint(world, "alarm_feedback", zone_id=zone.zone_id,
                                channel=channel,
                                feedback="failed" if world["device_faults"][channel]
                                else "on" if zone.desired_active else "off",
                                expected_version=zone.resource_version)
            state = DeviceState.model_validate(world["device_state"])
            zone = next(a for a in state.alarms if a.zone_id == zone.zone_id)
    for broadcast in list(state.broadcasts):
        if broadcast.simulated_playback == "pending":
            _operate_checkpoint(world, "broadcast_feedback",
                                broadcast_operation_id=broadcast.operation_id,
                                channel="simulated_playback",
                                feedback="failed" if world["device_faults"]["simulated_playback"]
                                else "played")
            state = DeviceState.model_validate(world["device_state"])
    for gate in list(state.gates):
        if gate.physical_state not in {"closing", "opening"} or gate.last_feedback == "unknown":
            # A stationary gate still has a sensor. Sample only the current
            # public observation; private geometry cannot authorize entry
            # restriction or closing. Fault/partial coverage stays unknown.
            observed = None if world["device_faults"]["gate"] else _observed_sensor(world, gate.gate_id)
            if gate.obstacle_detected is not observed:
                _operate_checkpoint(world, "tick", gate_id=gate.gate_id,
                                    expected_version=gate.resource_version,
                                    obstacle_detected=observed)
                state = DeviceState.model_validate(world["device_state"])
            continue
        observed = _observed_sensor(world, gate.gate_id)
        physical = _physical_gate_sensor(world, gate.gate_id)
        # A newly arrived body can force a stop before the next observation.
        sensor = True if physical is True else observed
        if gate.physical_state == "closing" and observed is None:
            sensor = None
        result = _operate_checkpoint(world, "tick", gate_id=gate.gate_id,
                                     expected_version=gate.resource_version,
                                     obstacle_detected=sensor)
        state = DeviceState.model_validate(world["device_state"])
        gate = next(g for g in state.gates if g.gate_id == gate.gate_id)
        if (result.outcome == "unknown" and gate.transition_target is not None
                and observed is False and physical is False):
            _operate_checkpoint(world, "gate_feedback", gate_id=gate.gate_id,
                                expected_version=gate.resource_version,
                                obstacle_detected=False,
                                feedback="failed" if world["device_faults"]["gate"]
                                else "played")
