"""Private, bounded SIM-0 input trace. Replay creates no business or model jobs."""
from copy import deepcopy


MAX_INPUTS = 512
MAX_CLOCKS = 65536
CLOCK_PURPOSES = {"observation", "safety", "safety_device", "object_event", "device_tick"}
KINDS = {"vehicle_action", "cancel_action", "device_command", "device_fault", "s2_reaction", "observation_mode",
         "restart_boundary", "recovery_resume"}


def record_input(world, kind, payload, *, occurred_at=None):
    if world.get("replay_state") is not None:
        return
    if kind not in KINDS:
        raise ValueError("Unsupported replay input")
    entries = world.setdefault("recorded_inputs", [])
    if len(entries) >= MAX_INPUTS:
        if kind in {"restart_boundary", "recovery_resume"}:
            world["replay_input_overflow"] = True
            return
        raise ValueError("Replay input history is full")
    from simulator.world import utc_now
    entries.append({"sequence": len(entries), "sim_time_ms": world["sim_time_ms"],
                    "occurred_at": occurred_at or utc_now(),
                    "kind": kind, "payload": deepcopy(payload)})


def recorded_clock(world, purpose):
    """Keep wall time for every environment and safety side effect."""
    if purpose not in CLOCK_PURPOSES:
        raise ValueError("Unsupported replay clock purpose")
    from simulator.world import utc_now
    replay = world.get("replay_state")
    if replay is not None:
        clocks = world.get("recorded_clocks", [])
        if not clocks:
            return utc_now()  # Checkpoints from before wall-clock tracing.
        cursor = replay["clock_cursor"]
        if cursor >= len(clocks):
            raise ValueError("Replay clock history exhausted")
        entry = clocks[cursor]
        if entry["purpose"] != purpose or entry["sim_time_ms"] != world["sim_time_ms"]:
            raise ValueError("Replay clock history diverged")
        replay["clock_cursor"] += 1
        replay["current_utc"] = entry["utc"]
        return entry["utc"]
    now = utc_now()
    clocks = world.setdefault("recorded_clocks", [])
    if len(clocks) >= MAX_CLOCKS:
        world["replay_clock_overflow"] = True
    else:
        clocks.append({"sim_time_ms": world["sim_time_ms"], "purpose": purpose, "utc": now})
    return now


def safety_clock_available(world):
    replay = world.get("replay_state")
    if replay is None:
        return True
    clocks = world.get("recorded_clocks", [])
    if not clocks:
        return True
    cursor = replay["clock_cursor"]
    return (cursor < len(clocks) and clocks[cursor]["purpose"] == "safety"
            and clocks[cursor]["sim_time_ms"] == world["sim_time_ms"])


def prepare_replay(source, candidate):
    if "recorded_epoch_utc" not in source:
        raise ValueError("Legacy checkpoint has no replay input trace")
    if source.get("replay_clock_overflow"):
        raise ValueError("Replay clock history exceeded its bound")
    if source.get("replay_input_overflow"):
        raise ValueError("Replay input history exceeded its bound")
    if source.get("replay_clock_version") != 2:
        raise ValueError("Legacy checkpoint lacks complete wall-clock evidence")
    entries = deepcopy(source.get("recorded_inputs", []))
    if len(entries) > MAX_INPUTS:
        raise ValueError("Invalid replay history")
    previous = 0
    from contracts.models import utc_timestamp
    for index, entry in enumerate(entries):
        clock = entry.get("sim_time_ms")
        if (entry.get("sequence") != index or type(clock) is not int
                or not previous <= clock <= source["sim_time_ms"] or clock % 100
                or entry.get("kind") not in KINDS or not isinstance(entry.get("payload"), dict)):
            raise ValueError("Invalid replay history")
        previous = clock
        utc_timestamp(entry.get("occurred_at"))
    clocks = deepcopy(source.get("recorded_clocks", []))
    if len(clocks) > MAX_CLOCKS:
        raise ValueError("Invalid replay clocks")
    previous_clock = 0
    for entry in clocks:
        clock = entry.get("sim_time_ms") if isinstance(entry, dict) else None
        if (type(clock) is not int or clock < previous_clock or clock > source["sim_time_ms"]
                or clock % 100 or entry.get("purpose") not in CLOCK_PURPOSES):
            raise ValueError("Invalid replay clocks")
        utc_timestamp(entry.get("utc"))
        previous_clock = clock
    if clocks and (clocks[0]["purpose"] != "observation" or clocks[0]["sim_time_ms"] != 0):
        raise ValueError("Invalid initial replay clock")
    if not clocks:
        raise ValueError("Missing replay clocks")
    candidate["recorded_inputs"] = entries
    candidate["recorded_clocks"] = clocks
    epoch = source.get("recorded_epoch_utc", candidate["device_state"]["now_utc"])
    candidate["device_state"]["now_utc"] = epoch
    candidate["replay_state"] = {"cursor": 0, "end_sim_time_ms": source["sim_time_ms"],
                                 "source_run_id": source["run_id"], "current_utc": epoch,
                                 "clock_cursor": 1 if clocks else 0}
    if clocks:
        initial = candidate["observation"]
        initial["observed_at"] = clocks[0]["utc"]
        initial["received_at"] = clocks[0]["utc"]
        for device in initial["devices"]:
            device["observed_at"] = clocks[0]["utc"]
        candidate["observation_history"] = [deepcopy(initial)]


def apply_replay_inputs(world):
    replay = world.get("replay_state")
    if replay is None:
        return
    from simulator.environment import (_queue_action, apply_device_command,
        cancel_vehicle_action, configure_s2_reaction, set_synthetic_fault)
    from simulator.world import set_observation_mode, utc_now
    entries = world["recorded_inputs"]
    while replay["cursor"] < len(entries):
        entry = entries[replay["cursor"]]
        if entry["sim_time_ms"] > world["sim_time_ms"]:
            break
        payload, kind = entry["payload"], entry["kind"]
        replay["current_utc"] = max(replay["current_utc"], entry["occurred_at"])
        if kind == "vehicle_action":
            _queue_action(world, **payload)
        elif kind == "cancel_action":
            cancel_vehicle_action(world, payload["action_key"])
        elif kind == "device_command":
            apply_device_command(world, payload, now_utc=utc_now())
        elif kind == "device_fault":
            set_synthetic_fault(world, **payload)
        elif kind == "s2_reaction":
            configure_s2_reaction(world, **payload)
        elif kind == "observation_mode":
            set_observation_mode(world, payload["mode"])
        elif kind == "restart_boundary":
            world["observation_queue"] = []
            world["observation_history"] = []
            world["recovery_required"] = True
            safety = world.get("safety_state", {})
            for claim in safety.get("claims", {}).values():
                claim["clear_since_ms"] = None
            safety["last_observation_id"] = None
            safety.pop("last_sim_time_ms", None)
        elif kind == "recovery_resume":
            world["recovery_required"] = False
        replay["cursor"] += 1
