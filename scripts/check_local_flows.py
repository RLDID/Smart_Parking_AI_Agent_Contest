"""Bounded, provider-free product flow evidence; no default DB or server.

These are supplemental measurements, not replacements for frozen acceptance IDs.
Run with Python -X utf8. Each case owns and removes a temporary SQLite directory.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import hashlib
from pathlib import Path
import sys
import subprocess
import tempfile
import traceback
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from backend.auth import ApiError, Auth
from backend.business import WebInbox
from backend.relationships import Relationships
from backend.runtime import Runtime
from contracts.autonomous import AutonomousControl
from contracts.business import NotifyVehicle, ReceiptInput, ResponseInput
from contracts.relationships import PersonMappingChange, VehicleUserChange
from contracts.synthetic_users import SyntheticUserInput
from simulator.world import FACILITY, public_state

FIXTURES = {"s1a": "s1a-foundation-v1", "s1b": "s1b-blocked-v1", "s1c": "s1c-overlap-v1"}
IMPACTS = {"s1a": ("aisle_obstruction", "aisle-west"), "s1b": ("exit_blocked", "B01"), "s1c": ("bay_intrusion", "B01")}
CASES = tuple([f"t04-{s}" for s in FIXTURES] + ["t04-delay"] +
              [f"silent-{s}" for s in FIXTURES] +
              ["t07-replace", "t07-unlink", "person-review", "t20-new-impact", "v02-restart", "v01-concurrent"])


class FlowFailure(AssertionError):
    pass


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 4, tzinfo=timezone.utc)

    def __call__(self):
        return self.value.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def advance(self, milliseconds):
        self.value += timedelta(milliseconds=milliseconds)

    def set(self, value, offset_ms=0):
        self.value = datetime.fromisoformat(value.replace("Z", "+00:00")) + timedelta(milliseconds=offset_ms)


class RecordingInbox(WebInbox):
    def __init__(self):
        self.sent = []

    async def send(self, notification_id, message):
        self.sent.append({"notification_id": notification_id, "message": deepcopy(message)})
        return await super().send(notification_id, message)


class Rig:
    def __init__(self, directory, observations):
        self.path = Path(directory) / "flow.sqlite3"
        self.observations = observations
        self.clock = Clock()
        self.inbox = RecordingInbox()
        self.stack = ExitStack()
        self.r = None
        self.serial = 0

    def __enter__(self):
        # One process, sequential cases: wall clocks share the declared test UTC.
        for target in ("simulator.world.utc_now", "simulator.environment.utc_now",
                       "backend.runtime.utc_now", "backend.registry.utc_now", "backend.relationships._now"):
            self.stack.enter_context(patch(target, self.clock))
        clock = self.clock
        class MeasurementDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock.value.astimezone(tz) if tz else clock.value.replace(tzinfo=None)
        for target in ("simulator.spatial.datetime", "backend.operating_analysis.datetime"):
            self.stack.enter_context(patch(target, MeasurementDateTime))
        try:
            self.open()
        except BaseException:
            try:
                if self.r:
                    self.r.store.close()
            finally:
                self.stack.close()
            raise
        return self

    def __exit__(self, *args):
        try:
            if self.r:
                try:
                    reader = self.r.knowledge._reader
                    if reader is not None:
                        reader.join(timeout=1)
                    resources = {"query_jobs": len(self.r.queries.active),
                        "autonomous_jobs": len(self.r.autonomous.active),
                        "knowledge_reader_alive": bool(reader and reader.is_alive())}
                    self.record("resources.before_close", resources)
                    if resources != {"query_jobs": 0, "autonomous_jobs": 0, "knowledge_reader_alive": False}:
                        raise FlowFailure("Measurement resources still active at close")
                finally:
                    self.r.store.close()
        finally:
            self.stack.close()

    def open(self):
        self.r = Runtime(self.path)
        def sqlite_clock(format_string, value):
            if (format_string, value) != ("%Y-%m-%dT%H:%M:%fZ", "now"):
                raise ValueError("Unsupported SQLite measurement clock")
            return self.clock.value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        # Match schema timestamp defaults to the same virtual wall clock;
        # leave product INSERTs, constraints and transactions intact.
        self.r.store.db.create_function("strftime", 2, sqlite_clock)
        self.r.business.clock = self.clock
        self.r.knowledge.clock = self.clock
        self.r.business.channel = self.inbox
        self.auth = Auth(self.r.store)
        logins = {name: self.auth.login(name, "parking-demo-only", "local-flow")
                  for name in ("demo-operator", "demo-owner", "demo-driver", "demo-driver-2")}
        self.sessions = {name: value[1] for name, value in logins.items()}
        self.tokens = {name: value[0] for name, value in logins.items()}
        self.operator = self.sessions["demo-operator"]

    @property
    def db(self):
        return self.r.store.db

    @property
    def run(self):
        return self.r.world["run_id"]

    def key(self, prefix):
        self.serial += 1
        return f"flow-{prefix}-{self.serial}"

    def record(self, stage, actual):
        self.observations.append({"stage": stage, "sim_time_ms": self.r.world["sim_time_ms"] if self.r.world else None,
                                  "wall_at": self.clock(), "actual": deepcopy(actual)})

    def check(self, name, actual, expected):
        self.record(name, {"value": actual, "expected": expected, "matches": actual == expected})
        if actual != expected:
            raise FlowFailure(f"{name}: expected {expected!r}, observed {actual!r}")

    async def create(self, scenario="s1a"):
        await self.r.mutate(self.operator, self.key("create"), "create", {"seed": 42, "fixture_ref": FIXTURES[scenario]})
        await self.step(60)
        self.record("candidate", self.assessment(scenario))
        self.check("candidate.support", self.assessment(scenario)["support_status"], "supported")
        self.check("candidate.violation", self.assessment(scenario)["violation_candidate"], True)

    async def step(self, count, **params):
        for index in range(count):
            arguments = {"action": "step"}
            if params and index == 0:
                arguments["action_params"] = params
            await self.r.mutate(self.operator, self.key("step"), "control", arguments, self.run)

    def assessment(self, scenario):
        kind, zone = IMPACTS[scenario]
        return self.r.business.impact_assessment(kind, "obj-car-02", zone)

    async def agent(self, scenario="s1a"):
        result = await self.r.autonomous.control(self.operator,
            AutonomousControl(run_id=self.run, action="process", mode="mock", scenario=scenario), self.key("agent"),
            lambda: self.auth.require(self.tokens["demo-operator"]))
        self.record("agent." + scenario, result)
        self.check("agent.provider_calls", result["model"]["model_call_count"], 0)
        return result

    def count(self, table, where="1=1"):
        return self.db.execute(f"SELECT count(*) FROM {table} WHERE {where}").fetchone()[0]

    def status(self, iid):
        return self.r.business.scoped("incidents", iid, "incident_id")["status"]

    def configure(self, mode, delay=0):
        return self.r.synthetic_users.configure(self.operator, self.run, SyntheticUserInput(
            mode=mode, movement_delay_ms=delay, expected_state_version=self.r.world["state_version"]), self.key("users"))

    async def notify(self, scenario="s1a"):
        result = await self.agent(scenario)
        self.check("notice.accepted", result["status"], "accepted")
        return result, self.db.execute("SELECT notification_id FROM notifications WHERE purpose='move_request' ORDER BY rowid DESC LIMIT 1").fetchone()[0]

    async def deliver(self):
        result = await self.r.business.deliver_one()
        self.record("dispatch", {"returned": result, "send_count": len(self.inbox.sent),
            "notifications": [dict(x) for x in self.db.execute("SELECT notification_id,recipient_user_id,contact_sequence,delivery_status,response_due_at FROM notifications ORDER BY rowid")]})
        return result

    async def tool(self, name, arguments):
        return await self.r.business_tool(self.operator, name, arguments, self.key(name), self.r.read_task(self.operator, self.run))

    def context(self):
        return {"facility_id": FACILITY, "run_id": self.run, "based_on_state_version": self.r.world["state_version"],
                "policy_version": self.r.knowledge.current_policy(FACILITY).policy_version}

    async def incident(self, impacts, *, iid=None, status="active"):
        evidence = sorted({oid for kind, zone in impacts for oid in
            self.r.business.impact_assessment(kind, "obj-car-02", zone)["observation_ids"]})
        args = self.context() | {"primary_object_id": "obj-car-02", "status": status,
            "impacts": [{"type": kind, "zone_id": zone} for kind, zone in impacts],
            "evidence_ids": evidence, "reason_summary": "보충 가상 흐름의 현재 영향 재평가"}
        if iid:
            args.update(incident_id=iid, expected_resource_version=self.r.business.scoped("incidents", iid, "incident_id")["resource_version"])
        result = await self.tool("create_or_update_incident", args)
        self.record("incident", result)
        return result["result"]

async def movement(rig, scenario):
    await rig.create(scenario)
    rig.configure("will_move")
    result, nid = await rig.notify(scenario)
    iid = result["incident_id"]
    before = deepcopy(rig.r.world["actors"])
    await rig.deliver()
    rig.r.synthetic_users.process()
    rig.r.synthetic_users.process()
    rig.check("reply_is_not_motion", rig.r.world["actors"], before)
    rig.check("synthetic.once", rig.count("synthetic_inbox_events"), 1)
    rig.check("receipt.once", rig.count("notification_receipts"), 1)
    rig.check("response.once", rig.count("notification_responses"), 1)
    rig.check("receipt.delivery", rig.r.business.scoped("notifications", nid, "notification_id")["delivery_status"], "client_received")
    rig.check("reply_is_not_resolution", rig.status(iid), "active")
    await rig.step(25)
    rig.record("response_plus_25_ticks", {"assessment": rig.assessment(scenario), "incident_status": rig.status(iid), "actions": rig.r.world.get("action_queue")})
    await rig.agent(scenario)
    rig.check("25ticks.not_resolved", rig.status(iid) == "resolved", False)
    ticks = 25
    while ticks < 400 and not rig.assessment(scenario)["clearance_sustained"]:
        await rig.step(5)
        ticks += 5
    rig.record("movement.final", {"post_response_ticks": ticks, "assessment": rig.assessment(scenario), "actions": rig.r.world.get("action_queue")})
    rig.check("fresh.sustained_clearance", rig.assessment(scenario)["clearance_sustained"], True)
    await rig.agent(scenario)
    rig.check("incident.resolved", rig.status(iid), "resolved")
    rig.check("contact.once", rig.count("notifications", "purpose='move_request'"), 1)


async def delayed(rig):
    await rig.create()
    rig.configure("will_move", 1000)
    await rig.notify()
    await rig.deliver()
    rig.r.synthetic_users.process()
    before = deepcopy(rig.r.world["actors"])
    action = rig.r.world["action_queue"][0]
    rig.record("delay.deadline", action)
    start = rig.r.world["sim_time_ms"]
    await rig.step(9)
    rig.check("delay.before.time", rig.r.world["sim_time_ms"] - start, 900)
    rig.check("delay.before.pose", rig.r.world["actors"], before)
    await rig.step(1)
    rig.check("delay.at.time", rig.r.world["sim_time_ms"], action["apply_at_ms"])
    rig.check("delay.at.motion", rig.r.world["actors"] != before, True)
    await rig.step(2)
    rig.check("delay.after.time", rig.r.world["sim_time_ms"] - start, 1200)
    rig.check("delay.after.motion", rig.r.world["actors"] != before, True)
    rig.check("delay.contact.once", rig.count("notifications"), 1)


async def silent(rig, scenario):
    await rig.create(scenario)
    rig.configure("silent")
    result, nid = await rig.notify(scenario)
    iid = result["incident_id"]
    rules = rig.r.knowledge.current_policy(FACILITY).execution_rules.model_dump()
    rig.record("wall_policy", rules)
    await rig.deliver()
    rig.r.synthetic_users.process()
    due = rig.r.business.scoped("notifications", nid, "notification_id")["response_due_at"]
    rig.clock.set(due, -1)
    rig.r.business.process_followups()
    rig.check("followup.before", rig.count("followups", "status='completed'"), 0)
    await rig.agent(scenario)
    rig.check("silent.before.contact", rig.count("notifications", "purpose='move_request'"), 1)
    rig.clock.set(due)
    rig.r.business.process_followups()
    rig.record("followup.at", [dict(x) for x in rig.db.execute("SELECT * FROM followups")])
    rig.check("followup.at.once", rig.count("followups", "status='completed'"), 1)
    await rig.agent(scenario)
    await rig.deliver()
    if scenario == "s1a":
        rig.check("silent.retry.contact", rig.count("notifications", "purpose='move_request'"), rules["contact_max_sequence"])
        latest = rig.db.execute("SELECT response_due_at FROM notifications WHERE purpose='move_request' ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        rig.clock.set(latest, 1)
        rig.r.business.process_followups()
        await rig.agent(scenario)
        await rig.deliver()
    else:
        rig.clock.advance(1)
    await rig.agent(scenario)
    rig.r.business.process_followups()
    rig.check("silent.owner_report.once", rig.count("notifications", "purpose='owner_report'"), 1)
    rig.check("silent.receipt.none", rig.count("notification_receipts"), 0)
    rig.check("silent.response.none", rig.count("notification_responses"), 0)
    rig.check("silent.not_resolved", rig.status(iid) in ("active", "monitoring", "needs_review", "escalated"), True)
    followups = [dict(x) for x in rig.db.execute("SELECT * FROM followups")]
    rig.check("followup.attempt_limit", [x["attempt_count"] for x in followups], [1] * len(followups))
    review_plans = rig.db.execute("SELECT status FROM plans WHERE trigger_followup_id IS NOT NULL").fetchall()
    rig.check("followup.review_held", [x[0] for x in review_plans], ["held"] * len(review_plans))
    # Measure the actual dispatch guard's independent overall deadline.
    first = rig.db.execute("SELECT e.payload_json,i.created_at FROM executions e JOIN incidents i USING(incident_id) WHERE e.tool_name='notify_vehicle_user' ORDER BY e.rowid LIMIT 1").fetchone()
    arguments = json.loads(first["payload_json"])
    arguments.pop("_server")
    arguments["plan_id"] = None
    arguments["expected_resource_version"] = rig.r.business.scoped("incidents", iid, "incident_id")["resource_version"]
    notice = NotifyVehicle.model_validate(arguments)
    for offset, expected in ((-1, "allowed"), (0, "INCIDENT_CONTACT_EXPIRED"), (1, "INCIDENT_CONTACT_EXPIRED")):
        rig.clock.set(first["created_at"], rules["overall_timeout_wall_ms"] + offset)
        try:
            rig.r.business.notification_guard(notice, rig.operator.username)
            observed = "allowed"
        except ApiError as error:
            observed = error.code
        rig.check(f"overall.guard.{offset}", observed, expected)
    rig.check("overall.no_extra_contact", rig.count("notifications", "purpose='move_request'"), 2 if scenario == "s1a" else 1)
    rig.record("silent.final", {"incident_status": rig.status(iid), "followups": [dict(x) for x in rig.db.execute("SELECT * FROM followups")],
        "plans": [dict(x) for x in rig.db.execute("SELECT status,steps_json FROM plans")]})


async def relationship(rig, unlink):
    await rig.create()
    result, old_nid = await rig.notify()
    iid = result["incident_id"]
    rig.clock.advance(1)
    rel = Relationships(rig.db)
    body = VehicleUserChange(expected_version=0, user_id=None if unlink else "demo-driver-2", reason="가상 관계 변경 시험")
    changed = rel.execute("demo-operator", rig.key("transfer"), "vehicle.user", body.model_dump(),
        lambda: rel.vehicle_user_change("demo-operator", "veh-demo-02", body))
    rig.record("relationship.changed", changed)
    await rig.deliver()
    rig.check("old_recipient.no_send", len(rig.inbox.sent), 0)
    rig.check("old_recipient.no_access", rig.r.business.notifications("demo-driver")["items"], [])
    try:
        rig.r.business.reply(rig.sessions["demo-driver"], old_nid, "response", ResponseInput(client_request_id="revoked", response="will_move"), rig.key("revoked"))
    except ApiError as error:
        rig.record("old_recipient.reply_denied", {"code": error.code})
    else:
        raise FlowFailure("Revoked recipient reply succeeded")
    if unlink:
        try:
            rig.r.business.resolve_recipient("obj-car-02")
        except ApiError as error:
            rig.check("unlink.no_recipient", error.code, "RECIPIENT_UNVERIFIED")
        else:
            raise FlowFailure("Unlinked vehicle still has a recipient")
        await rig.agent()
        rig.check("unlink.owner_report", rig.count("notifications", "purpose='owner_report'"), 1)
        rig.check("unlink.not_resolved", rig.status(iid) == "resolved", False)
        return
    await rig.step(2)  # A newly assigned driver must receive fresh observations.
    await rig.agent()
    new_nid = rig.db.execute("SELECT notification_id FROM notifications WHERE purpose='move_request' ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    await rig.deliver()
    rig.check("new_recipient.send.once", len(rig.inbox.sent), 1)
    items = rig.r.business.notifications("demo-driver-2")["items"]
    rig.check("new_recipient.inbox", [x["notification_id"] for x in items], [new_nid])
    receipt = rig.r.business.reply(rig.sessions["demo-driver-2"], new_nid, "receipt",
        ReceiptInput(client_request_id="current-receipt", received_at=rig.clock()), rig.key("receipt"))
    response = rig.r.business.reply(rig.sessions["demo-driver-2"], new_nid, "response",
        ResponseInput(client_request_id="current-response", response="will_move"), rig.key("response"))
    rig.record("new_recipient.receipt_response", {"receipt": receipt, "response": response})
    rig.check("new_recipient.receipt.once", rig.count("notification_receipts"), 1)
    rig.check("new_recipient.response.once", rig.count("notification_responses"), 1)
    rig.check("new_recipient.not_resolved", rig.status(iid), "active")


async def person(rig):
    await rig.create()
    rel = Relationships(rig.db)
    before = rig.r.store.registry.scope_stamp("demo-driver-2")
    def change(version, status, source, user):
        body = PersonMappingChange(run_id=rig.run, expected_version=version, status=status, source=source,
            user_id=user, reason="수동 가상 사람 연결 검증")
        value = rel.execute("demo-operator", rig.key("person"), "person.change", body.model_dump(),
            lambda: rel.person_change("demo-operator", "obj-person-01", body, rig.r.world))
        rig.record("person." + status, value)
        return value
    change(0, "proposed", "demo_config", "demo-driver-2")
    rig.check("person.proposed.no_review", rig.db.execute("SELECT reviewed_by FROM person_mappings").fetchone()[0], None)
    await rig.step(2)
    change(1, "verified", "reviewed", "demo-driver-2")
    row = rig.db.execute("SELECT mapping_status,mapping_source,reviewed_by,reviewed_at FROM person_mappings ORDER BY rowid DESC LIMIT 1").fetchone()
    rig.record("person.review_metadata", dict(row))
    rig.check("person.review.actor", row["reviewed_by"], "demo-operator")
    rig.check("person.no_vehicle_transfer", rig.r.store.registry.scope_stamp("demo-driver-2"), before)
    rig.check("person.vehicle_recipient", rig.r.business.resolve_recipient("obj-car-02")["user_id"], "demo-driver")
    await rig.step(2)
    change(2, "unmapped", "reviewed", None)
    rig.check("person.unlink.no_vehicle_transfer", rig.r.store.registry.scope_stamp("demo-driver-2"), before)


async def new_impact(rig):
    await rig.create()
    original_b = deepcopy(next(a for a in rig.r.world["actors"] if a["object_id"] == "obj-car-02"))
    rig.configure("will_move")
    result, _ = await rig.notify()
    iid = result["incident_id"]
    rig.record("initial_impact_scope", [dict(x) for x in rig.db.execute(
        "SELECT type,zone_id FROM incident_impacts WHERE incident_id=?", (iid,))])
    await rig.deliver()
    rig.r.synthetic_users.process()
    await rig.step(100)
    rig.check("old_impact.clear", rig.assessment("s1a")["clearance_sustained"], True)
    # Supplemental placement: the responded vehicle now blocks A's exit.
    # No source fixture or frozen observation/expected file is changed.
    candidate = deepcopy(rig.r.world)
    candidate["actors"] = [a for a in candidate["actors"] if a["object_id"] != "obj-car-02"]
    candidate["actors"].append(original_b | {"x": 15.1, "y": 21.7, "heading_deg": 0.0})
    candidate["move_requested"] = False
    candidate["state_version"] += 1
    rig.r.store.commit(candidate, Runtime.event(candidate, "run.updated"))
    rig.r.world = candidate
    await rig.step(60)
    new = rig.assessment("s1b")
    rig.record("new_impact.observation", new)
    rig.check("new_impact.support", new["support_status"], "supported")
    rig.check("new_impact.violation", new["violation_candidate"], True)
    rig.check("new_impact.old_still_clear", rig.assessment("s1a")["clearance_sustained"], True)
    rig.check("new_impact.old_no_violation", rig.assessment("s1a")["violation_candidate"], False)
    # An S1-a-only recheck must discover the new S1-b candidate before closing.
    await rig.agent("s1a")
    # Scenario-specific lookup must now use B01, not the first stored aisle.
    await rig.agent("s1b")
    added = await rig.incident([IMPACTS["s1b"]])
    linked = rig.r.business.scoped("incidents", added["incident_id"], "incident_id")["previous_incident_id"]
    rig.record("new_impact.link", {"old_incident_id": iid, "current_incident_id": added["incident_id"], "previous_incident_id": linked})
    rig.check("new_impact.same_incident", added["incident_id"], iid)
    rig.check("new_impact.one_incident", rig.count("incidents"), 1)
    actual_impacts = sorted((x["type"], x["zone_id"]) for x in rig.db.execute("SELECT type,zone_id FROM incident_impacts WHERE incident_id=?", (iid,)))
    rig.check("new_impact.accumulated", actual_impacts, sorted([IMPACTS["s1a"], IMPACTS["s1b"]]))
    repeated = await rig.incident([IMPACTS["s1b"]])
    rig.check("new_impact.dedup", repeated["incident_id"], added["incident_id"])
    rig.check("new_impact.not_resolved", rig.status(added["incident_id"]) == "resolved", False)
    await rig.agent("s1a")
    for name, actual, expected in (("same_cause.no_second_contact", rig.count("notifications", "purpose='move_request'"), 1),
            ("same_cause.old_not_resolved", rig.status(iid) == "resolved", False)):
        rig.check(name, actual, expected)
    return iid


async def restart(rig):
    await rig.create()
    result, nid = await rig.notify()
    rig.r.store.close()
    rig.r = None
    rig.open()
    rig.check("restart.recovery_required", rig.r.world["recovery_required"], True)
    rig.check("restart.followup_preserved", rig.count("followups"), 1)
    # Explicit resume before attempting dispatch; premature polling would
    # intentionally hold and consume the pending execution for human review.
    old_observation = rig.r.world["observation"]["observation_id"]
    await rig.step(60)
    rig.check("restart.explicit_resume", rig.r.world["recovery_required"], False)
    rig.check("restart.fresh_observation", rig.r.world["observation"]["observation_id"] != old_observation, True)
    rig.check("restart.recorded_resume", any(x["kind"] == "recovery_resume" for x in rig.r.world["recorded_inputs"]), True)
    await rig.deliver()
    rig.check("restart.send.once", len(rig.inbox.sent), 1)
    await rig.deliver()
    rig.check("restart.no_duplicate", len(rig.inbox.sent), 1)
    rig.check("restart.notification", rig.r.business.scoped("notifications", nid, "notification_id")["delivery_status"], "channel_accepted")
    rig.check("restart.attempt.once", rig.count("delivery_attempts"), 1)
    rig.check("restart.not_resolved", rig.status(result["incident_id"]), "active")


async def concurrent(rig):
    await rig.create()
    await rig.r.lock.acquire()
    tasks = [asyncio.create_task(rig.r.stream_batch(None, rig.run)) for _ in range(4)]
    await asyncio.sleep(0)  # Let consumers actually wait on the held writer lock.
    rig.check("concurrent.waiters", [t.done() for t in tasks], [False] * 4)
    candidate = deepcopy(rig.r.world)
    rig.r.advance_candidate(candidate)
    rig.r.store.commit(candidate, Runtime.event(candidate, "run.updated"))
    rig.r.world = candidate
    rig.r.lock.release()
    batches = await asyncio.gather(*tasks)
    rig.record("concurrent.batches", batches)
    expected_version = candidate["state_version"]
    rig.check("concurrent.snapshot_versions", [b[0][0]["state_version"] for b in batches], [expected_version] * 4)
    cursor = batches[0][1]
    rig.check("concurrent.cursors", [b[1] for b in batches], [cursor] * 4)
    await rig.step(2)
    next_batches = await asyncio.gather(*(rig.r.stream_batch(cursor, rig.run) for _ in range(4)))
    rig.record("concurrent.next_batches", next_batches)
    rig.check("concurrent.next_complete", [bool(b[0]) and b[0][-1]["state_version"] == rig.r.world["state_version"] for b in next_batches], [True] * 4)
    expected_ids = [f"evt-{seq}" for seq, event in rig.r.store.events()
                    if event["run_id"] == rig.run and seq > int(cursor.removeprefix("evt-"))]
    rig.check("concurrent.all_commits", [[event["event_id"] for event in b[0]] for b in next_batches], [expected_ids] * 4)


async def exercise(case, rig):
    if case.startswith("t04-s"):
        await movement(rig, case.removeprefix("t04-"))
    elif case == "t04-delay":
        await delayed(rig)
    elif case.startswith("silent-"):
        await silent(rig, case.removeprefix("silent-"))
    elif case.startswith("t07-"):
        await relationship(rig, case == "t07-unlink")
    elif case == "person-review":
        await person(rig)
    elif case == "t20-new-impact":
        await new_impact(rig)
    elif case == "v02-restart":
        await restart(rig)
    elif case == "v01-concurrent":
        await concurrent(rig)
    else:
        raise ValueError(f"Unknown case: {case}")


def run_case(case, directory):
    result = {"case": case, "status": "failed", "observations": [], "provider_calls": 0,
              "mode": "mock/local_web_inbox/synthetic", "original_acceptance_replaced": False}
    with tempfile.TemporaryDirectory(prefix=case + "-", dir=directory) as temporary:
        try:
            with Rig(temporary, result["observations"]) as rig:
                asyncio.run(exercise(case, rig))
            result["status"] = "passed"
        except Exception as error:
            result["failure"] = {"classification": getattr(error, "classification",
                "product" if isinstance(error, (FlowFailure, ApiError)) else "harness"),
                "type": type(error).__name__, "code": getattr(error, "code", None), "message": str(error),
                "traceback": traceback.format_exc()}
    result["temporary_database_removed"] = not Path(temporary).exists()
    return result


def output_path(value):
    path = Path(value).resolve()
    artifact_root = (ROOT / "Work_tree/artifacts/local-flow-completion").resolve()
    if path.suffix.lower() != ".json" or not path.is_relative_to(artifact_root):
        raise argparse.ArgumentTypeError("Output must be a new .json under Work_tree/artifacts/local-flow-completion")
    if path.exists():
        raise argparse.ArgumentTypeError("Output already exists; choose a new report name")
    return path


def provenance():
    def git(*args):
        result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        return result.stdout.strip() if result.returncode == 0 else None
    paths = ["scripts/check_local_flows.py", "tests/test_local_flow_completion.py",
        "code/backend/runtime.py", "code/backend/business.py", "code/backend/relationships.py",
        "code/backend/autonomous.py", "code/backend/synthetic_users.py", "code/agent/autonomous.py",
        "code/backend/business_schema.py", "code/backend/operating_analysis.py", "code/simulator/world.py",
        "code/simulator/environment.py", "data/samples/sim0-operation-settings.json",
        "data/samples/operating_knowledge/manifest-sim0.json"]
    return {"actual_head": git("rev-parse", "HEAD"), "branch": git("branch", "--show-current"),
        "dirty_status": git("status", "--porcelain"), "python": sys.version, "utf8_mode": sys.flags.utf8_mode,
        "sha256": {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in paths}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append", choices=CASES, help="Repeat for selected cases; omitted means all supplemental cases.")
    parser.add_argument("--output", type=output_path, required=True)
    parser.add_argument("--provider-calls", type=int, choices=[0], default=0)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    started_provenance = provenance()
    results = []
    for case in args.case or CASES:
        result = run_case(case, args.output.parent)
        results.append(result)
        print(f"{result['case']}: {result['status']}" + (f" ({result['failure']['message']})" if "failure" in result else ""), flush=True)
    report = {"schema_version": 1, "declared_base_commit": "d9480c6b00397e4f1346a0942e76a527ec62ed84",
        "provenance": {"start": started_provenance, "end": provenance()},
        "conditions": {"test_plan": "docs/구현범위_검증결과.md",
            "seed": 42, "candidate_ticks": 60, "post_response_milestone_ticks": 25,
            "post_response_max_ticks": 400, "tick_ms": 100, "observation_ms": 200,
            "wall_start": "2026-10-04T00:00:00Z", "mode": "mock/local_web_inbox/synthetic",
            "provider_calls_allowed": 0, "server_started": False, "temporary_case_db_cleanup": True,
            "scope": "supplemental product flows; frozen 194 inputs/expected/denominator unchanged"},
        "provider_calls": 0, "original_denominator": 194,
        "supplemental_passed": sum(r["status"] == "passed" for r in results), "supplemental_total": len(results), "cases": results}
    report["code_unchanged_during_run"] = report["provenance"]["start"]["sha256"] == report["provenance"]["end"]["sha256"]
    with args.output.open("x", encoding="utf-8") as output:
        output.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 0 if report["code_unchanged_during_run"] and all(
        r["status"] == "passed" and r["temporary_database_removed"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
