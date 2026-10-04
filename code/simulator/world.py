from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import cos, radians, sin
from uuid import uuid4

from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from contracts.models import Observation
from simulator.spatial import POLICY as ANALYSIS_POLICY

FACILITY = "fac-demo-01"
MAP_VERSION = "map-01-draft"
TICK_MS = 100
OBSERVATION_MS = 200
CONFIG = {"tick_ms": TICK_MS, "observation_ms": OBSERVATION_MS,
          "movement": "straight-north-v1", "vehicle_speed_mps": 2.0}
OBSERVATION_POLICY = {"version": "observation-faults-v1", "delay_ms": 1200,
                      "history_limit": ANALYSIS_POLICY["history_limit"]}


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode()).hexdigest()


def rectangle(x1, y1, x2, y2):
    return [{"x": x1, "y": y1}, {"x": x2, "y": y1},
            {"x": x2, "y": y2}, {"x": x1, "y": y2}]


def make_map():
    regions = [
        ("aisle-west", "aisle", (0, 18, 8, 22)),
        ("aisle-central", "aisle", (8, 16, 32, 24)),
        ("bay-south-pocket", "parking_bay", (2.5, 17.5, 5.5, 18)),
        ("aisle-north", "aisle", (2.5, 22, 5.5, 32)),
        ("aisle-east", "aisle", (28, 4, 34, 24)),
        ("entry", "entrance", (28, 0, 31, 6)),
        ("exit", "exit", (31, 0, 34, 6)),
        ("walkway", "pedestrian", (20, 10, 24, 30)),
        ("announcement-a", "announcement", (0, 16, 20, 32)),
        ("announcement-b", "announcement", (20, 0, 40, 32)),
    ]
    regions += [(f"B{i+1:02}", "parking_bay", (8+3*i, 24, 11+3*i, 29))
                for i in range(6)]
    zones = [{"zone_id": name, "type": kind, "polygon": rectangle(*bounds)}
             for name, kind, bounds in regions]
    return {
        "facility_id": FACILITY, "map_version": MAP_VERSION,
        "coordinate_system": {"unit": "m", "origin": "southwest",
                              "x_axis": "east", "y_axis": "north"},
        "bounds": {"min_x": 0, "min_y": 0, "max_x": 40, "max_y": 32},
        "zones": zones,
        "parking_bays": [{"zone_id": f"B{i+1:02}"} for i in range(6)],
        "lanes": [
            {"zone_id": "aisle-west", "connected_to": ["aisle-central", "aisle-north"]},
            {"zone_id": "aisle-north", "connected_to": ["portal-north"], "direction": "north"},
            {"zone_id": "aisle-central", "connected_to": ["aisle-west", "aisle-east"]},
            {"zone_id": "aisle-east", "connected_to": ["aisle-central", "entry", "exit"]},
        ],
        "gates": [{"gate_id": "gate-in-01", "zone_id": "entry",
                   "segment": rectangle(28, 3, 31, 3)[:2]},
                  {"gate_id": "gate-out-01", "zone_id": "exit",
                   "segment": rectangle(31, 3, 34, 3)[:2]}],
        "announcement_zones": ["announcement-a", "announcement-b"],
        "portals": [
            {"portal_id": name, "boundary_segment": [{"x": a, "y": b}, {"x": c, "y": d}],
             "allowed_object_types": ["vehicle"], "direction": direction}
            for name, a, b, c, d, direction in [
                ("portal-west", 0, 18, 0, 22, "both"),
                ("portal-north", 2.5, 32, 5.5, 32, "exit"),
                ("portal-entry", 28, 0, 31, 0, "entry"),
                ("portal-exit", 31, 0, 34, 0, "exit")]],
        "route_policy_version": "route-foundation-v1",
    }


MAP = make_map()
# Only this supported northbound route has an external portal continuation.
NORTH_ROUTE = box(2.5, 17.5, 5.5, 38)


def footprint(actor):
    angle = radians(actor["heading_deg"])
    points = []
    for u, v in [(-1, -1), (1, -1), (1, 1), (-1, 1)]:
        dx, dy = u*actor["length_m"]/2, v*actor["width_m"]/2
        points.append((actor["x"]+dx*cos(angle)-dy*sin(angle),
                       actor["y"]+dx*sin(angle)+dy*cos(angle)))
    return Polygon(points)


def swept_translation(before, after):
    """Exact occupied sweep for a convex body translated without rotating."""
    if before["heading_deg"] != after["heading_deg"]:
        raise ValueError("Rotation is not supported by the foundation movement model")
    return unary_union([footprint(before), footprint(after)]).convex_hull


