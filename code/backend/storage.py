import json
from pathlib import Path
import sqlite3
import sys

from backend.registry import Registry, migrate_registry, seed_run
from backend.knowledge import migrate_knowledge, transaction
from backend.business_schema import migrate_business
from backend.relationships import migrate_relationships
from backend.synthetic_users import migrate_synthetic_users
from backend.autonomous_schema import migrate_autonomous


class Store:
    """One process owns a DB; checkpoints, events and idempotency commit together."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_file = path.with_suffix(path.suffix + ".writer.lock").open("a+b")
        try:
            self.lock_file.seek(0)
            if not self.lock_file.read(1):
                self.lock_file.write(b"0")
                self.lock_file.flush()
            self.lock_file.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.lock_file.close()
            raise RuntimeError("This database already has a world writer") from exc
        try:
            self.db = sqlite3.connect(path)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4, 5, 6):
                raise RuntimeError("Unsupported database schema; database was not reset")
            if version == 0:
                if self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                    raise RuntimeError("Unversioned nonempty database; refusing to overwrite")
                self.db.executescript("""
                    BEGIN;
                    CREATE TABLE runs (run_id TEXT PRIMARY KEY, world_json TEXT NOT NULL);
                    CREATE TABLE current_run (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        run_id TEXT NOT NULL REFERENCES runs(run_id));
                    CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL REFERENCES runs(run_id), payload TEXT NOT NULL);
                    CREATE TABLE requests (requester TEXT NOT NULL, key TEXT NOT NULL,
                        argument_hash TEXT NOT NULL, response TEXT NOT NULL,
                        PRIMARY KEY(requester,key));
                    PRAGMA user_version=1;
                    COMMIT;
                """)
            if version in (0, 1):
                migrate_registry(self.db)
            if version in (0, 1, 2):
                migrate_knowledge(self.db)
            if version in (0, 1, 2, 3):
                migrate_business(self.db)
            if self.db.execute("PRAGMA user_version").fetchone()[0] == 4:
                migrate_relationships(self.db)
            if self.db.execute("PRAGMA user_version").fetchone()[0] >= 5:
                migrate_synthetic_users(self.db)
            if self.db.execute("PRAGMA user_version").fetchone()[0] == 5:
                migrate_autonomous(self.db)
            self.registry = Registry(self.db)
        except Exception:
            self.close()
            raise

    def close(self):
        if hasattr(self, "db"):
            self.db.close()
        self.lock_file.close()  # OS releases the writer lock, even after a crash.

    def load(self):
        row = self.db.execute("SELECT world_json FROM runs JOIN current_run USING(run_id)").fetchone()
        return json.loads(row[0]) if row else None

    def previous_request(self, requester, key):
        return self.db.execute("SELECT argument_hash,response FROM requests WHERE requester=? AND key=?",
                               (requester, key)).fetchone()

    def commit(self, world, event, request=None):
        with transaction(self.db):
            new_run = not self.db.execute("SELECT 1 FROM runs WHERE run_id=?", (world["run_id"],)).fetchone()
            self.db.execute("INSERT INTO runs VALUES (?,?) ON CONFLICT(run_id) DO UPDATE SET world_json=excluded.world_json",
                            (world["run_id"], json.dumps(world, allow_nan=False)))
            if new_run:
                seed_run(self.db, world["run_id"], world)
            self.db.execute("INSERT INTO current_run VALUES (1,?) ON CONFLICT(singleton) DO UPDATE SET run_id=excluded.run_id",
                            (world["run_id"],))
            if event is not None:
                self.append_event(event)
            if request:
                self.db.execute("INSERT INTO requests VALUES (?,?,?,?)", request)

    def append_event(self, event):
        cursor = self.db.execute("INSERT INTO events(run_id,payload) VALUES (?,?)", (event["run_id"], "{}"))
        event["event_id"] = f"evt-{cursor.lastrowid}"
        self.db.execute("UPDATE events SET payload=? WHERE seq=?", (json.dumps(event, allow_nan=False), cursor.lastrowid))
        has_outbox = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outbox_events'").fetchone()
        prune = "DELETE FROM events WHERE seq <= ?" + (" AND NOT EXISTS (SELECT 1 FROM outbox_events o WHERE o.stream_seq=events.seq)" if has_outbox else "")
        self.db.execute(prune, (cursor.lastrowid - 512,))
        return cursor.lastrowid

    def events(self):
        return [(row[0], json.loads(row[1])) for row in
                self.db.execute("SELECT seq,payload FROM events ORDER BY seq")]
