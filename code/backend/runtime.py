import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

from backend.auth import ApiError
from backend.storage import Store
from backend.knowledge import DEFAULT_MANIFEST, Knowledge
from backend.knowledge import transaction
from backend.business import Business
from backend.agent_queries import QueryService
from backend.operating_analysis import OperatingAnalysis
from backend.device_operations import DeviceOperations
from backend.safety import SafetyController
from backend.synthetic_users import SyntheticUsers
from agent.manual import manual_s1
from agent.tools import ReadTask, call_read_tool, validate_session
from contracts.knowledge import KnowledgeEvidence
from contracts.models import SCHEMA_VERSION
from simulator.world import (CONFIG, FACILITY, MAP, advance, digest, initial_world,
                             prepare_observation_state, public_state, set_observation_mode, utc_now)


class Runtime:
    def __init__(self, database: Path):
        self.store = Store(database)
        self.world = self.store.load()
        self.lock = asyncio.Lock()
        self.failure = False
        self.knowledge = Knowledge(self.store, database.parent / "knowledge/index")
        latest_policy = self.store.db.execute("SELECT max(policy_version) FROM policies WHERE facility_id=?", (FACILITY,)).fetchone()[0] or 0
        if latest_policy < 2:
            try:
                self.knowledge.activate(DEFAULT_MANIFEST)
            except (OSError, ValueError, sqlite3.Error):
                # Observation/control remain available when knowledge is missing.
                # Existing document access is never restored by this upgrade.
                pass
        if self.world:
            from simulator.environment import prepare_environment_restart, validate_environment_checkpoint
            if not validate_environment_checkpoint(self.world):
                self.store.close()
                raise RuntimeError("Checkpoint configuration mismatch; explicit migration required")
            self.world["run_status"] = "paused"
            self.world["recovery_required"] = True
            self.world["state_version"] += 1
            try:
                from simulator.replay import record_input
                record_input(self.world, "restart_boundary", {})
                prepare_observation_state(self.world)
                prepare_environment_restart(self.world)
                safety = self.world.get("safety_state", {})
                for claim in safety.get("claims", {}).values():
                    claim["clear_since_ms"] = None
                safety["last_observation_id"] = None
                safety.pop("last_sim_time_ms", None)
            except Exception:
                self.store.close()
                raise
            self.store.commit(self.world, self.event(self.world, "run.updated"))
        try:
            self.business = Business(self)
            self.queries = QueryService(self)
            self.operating_analysis = OperatingAnalysis(self)
            self.devices = DeviceOperations(self)
            self.execute_device_action = self.devices.execute
            self.safety = SafetyController(self)
            self.synthetic_users = SyntheticUsers(self)
            from backend.autonomous import AutonomousService
            from agent.operating_models import LiveOperationsAdapter
            self.autonomous = AutonomousService(self, LiveOperationsAdapter)
        except Exception:
            self.store.close()
            raise

    @staticmethod
    def event(world, kind="state.snapshot"):
        return {"schema_version": SCHEMA_VERSION, "facility_id": FACILITY,
                "run_id": world["run_id"], "state_version": world["state_version"],
                "occurred_at": utc_now(), "type": kind, "payload": public_state(world)}

    def ensure_run(self, run_id):
        if not self.world or self.world["run_id"] != run_id:
            raise ApiError(404, "RUN_NOT_FOUND", "현재 실행 회차를 확인하세요.")

    def ensure_autonomous_policy(self):
        policy = self.knowledge.current_policy(FACILITY)
        if policy.policy_version == 2:
            manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
            from contracts.knowledge import OperatingPolicy, ManifestDocument
            if policy.model_dump() != OperatingPolicy.model_validate(manifest["policy"]).model_dump():
                raise ApiError(409, "KNOWLEDGE_CHANGED", "변경된 운영 정책은 자동 이행하지 않습니다.")
            for document in manifest["documents"]:
                document = ManifestDocument.model_validate(document).model_dump()
                row = self.store.db.execute("SELECT approval_status,allowed_roles_json,retired_at,reviewed_conflict,content_digest,effective_at FROM knowledge_documents WHERE facility_id=? AND document_id=? AND document_version=?",
                    (FACILITY, document["document_id"], document["document_version"])).fetchone()
                if (not row or row["approval_status"] != "approved" or row["retired_at"] is not None
                        or row["reviewed_conflict"] or row["content_digest"] != document["content_digest"]
                        or row["effective_at"] != document["effective_at"]
                        or set(json.loads(row["allowed_roles_json"])) != set(document["allowed_roles"])):
                    raise ApiError(409, "KNOWLEDGE_CHANGED", "철회·변경된 운영 문서를 새 정책으로 자동 복원하지 않습니다.")
            self.knowledge.activate(DEFAULT_MANIFEST.with_name("manifest-sim0.json"))

    def operating_candidates(self, scenario):
        if not self.world:
            return []
        impact, zone = {"s1b": ("exit_blocked", "B01"), "s1c": ("bay_intrusion", "B01"),
                        "s2": ("approach_risk", "announcement-a")}.get(scenario, (None, None))
        if impact is None:
            return []
        objects = self.world["observation"]["objects"]
        # B01 is the fixed synthetic candidate's measurement scope, not inferred
        # ownership or an assignment obtained from a vehicle's position.
        return [{"object_id": obj["object_id"], "zone_id": zone,
                 "assessment": self.operating_analysis(impact, obj["object_id"], zone)}
                for obj in objects if obj["object_type"] == "vehicle"][:8]

    @staticmethod
    def public_devices(world):
        from simulator.environment import public_devices
        state = world.get("safety_state", {})
        return public_devices(world) | {"execution_mode": "recorded_replay" if world.get("replay_state") is not None else "synthetic_live",
            "independent_safety": {"mode": "synthetic_demo",
            "analysis_status": state.get("analysis_status", "awaiting_observation"),
            "tracked_claim_count": len(state.get("claims", {})),
            "feedback_reason": state.get("feedback_reason")}}

    def read_task(self, session, run_id):
        validate_session(self, session)
        self.ensure_run(run_id)
        return ReadTask(session.username, session.role, run_id)

    async def read_tool(self, session, name, arguments, task):
        async with self.lock:
            return call_read_tool(self, session, name, arguments, task)

    async def business_tool(self, session, name, arguments, key, task):
        async with self.lock:
            validate_session(self, session)
            self.ensure_business_writable()
            task.consume(session, name)
            self.ensure_run(task.run_id)
            if arguments.get("run_id") != task.run_id:
                raise ApiError(409, "CONTEXT_CHANGED", "현재 작업 회차의 도구 인자가 필요합니다.")
            return self.business.execute(session, name, arguments, key)

    async def manual_s1a(self, session, body, key, authenticate=None):
        async with self.lock:
            if authenticate is not None:
                authenticate()
            validate_session(self, session)
            self.ensure_business_writable()
            return manual_s1(self, session, body, key)

    def ensure_business_writable(self):
        if self.world and self.world.get("replay_state") is not None:
            raise ApiError(409, "REPLAY_INPUT_REJECTED", "기록 재생에서는 새 업무나 연락을 실행하지 않습니다.")

    async def environment_control(self, session, run_id, body, key, operation, authenticate=None):
        from simulator.environment import configure_s2_reaction, set_synthetic_fault
        async with self.lock:
            if authenticate is not None:
                authenticate()
            validate_session(self, session)
            if session.role != "test_operator":
                raise ApiError(403, "FORBIDDEN", "시험 운영자 권한이 필요합니다.")
            self.ensure_run(run_id)
            fingerprint, old = self.business.key(session.username, key, operation, {"run_id": run_id, **body.model_dump()})
            if old:
                return old
            if self.failure or self.world["recovery_required"] or body.expected_state_version != self.world["state_version"]:
                raise ApiError(409, "STATE_CHANGED", "실행 복구와 현재 버전을 확인하세요.")
            if self.world.get("replay_state") is not None:
                raise ApiError(409, "REPLAY_INPUT_REJECTED", "기록 재생 중에는 새 시험 입력을 받지 않습니다.")
            candidate = deepcopy(self.world)
            try:
                if operation == "device_fault":
                    set_synthetic_fault(candidate, body.channel, body.failed)
                else:
                    configure_s2_reaction(candidate, body.mode, delay_ms=body.delay_ms)
            except ValueError:
                raise ApiError(422, "UNSUPPORTED_CONTROL", "이 회차에서 지원하는 시험 제어를 확인하세요.") from None
            candidate["state_version"] += 1
            result = {"run_id": run_id, "state_version": candidate["state_version"], "mode": "synthetic_demo", "control": body.model_dump()}
            with transaction(self.store.db):
                self.store.commit(candidate, self.event(candidate, "run.updated"))
                self.business.save_key(session.username, key, fingerprint, result)
                self.business.audit(session.username, operation, run_id, run_id)
            self.world = candidate
            return result

    async def recipient_tool(self, session, object_id, task):
        async with self.lock:
            validate_session(self, session)
            task.consume(session, "resolve_vehicle_recipient")
            self.ensure_run(task.run_id)
            if session.role not in ("owner", "test_operator"):
                raise ApiError(403, "FORBIDDEN", "차주 연결 조회 권한이 없습니다.")
            value = self.business.resolve_recipient(object_id)
            return {"recipient_ref": value["recipient_ref"], "mapping_status": "verified"}

    async def notification_status_tool(self, session, notification_id, task):
        async with self.lock:
            validate_session(self, session)
            task.consume(session, "get_notification_status")
            self.ensure_run(task.run_id)
            row = self.business.scoped("notifications", notification_id, "notification_id")
            if row["run_id"] != task.run_id or (session.role == "driver" and not self.business.notification_allowed(session.username, row)):
                raise ApiError(404, "NOT_FOUND", "현재 작업에서 알림을 찾을 수 없습니다.")
            return self.business.notification_view(row)

    async def business_loop(self):
        while True:
            await asyncio.sleep(0.1)
            if self.failure:
                continue
            try:
                await self.business.deliver_one()
                async with self.lock:
                    self.synthetic_users.process()
                    self.devices.reconcile()
                    self.business.process_followups()
                    self.business.publish_outbox()
            except Exception:
                self.failure = True
                if self.world:
                    self.world["run_status"] = "paused"
                    self.world["recovery_required"] = True
                continue
            try:
                await self.autonomous.tick()
            except Exception:
                self.autonomous.enabled = None
                self.autonomous.last_error = "WATCHER_UNAVAILABLE"

    async def validate_knowledge(self, session, run_id, evidence: KnowledgeEvidence | None, *, tool_name, purpose):
        # Read-only evidence check. Business also validates inside acceptance
        # and dispatch transactions; this check alone does not authorize I/O.
        async with self.lock:
            validate_session(self, session)
            self.ensure_run(run_id)
            if session.role not in ("owner", "test_operator"):
                raise ApiError(403, "FORBIDDEN", "실행 근거 검증 권한이 없습니다.")
            return self.knowledge.validate_evidence(session.username, FACILITY, run_id, evidence,
                                                    tool_name=tool_name, purpose=purpose)

    def knowledge_readiness(self):
        try:
            policy = self.knowledge.current_policy(FACILITY)
            self.knowledge._index(FACILITY, policy.knowledge_release_id)
            return "ready"
        except (OSError, ValueError, KeyError, sqlite3.Error, TimeoutError):
            return "unavailable"

    def business_readiness(self):
        try:
            return "ready" if not self.failure and self.knowledge.current_policy(FACILITY).execution_rules else "unavailable"
        except (ValueError, OSError, sqlite3.Error):
            return "unavailable"

    async def mutate(self, session, key, operation, arguments, run_id=None, authenticate=None):
        if not key or not 1 <= len(key) <= 128:
            raise ApiError(400, "IDEMPOTENCY_REQUIRED", "1~128자 Idempotency-Key가 필요합니다.")
        arguments = deepcopy(arguments)
        if arguments.get("action_params"):
            for name in ("request_vehicle_departure", "request_portal_attempt"):
                if arguments["action_params"].get(name) is None:
                    arguments["action_params"].pop(name, None)
        argument_hash = digest({"operation": operation, "run_id": run_id, "args": arguments})
        async with self.lock:
            if authenticate is not None:
                authenticate()
            validate_session(self, session)
            previous = self.store.previous_request(session.username, key)
            if previous:
                if previous[0] != argument_hash:
                    raise ApiError(409, "IDEMPOTENCY_CONFLICT", "같은 키에 다른 요청을 보낼 수 없습니다.")
                saved = json.loads(previous[1])
                if self.world and saved["run_id"] != self.world["run_id"]:
                    raise ApiError(409, "REQUEST_RUN_CHANGED", "이전 요청의 실행 회차가 이미 교체됐습니다.")
                return saved
            if self.failure:
                raise ApiError(503, "RUNTIME_UNAVAILABLE", "저장 실패로 실행이 중지됐습니다. 재시작 후 복구하세요.")
            if operation == "create":
                if self.world and self.world["run_status"] == "running":
                    raise ApiError(409, "CONFLICT", "기존 실행을 먼저 일시정지하세요.")
                candidate = initial_world(arguments["seed"], arguments.get("fixture_ref", "s1a-foundation-v1"))
            else:
                self.ensure_run(run_id)
                candidate = deepcopy(self.world)
                action = arguments["action"]
                params = arguments.get("action_params") or {}
                if action in ("reset", "replay"):
                    if candidate["run_status"] == "running":
                        raise ApiError(409, "CONFLICT", "초기화 전에 실행을 일시정지하세요.")
                    if params:
                        raise ApiError(422, "INVALID_ACTION_PARAMS", "초기화에는 시험 입력을 함께 보내지 않습니다.")
                    source = candidate
                    candidate = initial_world(source["seed"], source.get("fixture_ref", "s1a-foundation-v1"))
                    if action == "replay":
                        from simulator.replay import prepare_replay
                        try:
                            prepare_replay(source, candidate)
                        except ValueError:
                            raise ApiError(409, "REPLAY_UNAVAILABLE", "기록된 입력과 지원 버전을 확인하세요.") from None
                if params and action != "step":
                    raise ApiError(422, "INVALID_ACTION_PARAMS", "시험 입력은 paused step에서만 허용됩니다.")
                if action == "step" and candidate["run_status"] != "paused":
                    raise ApiError(409, "CONFLICT", "한 단계 진행은 일시정지 상태에서만 가능합니다.")
                if params and candidate.get("replay_state") is not None:
                    raise ApiError(409, "REPLAY_INPUT_REJECTED", "기록 재생 중에는 새 시험 입력을 받지 않습니다.")
                # Checkpoint hashes were checked on startup; an explicit operator action
                # acknowledges recovery; dispatcher still needs a new observation.
                if action in ("start", "step"):
                    if candidate["recovery_required"]:
                        from simulator.replay import record_input
                        record_input(candidate, "recovery_resume", {})
                    candidate["recovery_required"] = False
                if params.get("request_vehicle_move"):
                    from simulator.environment import queue_vehicle_response
                    try:
                        queue_vehicle_response(candidate, params["request_vehicle_move"], "will_move",
                                               action_key="operator-move:" + key)
                    except ValueError:
                        raise ApiError(422, "UNSUPPORTED_MOVEMENT", "현재 회차의 지원 이동을 확인하세요.") from None
                if params.get("request_vehicle_departure"):
                    from simulator.environment import queue_vehicle_departure
                    try:
                        queue_vehicle_departure(candidate, params["request_vehicle_departure"],
                                                action_key="operator-departure:" + key)
                    except ValueError:
                        raise ApiError(422, "UNSUPPORTED_MOVEMENT", "현재 회차의 지원 출차를 확인하세요.") from None
                if params.get("request_portal_attempt"):
                    from simulator.environment import queue_portal_attempt
                    try:
                        queue_portal_attempt(candidate, params["request_portal_attempt"],
                                             action_key="operator-portal:" + key)
                    except ValueError:
                        raise ApiError(422, "UNSUPPORTED_MOVEMENT", "현재 회차의 지원 입출차를 확인하세요.") from None
                if params.get("observation_mode"):
                    set_observation_mode(candidate, params["observation_mode"])
                if action != "step":
                    candidate["state_version"] += 1
                    candidate["run_status"] = "running" if action == "start" else "paused"
            with transaction(self.store.db):
                if operation != "create" and arguments["action"] == "step":
                    self.synthetic_users.validate_pending(candidate)
                    self.advance_candidate(candidate)
                    self.safety.step(candidate)
                    self.synthetic_users.validate_pending(candidate)
                response = {"run_id": candidate["run_id"], **public_state(candidate)}
                event = self.event(candidate, "state.snapshot" if operation == "create" else "run.updated")
                self.store.commit(candidate, event,
                                  (session.username, key, argument_hash, json.dumps(response)))
                if operation == "create" or arguments.get("action") in ("reset", "replay"):
                    self.business.cancel_old_run(candidate["run_id"])
            if operation == "create" or arguments.get("action") in ("reset", "replay"):
                self.autonomous.enabled = None
            self.world = candidate
            return response

    async def tick(self):
        async with self.lock:
            if self.failure or not self.world or self.world["run_status"] != "running":
                return
            candidate = deepcopy(self.world)
            previous_observation = candidate["observation"]["observation_id"]
            with transaction(self.store.db):
                self.synthetic_users.validate_pending(candidate)
                self.advance_candidate(candidate)
                self.safety.step(candidate)
                self.synthetic_users.validate_pending(candidate)
                event = None
                if candidate["sim_time_ms"] % CONFIG["observation_ms"] == 0:
                    kind = "state.snapshot" if previous_observation != candidate["observation"]["observation_id"] else "run.updated"
                    event = self.event(candidate, kind)
                self.store.commit(candidate, event)
            self.world = candidate

    def advance_candidate(self, candidate):
        from simulator.replay import apply_replay_inputs
        apply_replay_inputs(candidate)
        replay = candidate.get("replay_state")
        if replay is None or candidate["sim_time_ms"] < replay["end_sim_time_ms"]:
            advance(candidate)
        if replay is not None and candidate["sim_time_ms"] >= replay["end_sim_time_ms"]:
            apply_replay_inputs(candidate)
            candidate["run_status"] = "paused"

    async def loop(self):
        while True:
            await asyncio.sleep(CONFIG["tick_ms"] / 1000)
            try:
                await self.tick()
            except Exception:
                # No raw DB/config data enters logs or clients; no success publication.
                self.failure = True
                if self.world:
                    self.world["run_status"] = "paused"
                    self.world["recovery_required"] = True

    async def stream_batch(self, cursor, requested_run):
        """Snapshot and cursor capture share the writer lock (no subscription gap)."""
        async with self.lock:
            if not self.world:
                return [], cursor
            records = [(seq, evt) for seq, evt in self.store.events() if evt["run_id"] == self.world["run_id"]]
            latest = records[-1][0] if records else 0
            same_run = requested_run == self.world["run_id"]
            parsed = None
            if cursor:
                try:
                    parsed = int(cursor.removeprefix("evt-"))
                except ValueError:
                    pass
            matching = next((entry for seq, entry in records if seq == parsed), None)
            valid = bool(same_run and matching and matching["run_id"] == requested_run)
            if cursor is None and same_run:
                snapshot = self.event(self.world)
                snapshot["event_id"] = f"evt-{latest}"
                return [snapshot], snapshot["event_id"]
            if not valid:
                reset = self.event(self.world, "reset_required")
                reset["event_id"] = f"evt-{latest}"
                reset["payload"] = {"reason": "cursor_or_run_changed", "run_id": self.world["run_id"]}
                snapshot = self.event(self.world)
                snapshot["event_id"] = f"evt-{latest}"
                return [reset, snapshot], snapshot["event_id"]
            newer = [event for seq, event in records if seq > parsed]
            return newer, f"evt-{latest}"