def initial_world(seed, fixture_ref="s1a-foundation-v1"):
    """Create a JSON-checkpointable synthetic run from a supported fixture."""
    from simulator.environment import FIXTURE_REFS, initialize_environment

    if fixture_ref not in FIXTURE_REFS:
        raise ValueError("Unsupported fixture_ref")
    world = {
        "run_id": "run-"+uuid4().hex, "seed": seed, "run_status": "paused",
        "recovery_required": False, "state_version": 0, "sim_time_ms": 0,
        "configuration_version": "foundation-v1", "configuration_digest": digest(CONFIG),
        "map_version": MAP_VERSION, "map_digest": digest(MAP),
        "behavior_policy_version": "straight-north-v1", "behavior_digest": digest("straight-north-v1"),
        "actors": [
            {"actor_id": "internal-a", "object_id": "obj-car-01", "object_type": "vehicle",
             "x": 9.5, "y": 26.5, "length_m": 4.6, "width_m": 1.8, "heading_deg": 90.0},
            {"actor_id": "internal-b", "object_id": "obj-car-02", "object_type": "vehicle",
             "x": 4.0, "y": 20.0, "length_m": 4.6, "width_m": 1.8, "heading_deg": 90.0},
            {"actor_id": "internal-p", "object_id": "obj-person-01", "object_type": "pedestrian",
             "x": 22.0, "y": 12.0, "length_m": 0.6, "width_m": 0.6, "heading_deg": 90.0},
        ],
        "move_requested": False, "movement_blocked": False, "pending_events": [],
        "observation_mode": "normal", "observation_queue": [], "observation_history": [],
        "observation_policy_digest": digest(OBSERVATION_POLICY),
        "replay_clock_version": 2,
    }
    initialize_environment(world, fixture_ref)
    observe(world)
    return world


def valid_behavior_digest(world):
    """Accept the exact legacy hash or one supported SIM-0 fixture checkpoint."""
    from simulator.environment import validate_environment_checkpoint

    return validate_environment_checkpoint(world)


def prepare_observation_state(world):
    """Add observation-only fields to a v1 foundation checkpoint without resetting it."""
    if world.get("observation_policy_digest", digest(OBSERVATION_POLICY)) != digest(OBSERVATION_POLICY):
        raise RuntimeError("Observation policy mismatch; explicit migration required")
    world["observation_policy_digest"] = digest(OBSERVATION_POLICY)
    world.setdefault("observation_mode", "normal")
    # Restart is an evidence boundary. Pre-restart time never proves a new hold.
    world["observation_queue"] = []
    world["observation_history"] = []
    from simulator.environment import prepare_environment_restart
    prepare_environment_restart(world)


def set_observation_mode(world, mode):
    from simulator.replay import record_input
    if mode != world.get("observation_mode", "normal"):
        record_input(world, "observation_mode", {"mode": mode})
    if mode != world.get("observation_mode", "normal"):
        world["observation_queue"] = []
        world["observation_history"] = []
    world["observation_mode"] = mode


def publish_observation(world, observation):
    # Delayed delivery can never replace an observation with an older version.
    previous = world.get("observation")
    if previous and observation["state_version"] <= previous["state_version"]:
        return
    if previous and observation["sim_time_ms"] < previous["sim_time_ms"]:
        return
    if (previous and observation["sim_time_ms"] == previous["sim_time_ms"]
            and observation["coverage"] == "complete"):
        # A device command can publish another version in one simulation tick.
        # Preserve previously emitted portal events without duplicating the
        # time point used by history-based motion analyzers.
        known = {(e["object_id"], e["event_type"], e["portal_id"], e["sim_time_ms"])
                 for e in observation["object_events"]}
        for event in previous["object_events"]:
            key = (event["object_id"], event["event_type"], event["portal_id"], event["sim_time_ms"])
            if key not in known:
                observation["object_events"].append(deepcopy(event))
                known.add(key)
    world["observation"] = observation
    history = world.setdefault("observation_history", [])
    if history and history[-1]["sim_time_ms"] == observation["sim_time_ms"]:
        history[-1] = deepcopy(observation)
    else:
        history.append(deepcopy(observation))
    del history[:-OBSERVATION_POLICY["history_limit"]]


