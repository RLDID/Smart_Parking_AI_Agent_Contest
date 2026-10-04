"""Synthetic identity/vehicle registry and server-only observation projection."""
from copy import deepcopy
import hashlib
import secrets

from backend.auth import ApiError
from simulator.world import FACILITY, utc_now


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)


def migrate_registry(db):
    # One transaction includes schema, seed and legacy-run mappings. Never reseed
    # an existing v2 database: revocations and disabled records must survive restart.
    db.executescript("""
        BEGIN;
        CREATE TABLE facilities (facility_id TEXT PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE users (user_id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
            display_alias TEXT NOT NULL, password_salt BLOB NOT NULL, password_hash BLOB NOT NULL,
            disabled_at TEXT);
        CREATE TABLE memberships (facility_id TEXT NOT NULL REFERENCES facilities,
            user_id TEXT NOT NULL REFERENCES users, role TEXT NOT NULL
            CHECK(role IN ('owner','driver','test_operator')), revoked_at TEXT,
            PRIMARY KEY(facility_id,user_id,role));
        CREATE TABLE vehicles (registered_vehicle_id TEXT PRIMARY KEY,
            facility_id TEXT NOT NULL REFERENCES facilities, display_alias TEXT NOT NULL,
            active INTEGER NOT NULL CHECK(active IN (0,1)), UNIQUE(facility_id,registered_vehicle_id));
        CREATE TABLE vehicle_users (registered_vehicle_id TEXT NOT NULL REFERENCES vehicles,
            user_id TEXT NOT NULL REFERENCES users, valid_from TEXT NOT NULL, valid_until TEXT,
            CHECK(valid_until IS NULL OR valid_until>valid_from),
            PRIMARY KEY(registered_vehicle_id,user_id,valid_from));
        CREATE TABLE run_facilities (run_id TEXT PRIMARY KEY REFERENCES runs,
            facility_id TEXT NOT NULL REFERENCES facilities, UNIQUE(facility_id,run_id));
        CREATE TABLE object_mappings (mapping_id TEXT PRIMARY KEY, facility_id TEXT NOT NULL,
            run_id TEXT NOT NULL, object_id TEXT NOT NULL, registered_vehicle_id TEXT,
            mapping_status TEXT NOT NULL CHECK(mapping_status IN ('verified','uncertain','unmapped')),
            mapping_source TEXT NOT NULL CHECK(mapping_source IN ('demo_config','reviewed')),
            valid_from_sim_time_ms INTEGER NOT NULL CHECK(valid_from_sim_time_ms>=0),
            valid_to_sim_time_ms INTEGER,
            CHECK(valid_to_sim_time_ms IS NULL OR valid_to_sim_time_ms>valid_from_sim_time_ms),
            CHECK(mapping_status!='verified' OR registered_vehicle_id IS NOT NULL),
            FOREIGN KEY(facility_id,run_id) REFERENCES run_facilities(facility_id,run_id),
            FOREIGN KEY(facility_id,registered_vehicle_id) REFERENCES vehicles(facility_id,registered_vehicle_id));
    """)
    try:
        # Reject overlapping identities in either direction, including UPDATE.
        for operation in ("INSERT", "UPDATE"):
            db.execute(f"""CREATE TRIGGER mappings_no_overlap_{operation.lower()}
                BEFORE {operation} ON object_mappings WHEN EXISTS (
                    SELECT 1 FROM object_mappings m WHERE m.mapping_id != NEW.mapping_id
                    AND m.facility_id=NEW.facility_id AND m.run_id=NEW.run_id
                    AND (m.object_id=NEW.object_id OR m.registered_vehicle_id=NEW.registered_vehicle_id)
                    AND (m.valid_to_sim_time_ms IS NULL OR NEW.valid_from_sim_time_ms<m.valid_to_sim_time_ms)
                    AND (NEW.valid_to_sim_time_ms IS NULL OR m.valid_from_sim_time_ms<NEW.valid_to_sim_time_ms))
                BEGIN SELECT RAISE(ABORT,'Overlapping object mapping'); END""")
        db.execute("INSERT INTO facilities VALUES (?,?)", (FACILITY, "가상 시험 주차장"))
        for username, role in [("demo-owner", "owner"), ("demo-operator", "test_operator"),
                               ("demo-driver", "driver"), ("demo-driver-2", "driver")]:
            salt = secrets.token_bytes(16)
            db.execute("INSERT INTO users VALUES (?,?,?,?,?,NULL)",
                       (username, username, username, salt, password_hash("parking-demo-only", salt)))
            db.execute("INSERT INTO memberships VALUES (?,?,?,NULL)", (FACILITY, username, role))
        for number, username in [(1, "demo-driver-2"), (2, "demo-driver")]:
            vehicle = f"veh-demo-{number:02}"
            db.execute("INSERT INTO vehicles VALUES (?,?,?,1)",
                       (vehicle, FACILITY, f"가상 차량 {'A' if number == 1 else 'B'}"))
            db.execute("INSERT INTO vehicle_users VALUES (?,?,?,NULL)",
                       (vehicle, username, "1970-01-01T00:00:00Z"))
        for row in db.execute("SELECT run_id FROM runs").fetchall():
            seed_run(db, row[0])
        db.execute("PRAGMA user_version=2")
        db.commit()
    except Exception:
        db.rollback()
        raise


