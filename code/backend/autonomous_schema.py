"""Preserve v5 business records while adding bounded autonomous job history."""


def migrate_autonomous(db):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version == 6:
        return
    if version != 5 or db.in_transaction:
        raise RuntimeError("Autonomous migration requires a fresh v5 database")
    foreign_keys = db.execute("PRAGMA foreign_keys").fetchone()[0]
    db.execute("PRAGMA foreign_keys=OFF")
    try:
        db.execute("BEGIN IMMEDIATE")
        schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='incident_impacts'").fetchone()[0]
        old_check = "CHECK(type IN ('aisle_obstruction'))"
        new_check = "CHECK(type IN ('aisle_obstruction','exit_blocked','bay_intrusion','approach_risk'))"
        if old_check not in schema:
            raise RuntimeError("Unknown incident impact schema")
        db.execute(schema.replace("CREATE TABLE incident_impacts", "CREATE TABLE incident_impacts_v6")
                   .replace(old_check, new_check))
        db.execute("INSERT INTO incident_impacts_v6 SELECT * FROM incident_impacts")
        db.execute("DROP TABLE incident_impacts")
        db.execute("ALTER TABLE incident_impacts_v6 RENAME TO incident_impacts")
        db.execute("""CREATE TABLE autonomous_jobs (
            job_id TEXT PRIMARY KEY, facility_id TEXT NOT NULL, run_id TEXT NOT NULL,
            requester_ref TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('mock','live')),
            scenario TEXT NOT NULL CHECK(scenario IN ('s1a','s1b','s1c','s2','s3')),
            trigger_key TEXT NOT NULL, context_stamp TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','completed','held','unknown','cancelled')),
            result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(facility_id,run_id) REFERENCES run_facilities(facility_id,run_id),
            UNIQUE(facility_id,run_id,requester_ref,trigger_key))""")
        db.execute("CREATE INDEX autonomous_jobs_scope ON autonomous_jobs(facility_id,run_id,status,created_at)")
        violations = db.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("Autonomous migration violated existing references")
        db.execute("PRAGMA user_version=6")
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.execute(f"PRAGMA foreign_keys={int(foreign_keys)}")