def observe(world):
    from simulator.replay import recorded_clock
    now = recorded_clock(world, "observation")
    objects = [{"object_id": a["object_id"], "object_type": a["object_type"],
                "position": {"x": a["x"], "y": a["y"]},
                "size": {"length_m": a["length_m"], "width_m": a["width_m"]},
                "heading_deg": a["heading_deg"],
                "quality": {"visibility": "visible", "uncertainty_m": 0, "missing_fields": []}}
               for a in world["actors"]
               if not (world.get("fixture_ref", "").startswith("s3-")
                       and (a["y"] + a["length_m"]/2 < 0 or a["y"] - a["length_m"]/2 > 32))]
    from simulator.environment import gate_observations
    observation = Observation(
        facility_id=FACILITY, run_id=world["run_id"],
        observation_id=f"obs-{world['run_id']}-{world['state_version']}",
        map_version=MAP_VERSION, state_version=world["state_version"],
        sim_time_ms=world["sim_time_ms"], observed_at=now, received_at=now,
        coverage="complete", objects=objects,
        devices=gate_observations(world, now), object_events=world["pending_events"],
    ).model_dump()
    world["pending_events"] = []
    mode = world.get("observation_mode", "normal")
    if mode == "occluded_vehicle":
        for obj in observation["objects"]:
            if obj["object_id"] == "obj-car-02":
                obj.update(position=None, size=None, heading_deg=None,
                           quality={"visibility": "occluded", "uncertainty_m": None,
                                    "missing_fields": ["position", "size", "heading_deg"]})
        observation["coverage"] = "partial"
        observation["object_events"] = [e for e in observation["object_events"] if e["object_id"] != "obj-car-02"]
    elif mode == "missing_vehicle":
        observation["objects"] = [o for o in observation["objects"] if o["object_id"] != "obj-car-02"]
        observation["coverage"] = "partial"
        observation["object_events"] = [e for e in observation["object_events"] if e["object_id"] != "obj-car-02"]
    elif mode in ("occluded_pedestrian", "missing_pedestrian"):
        for obj in list(observation["objects"]):
            if obj["object_type"] != "pedestrian":
                continue
            if mode == "missing_pedestrian":
                observation["objects"].remove(obj)
            else:
                obj.update(position=None, size=None, heading_deg=None,
                           quality={"visibility": "occluded", "uncertainty_m": None,
                                    "missing_fields": ["position", "size", "heading_deg"]})
        observation["coverage"] = "partial"
        observation["object_events"] = [e for e in observation["object_events"]
                                        if e["object_id"] not in {a["object_id"] for a in world["actors"]
                                                                   if a["object_type"] == "pedestrian"}]
    if (world.get("fixture_ref") == "s2-occluded-v1"
            and 200 <= world["sim_time_ms"] < 2000 and mode == "normal"):
        for obj in observation["objects"]:
            if obj["object_type"] == "pedestrian":
                obj.update(position=None, size=None, heading_deg=None,
                           quality={"visibility": "occluded", "uncertainty_m": None,
                                    "missing_fields": ["position", "size", "heading_deg"]})
                observation["coverage"] = "partial"
    elif mode == "unavailable":
        observation.update(coverage="unavailable", objects=[], devices=[], object_events=[])
    if mode == "delayed":
        queue = world.setdefault("observation_queue", [])
        queue.append(observation)
        if queue[0]["sim_time_ms"] + OBSERVATION_POLICY["delay_ms"] > world["sim_time_ms"]:
            return
        observation = queue.pop(0)
        observation["received_at"] = now
    publish_observation(world, Observation.model_validate(observation).model_dump())


def advance(world):
    """One fixed step, with full-body sweep; no catch-up teleport on wall delay."""
    world["sim_time_ms"] += TICK_MS
    world["state_version"] += 1
    from simulator.environment import advance_environment, tick_environment_devices
    if world.get("fixture_ref", "s1a-foundation-v1") != "s1a-foundation-v1":
        advance_environment(world)
        tick_environment_devices(world)
        if world["sim_time_ms"] % OBSERVATION_MS == 0:
            observe(world)
        return
    advance_environment(world)
    moving = next((a for a in world["actors"] if a["object_id"] == "obj-car-02"), None)
    if moving and world["move_requested"]:
        after = dict(moving, y=moving["y"] + CONFIG["vehicle_speed_mps"]*TICK_MS/1000)
        sweep = swept_translation(moving, after)
        blocked = not NORTH_ROUTE.covers(sweep) or any(
            sweep.intersects(footprint(other)) for other in world["actors"] if other is not moving)
        world["movement_blocked"] = blocked
        if not blocked:
            moving.update(after)
            # Boundary passage is emitted only after the entire observed body exits.
            if footprint(moving).bounds[1] > 32:
                from simulator.replay import recorded_clock
                world["pending_events"].append({"object_id": moving["object_id"],
                    "event_type": "exited", "portal_id": "portal-north",
                    "sim_time_ms": world["sim_time_ms"],
                    "observed_at": recorded_clock(world, "object_event"),
                    "quality": {"visibility": "visible", "uncertainty_m": 0, "missing_fields": []}})
                world["actors"].remove(moving)
                for action in world.get("action_queue", []):
                    if action["object_id"] == moving["object_id"] and action["status"] == "moving":
                        action["status"] = "completed"
    tick_environment_devices(world)
    if world["sim_time_ms"] % OBSERVATION_MS == 0:
        observe(world)


def public_state(world):
    # Explicit projection: no seed, scenario, internal actors or future movement.
    return {"snapshot": deepcopy(world["observation"]), "run_status": world["run_status"],
            "recovery_required": world["recovery_required"],
            "applied_state_version": world["state_version"],
            "applied_sim_time_ms": world["sim_time_ms"]}