def seed_run(db, run_id, world=None):
    """Explicit synthetic registration; never infer an identity from geometry."""
    db.execute("INSERT INTO run_facilities VALUES (?,?)", (run_id, FACILITY))
    fixture = (world or {}).get("fixture_ref", "s1a-foundation-v1")
    pairs = [(1, "obj-car-01"), (2, "obj-car-02")]
    if fixture.startswith("s1c-"):
        pairs = [(2, "obj-car-02")]
    elif fixture.startswith("s2-"):
        pairs = [(2, "obj-car-s2-v")]
    elif fixture.startswith("s3-"):
        pairs = [(1, "obj-car-s3-u"), (2, "obj-car-s3-w")]
    for number, object_id in pairs:
        db.execute("INSERT INTO object_mappings VALUES (?,?,?,?,?,'verified','demo_config',0,NULL)",
                   (f"mapping-{run_id}-{number}", FACILITY, run_id,
                    object_id, f"veh-demo-{number:02}"))


class Registry:
    def __init__(self, db):
        self.db = db

    def role(self, username):
        row = self.db.execute("""SELECT m.role FROM memberships m JOIN users u USING(user_id)
            WHERE u.username=? AND u.disabled_at IS NULL AND m.facility_id=? AND m.revoked_at IS NULL
            ORDER BY CASE m.role WHEN 'test_operator' THEN 0 WHEN 'owner' THEN 1 ELSE 2 END LIMIT 1""",
                              (username, FACILITY)).fetchone()
        return row[0] if row else None

    def vehicles(self, username, now=None):
        return [dict(row) for row in self.db.execute("""SELECT DISTINCT v.registered_vehicle_id,
            v.facility_id,v.display_alias FROM vehicles v JOIN vehicle_users vu USING(registered_vehicle_id)
            JOIN users u USING(user_id) WHERE u.username=? AND v.facility_id=? AND v.active=1
            AND julianday(vu.valid_from)<=julianday(?)
            AND (vu.valid_until IS NULL OR julianday(vu.valid_until)>julianday(?))
            ORDER BY v.registered_vehicle_id""", (username, FACILITY, now or utc_now(), now or utc_now()))]

    def scope_stamp(self, username):
        # Changes while paused must also invalidate an open SSE connection.
        # Include ended/future links and mappings: conservative invalidation is
        # preferable to retaining a formerly authorised position in the browser.
        vehicles = self.vehicles(username)
        links = [tuple(row) for row in self.db.execute("""SELECT vu.* FROM vehicle_users vu
            JOIN users u USING(user_id) WHERE u.username=? ORDER BY registered_vehicle_id,valid_from""", (username,))]
        mappings = [tuple(row) for row in self.db.execute("""SELECT m.* FROM object_mappings m
            JOIN vehicle_users vu USING(registered_vehicle_id) JOIN users u USING(user_id)
            WHERE u.username=? ORDER BY mapping_id,valid_from""", (username,))]
        return repr((self.role(username), vehicles, links, mappings))

    def allowed_objects(self, username, run_id, frame_sim, current_sim, observed_at):
        # Both current grant AND historical grant must hold. A new owner cannot
        # replay observations from before their vehicle assignment.
        now = utc_now()
        return {row[0] for row in self.db.execute("""SELECT DISTINCT m.object_id
            FROM object_mappings m JOIN vehicles v USING(registered_vehicle_id)
            JOIN vehicle_users vu USING(registered_vehicle_id) JOIN users u USING(user_id)
            WHERE u.username=? AND m.facility_id=? AND m.run_id=? AND m.mapping_status='verified' AND v.active=1
            AND m.valid_from_sim_time_ms<=? AND m.valid_from_sim_time_ms<=?
            AND (m.valid_to_sim_time_ms IS NULL OR (m.valid_to_sim_time_ms>? AND m.valid_to_sim_time_ms>?))
            AND julianday(vu.valid_from)<=julianday(?) AND julianday(vu.valid_from)<=julianday(?)
            AND (vu.valid_until IS NULL OR (julianday(vu.valid_until)>julianday(?) AND julianday(vu.valid_until)>julianday(?)))""",
            (username, FACILITY, run_id, frame_sim, current_sim, frame_sim, current_sim,
             now, observed_at, now, observed_at))}

    def project(self, username, state, current_sim):
        role = self.role(username)
        if not role:
            raise ApiError(403, "FORBIDDEN", "현재 시설 조회 권한이 없습니다.")
        if role in ("owner", "test_operator"):
            return state
        result = deepcopy(state)
        snapshot = result["snapshot"]
        allowed = self.allowed_objects(username, snapshot["run_id"], snapshot["sim_time_ms"],
                                       current_sim, snapshot["observed_at"])
        snapshot["objects"] = [o for o in snapshot["objects"] if o["object_id"] in allowed]
        snapshot["devices"] = []
        snapshot["object_events"] = [e for e in snapshot["object_events"]
            if e["object_id"] in self.allowed_objects(username, snapshot["run_id"], e["sim_time_ms"],
                                                      current_sim, e["observed_at"])]
        # Full-facility coverage must not be confused with a filtered view.
        result["view_scope"] = "own_vehicles"
        result["registered_vehicle_ids"] = [v["registered_vehicle_id"] for v in self.vehicles(username)]
        return result
