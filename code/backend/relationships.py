"""Synthetic relationship administration on the existing single-writer SQLite DB."""
from datetime import datetime, timezone
import hashlib
import json
from math import isfinite
import secrets
import sqlite3
from uuid import uuid4

from backend.auth import ApiError
from backend.knowledge import transaction
from backend.registry import password_hash
from simulator.world import FACILITY


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _id(prefix):
    return prefix + "-" + uuid4().hex


def _alias(value):
    value = value.strip()
    if not value:
        raise ApiError(422, "INVALID_ALIAS", "표시 별칭을 입력하세요.")
    return value


def migrate_relationships(db):
    """One-way v4→v5 addition; the caller owns the DB writer and schema order."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version in (5, 6):
        return
    if version != 4:
        raise RuntimeError("Relationship migration requires schema v4")
    # executescript commits a pending transaction, so the script owns BEGIN/COMMIT.
    try:
        db.executescript("""
            BEGIN;
            CREATE TABLE relation_versions (
                scope TEXT NOT NULL, subject TEXT NOT NULL,
                resource_version INTEGER NOT NULL DEFAULT 0 CHECK(resource_version>=0),
                PRIMARY KEY(scope,subject));
            CREATE TABLE relationship_requests (
                actor TEXT NOT NULL, request_key TEXT NOT NULL,
                request_hash TEXT NOT NULL, response_json TEXT NOT NULL,
                PRIMARY KEY(actor,request_key));
            CREATE TABLE relationship_audit (
                audit_id TEXT PRIMARY KEY, facility_id TEXT NOT NULL REFERENCES facilities,
                actor TEXT NOT NULL, action TEXT NOT NULL, target_ref TEXT NOT NULL,
                reason TEXT NOT NULL, occurred_at TEXT NOT NULL);
            CREATE TABLE person_mappings (
                mapping_id TEXT PRIMARY KEY, facility_id TEXT NOT NULL,
                run_id TEXT NOT NULL, object_id TEXT NOT NULL,
                user_id TEXT REFERENCES users,
                mapping_status TEXT NOT NULL CHECK(mapping_status IN ('proposed','uncertain','verified','unmapped')),
                mapping_source TEXT NOT NULL CHECK(mapping_source IN ('demo_config','reviewed')),
                reviewed_by TEXT REFERENCES users, reviewed_at TEXT,
                reason TEXT NOT NULL,
                valid_from_sim_time_ms INTEGER NOT NULL CHECK(valid_from_sim_time_ms>=0),
                valid_to_sim_time_ms INTEGER,
                CHECK(valid_to_sim_time_ms IS NULL OR valid_to_sim_time_ms>valid_from_sim_time_ms),
                CHECK(mapping_status!='verified' OR (user_id IS NOT NULL AND mapping_source='reviewed' AND reviewed_by IS NOT NULL)),
                FOREIGN KEY(facility_id,run_id) REFERENCES run_facilities(facility_id,run_id));
            CREATE INDEX person_mapping_context ON person_mappings(facility_id,run_id,object_id);
            CREATE TRIGGER person_mapping_overlap_insert BEFORE INSERT ON person_mappings
            WHEN EXISTS (SELECT 1 FROM person_mappings old
                WHERE old.facility_id=NEW.facility_id AND old.run_id=NEW.run_id
                AND (old.object_id=NEW.object_id OR (old.user_id IS NOT NULL AND old.user_id=NEW.user_id))
                AND (old.valid_to_sim_time_ms IS NULL OR NEW.valid_from_sim_time_ms<old.valid_to_sim_time_ms)
                AND (NEW.valid_to_sim_time_ms IS NULL OR old.valid_from_sim_time_ms<NEW.valid_to_sim_time_ms))
            BEGIN SELECT RAISE(ABORT,'Overlapping person mapping'); END;
            CREATE TRIGGER person_mapping_overlap_update BEFORE UPDATE ON person_mappings
            WHEN EXISTS (SELECT 1 FROM person_mappings old
                WHERE old.mapping_id!=NEW.mapping_id AND old.facility_id=NEW.facility_id AND old.run_id=NEW.run_id
                AND (old.object_id=NEW.object_id OR (old.user_id IS NOT NULL AND old.user_id=NEW.user_id))
                AND (old.valid_to_sim_time_ms IS NULL OR NEW.valid_from_sim_time_ms<old.valid_to_sim_time_ms)
                AND (NEW.valid_to_sim_time_ms IS NULL OR old.valid_from_sim_time_ms<NEW.valid_to_sim_time_ms))
            BEGIN SELECT RAISE(ABORT,'Overlapping person mapping'); END;
            PRAGMA user_version=5;
            COMMIT;
        """)
    except Exception:
        db.rollback()
        raise


class Relationships:
    def __init__(self, db):
        self.db = db

    def version(self, scope, subject):
        row = self.db.execute("SELECT resource_version FROM relation_versions WHERE scope=? AND subject=?",
                              (scope, subject)).fetchone()
        return row[0] if row else 0

    def _advance(self, scope, subject, expected):
        current = self.version(scope, subject)
        if current != expected:
            raise ApiError(409, "RESOURCE_CHANGED", "관계가 변경되었습니다. 목록을 다시 조회하세요.")
        self.db.execute("INSERT INTO relation_versions(scope,subject,resource_version) VALUES (?,?,1) "
                        "ON CONFLICT(scope,subject) DO UPDATE SET resource_version=resource_version+1",
                        (scope, subject))
        return current + 1

    def _audit(self, actor, action, target, reason):
        self.db.execute("INSERT INTO relationship_audit VALUES (?,?,?,?,?,?,?)",
                        (_id("rel-audit"), FACILITY, actor, action, target, reason, _now()))

    def execute(self, actor, request_key, action, payload, perform):
        if not request_key or len(request_key) > 128:
            raise ApiError(400, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key가 필요합니다.")
        digest = hashlib.sha256(json.dumps({"action": action, "payload": payload},
                             sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        with transaction(self.db):
            prior = self.db.execute("SELECT request_hash,response_json FROM relationship_requests "
                                    "WHERE actor=? AND request_key=?", (actor, request_key)).fetchone()
            if prior:
                if prior[0] != digest:
                    raise ApiError(409, "IDEMPOTENCY_CONFLICT", "같은 요청 키의 내용이 다릅니다.")
                return json.loads(prior[1])
            response = perform()
            self.db.execute("INSERT INTO relationship_requests VALUES (?,?,?,?)",
                            (actor, request_key, digest, json.dumps(response, ensure_ascii=False)))
            return response

    def snapshot(self):
        users = [dict(row) for row in self.db.execute("""SELECT u.user_id,u.display_alias,u.disabled_at,
            COALESCE((SELECT resource_version FROM relation_versions WHERE scope='customer' AND subject=u.user_id),0) AS resource_version
            FROM users u JOIN memberships m USING(user_id) WHERE m.facility_id=? AND m.role='driver'
            ORDER BY u.user_id""", (FACILITY,))]
        vehicles = [dict(row) for row in self.db.execute("""SELECT v.*,
            COALESCE((SELECT resource_version FROM relation_versions WHERE scope='vehicle' AND subject=v.registered_vehicle_id),0) AS resource_version
            FROM vehicles v WHERE facility_id=? ORDER BY registered_vehicle_id""", (FACILITY,))]
        links = [dict(row) for row in self.db.execute("SELECT registered_vehicle_id,user_id,valid_from,valid_until "
                                                     "FROM vehicle_users ORDER BY registered_vehicle_id,valid_from")]
        objects = [dict(row) for row in self.db.execute("SELECT * FROM object_mappings WHERE facility_id=? "
                                                       "ORDER BY run_id,object_id,valid_from_sim_time_ms", (FACILITY,))]
        people = [dict(row) for row in self.db.execute("SELECT * FROM person_mappings WHERE facility_id=? "
                                                      "ORDER BY run_id,object_id,valid_from_sim_time_ms", (FACILITY,))]
        versions = [dict(row) for row in self.db.execute("SELECT * FROM relation_versions ORDER BY scope,subject")]
        return {"facility_id": FACILITY, "customers": users, "vehicles": vehicles,
                "vehicle_users": links, "vehicle_mappings": objects, "person_mappings": people,
                "versions": versions, "updated_at": _now()}

    def customer_create(self, actor, alias, reason):
        customer = _id("synthetic-user")
        salt = secrets.token_bytes(16)
        self.db.execute("INSERT INTO users VALUES (?,?,?,?,?,NULL)",
                        (customer, customer, _alias(alias), salt, password_hash("parking-demo-only", salt)))
        self.db.execute("INSERT INTO memberships VALUES (?,?,?,NULL)", (FACILITY, customer, "driver"))
        self._audit(actor, "customer_created", customer, reason)
        return {"user_id": customer, "display_alias": _alias(alias), "resource_version": 0, "active": True}

    def customer_change(self, actor, customer, body):
        row = self.db.execute("""SELECT u.user_id,u.display_alias,u.disabled_at FROM users u
            JOIN memberships m USING(user_id) WHERE u.user_id=? AND m.facility_id=? AND m.role='driver'""",
            (customer, FACILITY)).fetchone()
        if not row:
            raise ApiError(404, "NOT_FOUND", "가상 고객을 찾을 수 없습니다.")
        version = self._advance("customer", customer, body.expected_version)
        alias = _alias(body.display_alias) if body.display_alias is not None else row["display_alias"]
        disabled = row["disabled_at"]
        if body.active is False and disabled is None:
            disabled = _now()
            # Close current grants; previous interval remains for audit and historic checks.
            self.db.execute("UPDATE vehicle_users SET valid_until=? WHERE user_id=? AND valid_until IS NULL", (disabled, customer))
        if body.active is True and disabled is not None:
            raise ApiError(409, "REACTIVATION_UNSUPPORTED", "비활성 고객의 재활성화는 지원하지 않습니다.")
        self.db.execute("UPDATE users SET display_alias=?,disabled_at=? WHERE user_id=?", (alias, disabled, customer))
        self._audit(actor, "customer_changed", customer, body.reason)
        return {"user_id": customer, "display_alias": alias, "resource_version": version, "active": disabled is None}

    def vehicle_create(self, actor, alias, reason):
        vehicle = _id("synthetic-vehicle")
        self.db.execute("INSERT INTO vehicles VALUES (?,?,?,1)", (vehicle, FACILITY, _alias(alias)))
        self._audit(actor, "vehicle_created", vehicle, reason)
        return {"registered_vehicle_id": vehicle, "display_alias": _alias(alias), "resource_version": 0, "active": True}

    def vehicle_change(self, actor, vehicle, body):
        row = self.db.execute("SELECT display_alias,active FROM vehicles WHERE registered_vehicle_id=? AND facility_id=?",
                              (vehicle, FACILITY)).fetchone()
        if not row:
            raise ApiError(404, "NOT_FOUND", "등록 차량을 찾을 수 없습니다.")
        version = self._advance("vehicle", vehicle, body.expected_version)
        alias = _alias(body.display_alias) if body.display_alias is not None else row["display_alias"]
        active = bool(row["active"]) if body.active is None else body.active
        if active and not row["active"]:
            raise ApiError(409, "REACTIVATION_UNSUPPORTED", "비활성 차량의 재활성화는 지원하지 않습니다.")
        self.db.execute("UPDATE vehicles SET display_alias=?,active=? WHERE registered_vehicle_id=?",
                        (alias, int(active), vehicle))
        if not active:
            self.db.execute("UPDATE vehicle_users SET valid_until=? WHERE registered_vehicle_id=? AND valid_until IS NULL",
                            (_now(), vehicle))
        self._audit(actor, "vehicle_changed", vehicle, body.reason)
        return {"registered_vehicle_id": vehicle, "display_alias": alias,
                "resource_version": version, "active": active}

    def vehicle_user_change(self, actor, vehicle, body):
        row = self.db.execute("SELECT active FROM vehicles WHERE registered_vehicle_id=? AND facility_id=?",
                              (vehicle, FACILITY)).fetchone()
        if not row or not row["active"]:
            raise ApiError(409, "VEHICLE_UNAVAILABLE", "활성 등록 차량이 필요합니다.")
        if body.user_id is not None:
            target = self.db.execute("""SELECT 1 FROM users u JOIN memberships m USING(user_id)
                WHERE u.user_id=? AND u.disabled_at IS NULL AND m.facility_id=?
                AND m.role='driver' AND m.revoked_at IS NULL""", (body.user_id, FACILITY)).fetchone()
            if not target:
                raise ApiError(409, "CUSTOMER_UNAVAILABLE", "활성 가상 차주가 필요합니다.")
        version = self._advance("vehicle_user", vehicle, body.expected_version)
        current = self.db.execute("SELECT user_id FROM vehicle_users WHERE registered_vehicle_id=? AND valid_until IS NULL",
                                  (vehicle,)).fetchall()
        if len(current) > 1:
            raise ApiError(409, "AMBIGUOUS_RELATION", "기존 연결이 복수라 검토가 필요합니다.")
        old = current[0][0] if current else None
        if old == body.user_id:
            raise ApiError(409, "RELATION_UNCHANGED", "현재 연결과 같습니다.")
        now = _now()
        if old is not None:
            self.db.execute("UPDATE vehicle_users SET valid_until=? WHERE registered_vehicle_id=? AND valid_until IS NULL",
                            (now, vehicle))
        if body.user_id is not None:
            self.db.execute("INSERT INTO vehicle_users VALUES (?,?,?,NULL)", (vehicle, body.user_id, now))
        self._audit(actor, "vehicle_user_changed", vehicle, body.reason)
        return {"registered_vehicle_id": vehicle, "user_id": body.user_id,
                "resource_version": version, "valid_from": now if body.user_id else None}

    def _observed(self, world, run_id, object_id, kind, *, require_observed=True):
        if not world or world["run_id"] != run_id:
            raise ApiError(409, "RUN_CHANGED", "현재 실행 회차를 다시 확인하세요.")
        rows = world["observation"]["objects"]
        if require_observed and not any(o["object_id"] == object_id and o["object_type"] == kind for o in rows):
            raise ApiError(409, "OBJECT_UNOBSERVED", "현재 공개 관측에서 해당 객체를 확인할 수 없습니다.")
        if not require_observed:
            table = "object_mappings" if kind == "vehicle" else "person_mappings"
            if not self.db.execute(f"SELECT 1 FROM {table} WHERE facility_id=? AND run_id=? AND object_id=? AND valid_to_sim_time_ms IS NULL",
                    (FACILITY, run_id, object_id)).fetchone():
                raise ApiError(409, "MAPPING_NOT_FOUND", "해제할 현재 관계가 없습니다.")
        if not self.db.execute("SELECT 1 FROM run_facilities WHERE run_id=? AND facility_id=?",
                               (run_id, FACILITY)).fetchone():
            raise ApiError(404, "RUN_NOT_FOUND", "해당 시설의 실행 회차가 아닙니다.")
        return world["sim_time_ms"]

    def object_change(self, actor, object_id, body, world):
        sim = self._observed(world, body.run_id, object_id, "vehicle", require_observed=body.mapping_status != "unmapped")
        if body.mapping_status == "verified" and not self._visible(world, object_id):
            raise ApiError(409, "OBJECT_UNCERTAIN", "확인된 현재 관측이 필요합니다.")
        if body.registered_vehicle_id:
            row = self.db.execute("SELECT active FROM vehicles WHERE registered_vehicle_id=? AND facility_id=?",
                                  (body.registered_vehicle_id, FACILITY)).fetchone()
            if not row or not row["active"]:
                raise ApiError(409, "VEHICLE_UNAVAILABLE", "활성 등록 차량이 필요합니다.")
        subject = body.run_id + ":" + object_id
        version = self._advance("object_mapping", subject, body.expected_version)
        if body.registered_vehicle_id:
            conflict = self.db.execute("""SELECT 1 FROM object_mappings WHERE facility_id=? AND run_id=?
                AND registered_vehicle_id=? AND object_id!=? AND valid_to_sim_time_ms IS NULL""",
                (FACILITY, body.run_id, body.registered_vehicle_id, object_id)).fetchone()
            if conflict:
                raise ApiError(409, "MAPPING_CONFLICT", "등록 차량이 다른 관측 객체와 연결돼 있습니다.")
        active = self.db.execute("SELECT * FROM object_mappings WHERE facility_id=? AND run_id=? AND object_id=? "
                                 "AND valid_to_sim_time_ms IS NULL", (FACILITY, body.run_id, object_id)).fetchone()
        if active:
            if sim <= active["valid_from_sim_time_ms"]:
                raise ApiError(409, "SIM_TIME_NOT_ADVANCED", "관측 시간이 진행된 뒤 관계를 변경하세요.")
            self.db.execute("UPDATE object_mappings SET valid_to_sim_time_ms=? WHERE mapping_id=?",
                            (sim, active["mapping_id"]))
        mapping = _id("mapping")
        self.db.execute("INSERT INTO object_mappings VALUES (?,?,?,?,?,?,?,?,NULL)",
                        (mapping, FACILITY, body.run_id, object_id, body.registered_vehicle_id,
                         body.mapping_status, body.mapping_source, sim))
        self._audit(actor, "object_mapping_changed", mapping, body.reason)
        return {"mapping_id": mapping, "object_id": object_id, "registered_vehicle_id": body.registered_vehicle_id,
                "mapping_status": body.mapping_status, "resource_version": version, "valid_from_sim_time_ms": sim}

    def person_change(self, actor, object_id, body, world):
        sim = self._observed(world, body.run_id, object_id, "pedestrian", require_observed=body.status != "unmapped")
        if body.status == "verified" and not self._visible(world, object_id):
            raise ApiError(409, "OBJECT_UNCERTAIN", "확인된 현재 관측이 필요합니다.")
        if body.user_id:
            row = self.db.execute("""SELECT 1 FROM users u JOIN memberships m USING(user_id)
                WHERE u.user_id=? AND u.disabled_at IS NULL AND m.facility_id=?
                AND m.role='driver' AND m.revoked_at IS NULL""", (body.user_id, FACILITY)).fetchone()
            if not row:
                raise ApiError(409, "CUSTOMER_UNAVAILABLE", "활성 가상 고객이 필요합니다.")
        subject = body.run_id + ":" + object_id
        version = self._advance("person_mapping", subject, body.expected_version)
        if body.user_id:
            conflict = self.db.execute("""SELECT 1 FROM person_mappings WHERE facility_id=? AND run_id=?
                AND user_id=? AND object_id!=? AND valid_to_sim_time_ms IS NULL""",
                (FACILITY, body.run_id, body.user_id, object_id)).fetchone()
            if conflict:
                raise ApiError(409, "MAPPING_CONFLICT", "가상 고객이 다른 사람 객체와 연결돼 있습니다.")
        active = self.db.execute("SELECT * FROM person_mappings WHERE facility_id=? AND run_id=? AND object_id=? "
                                 "AND valid_to_sim_time_ms IS NULL", (FACILITY, body.run_id, object_id)).fetchone()
        if active:
            if sim <= active["valid_from_sim_time_ms"]:
                raise ApiError(409, "SIM_TIME_NOT_ADVANCED", "관측 시간이 진행된 뒤 관계를 변경하세요.")
            self.db.execute("UPDATE person_mappings SET valid_to_sim_time_ms=? WHERE mapping_id=?",
                            (sim, active["mapping_id"]))
        mapping = _id("person-map")
        reviewed_at = _now() if body.source == "reviewed" else None
        self.db.execute("INSERT INTO person_mappings VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)",
                        (mapping, FACILITY, body.run_id, object_id, body.user_id, body.status,
                         body.source, actor if reviewed_at else None, reviewed_at, body.reason, sim))
        self._audit(actor, "person_mapping_changed", mapping, body.reason)
        return {"mapping_id": mapping, "object_id": object_id, "user_id": body.user_id,
                "status": body.status, "source": body.source, "resource_version": version,
                "valid_from_sim_time_ms": sim}

    @staticmethod
    def _visible(world, object_id):
        observation = world["observation"]
        if (world.get("recovery_required") or observation.get("coverage") != "complete"
                or not 0 <= world["sim_time_ms"] - observation["sim_time_ms"] <= 1000):
            return False
        if world["run_status"] == "running":
            received = datetime.fromisoformat(observation["received_at"].replace("Z", "+00:00"))
            if not 0 <= (datetime.now(timezone.utc) - received).total_seconds() <= 1:
                return False
        return any(item["object_id"] == object_id and item["quality"]["visibility"] == "visible"
            and not item["quality"]["missing_fields"] and item.get("position") is not None
            and item.get("size") is not None and item.get("heading_deg") is not None
            and item["quality"].get("uncertainty_m") is not None
            and isfinite(item["quality"]["uncertainty_m"])
            # Synthetic manual-review bound; never an inferred identity rule.
            and 0 <= item["quality"]["uncertainty_m"] <= .2
            for item in observation["objects"])
