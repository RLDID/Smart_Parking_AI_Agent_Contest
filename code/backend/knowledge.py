"""Local keyword retrieval. Manuals are data, never instructions or authority."""
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import threading
import unicodedata
from uuid import uuid4

from backend.auth import ApiError
from contracts.knowledge import (KnowledgeEvidence, KnowledgeManifest, KnowledgeQuery,
                                 KnowledgeResult, Manual, OperatingPolicy)

ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = ROOT / "data/samples/operating_knowledge"
DEFAULT_MANIFEST = SOURCE_ROOT / "manifest.json"
LIMITS = {"groups": 4, "chars": 6000, "timeout_s": 2, "calls_per_task": 2}
SYNONYMS = {
    "통로차단": ("통로", "길막", "통행", "통로차단"),
    "출차방해": ("이중주차", "출차 방해", "출차방해"),
    "이동요청": ("이동", "차량 이동", "이동요청"),
    "후속확인": ("무응답", "미응답", "답이 없", "응답", "후속"),
    "영업종료": ("영업 종료", "영업종료", "입차", "종료"),
    "안내방송": ("방송", "안내방송"),
}
STOP_WORDS = {"가상", "차량", "주차장", "규정", "어떻게", "알려줘", "방법", "지금", "해주세요"}


def sha(value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    return "sha256:" + hashlib.sha256(value).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def wall_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def terms(text):
    normalized = unicodedata.normalize("NFKC", text).lower()
    result = set(re.findall(r"[가-힣a-z0-9]+", normalized)) - STOP_WORDS
    for canonical, synonyms in SYNONYMS.items():
        if any(word in normalized for word in synonyms):
            result.add(canonical)
    return sorted(result)


@contextmanager
def transaction(db):
    # A caller's dispatch transaction must remain open across validation/apply.
    if db.in_transaction:
        yield
    else:
        with db:
            db.execute("BEGIN")
            yield


@contextmanager
def bounded_database(db, expires):
    prior_busy = db.execute("PRAGMA busy_timeout").fetchone()[0]
    db.execute("PRAGMA busy_timeout=100")
    db.set_progress_handler(lambda: int(time.monotonic() >= expires), 1000)
    try:
        yield
    finally:
        db.set_progress_handler(None, 0)
        db.execute(f"PRAGMA busy_timeout={int(prior_busy)}")


def migrate_knowledge(db):
    db.executescript("""
        BEGIN;
        CREATE TABLE knowledge_documents (
            facility_id TEXT NOT NULL REFERENCES facilities, document_id TEXT NOT NULL, document_version TEXT NOT NULL,
            metadata_json TEXT NOT NULL, content TEXT NOT NULL, content_digest TEXT NOT NULL,
            approval_status TEXT NOT NULL CHECK(approval_status IN ('draft','approved','withdrawn')),
            allowed_roles_json TEXT NOT NULL, effective_at TEXT NOT NULL, retired_at TEXT,
            reviewed_conflict INTEGER NOT NULL CHECK(reviewed_conflict IN (0,1)),
            CHECK(retired_at IS NULL OR retired_at>effective_at), PRIMARY KEY(facility_id,document_id,document_version));
        CREATE TABLE knowledge_chunks (
            reference_id TEXT PRIMARY KEY, facility_id TEXT NOT NULL, document_id TEXT NOT NULL, document_version TEXT NOT NULL,
            section TEXT NOT NULL, topic TEXT NOT NULL CHECK(topic IN ('parking_order','entry_exit','announcement','user_guidance')),
            procedure_group_id TEXT NOT NULL, content TEXT NOT NULL, chunk_digest TEXT NOT NULL,
            FOREIGN KEY(facility_id,document_id,document_version) REFERENCES knowledge_documents);
        CREATE TABLE knowledge_releases (
            facility_id TEXT NOT NULL REFERENCES facilities, knowledge_release_id TEXT NOT NULL,
            manifest_digest TEXT NOT NULL, index_version TEXT NOT NULL, index_digest TEXT NOT NULL, index_file TEXT NOT NULL,
            created_at TEXT NOT NULL, PRIMARY KEY(facility_id,knowledge_release_id));
        CREATE TABLE knowledge_release_documents (
            facility_id TEXT NOT NULL, knowledge_release_id TEXT NOT NULL, document_id TEXT NOT NULL, document_version TEXT NOT NULL,
            PRIMARY KEY(facility_id,knowledge_release_id,document_id,document_version),
            FOREIGN KEY(facility_id,knowledge_release_id) REFERENCES knowledge_releases,
            FOREIGN KEY(facility_id,document_id,document_version) REFERENCES knowledge_documents);
        CREATE TABLE policies (
            facility_id TEXT NOT NULL, policy_version INTEGER NOT NULL CHECK(policy_version>0), knowledge_release_id TEXT NOT NULL,
            effective_at TEXT NOT NULL, retired_at TEXT, content_json TEXT NOT NULL,
            CHECK(retired_at IS NULL OR retired_at>effective_at), PRIMARY KEY(facility_id,policy_version),
            FOREIGN KEY(facility_id,knowledge_release_id) REFERENCES knowledge_releases);
        CREATE TABLE knowledge_retrievals (
            retrieval_id TEXT PRIMARY KEY, facility_id TEXT NOT NULL, run_id TEXT NOT NULL,
            requester_ref TEXT NOT NULL REFERENCES users(user_id), access_scope_digest TEXT NOT NULL,
            query_digest TEXT NOT NULL, result_json TEXT NOT NULL,
            FOREIGN KEY(facility_id,run_id) REFERENCES run_facilities(facility_id,run_id));
        CREATE TABLE knowledge_audit (audit_id TEXT PRIMARY KEY, facility_id TEXT NOT NULL REFERENCES facilities,
            occurred_at TEXT NOT NULL, kind TEXT NOT NULL, detail_json TEXT NOT NULL);
        CREATE INDEX knowledge_lookup ON knowledge_chunks(facility_id,document_id,document_version,procedure_group_id);
        CREATE INDEX retrieval_scope ON knowledge_retrievals(facility_id,run_id,requester_ref);
        CREATE TRIGGER manual_content_immutable BEFORE UPDATE OF facility_id,document_id,document_version,metadata_json,content,content_digest
            ON knowledge_documents BEGIN SELECT RAISE(ABORT,'Immutable manual version'); END;
        CREATE TRIGGER chunks_immutable BEFORE UPDATE ON knowledge_chunks BEGIN SELECT RAISE(ABORT,'Immutable manual chunk'); END;
        CREATE TRIGGER chunks_no_delete BEFORE DELETE ON knowledge_chunks BEGIN SELECT RAISE(ABORT,'Immutable manual chunk'); END;
        CREATE TRIGGER releases_immutable BEFORE UPDATE ON knowledge_releases BEGIN SELECT RAISE(ABORT,'Immutable release'); END;
        CREATE TRIGGER release_members_immutable BEFORE UPDATE ON knowledge_release_documents BEGIN SELECT RAISE(ABORT,'Immutable release membership'); END;
        CREATE TRIGGER release_members_no_delete BEFORE DELETE ON knowledge_release_documents BEGIN SELECT RAISE(ABORT,'Immutable release membership'); END;
        CREATE TRIGGER retrievals_immutable BEFORE UPDATE ON knowledge_retrievals BEGIN SELECT RAISE(ABORT,'Immutable retrieval snapshot'); END;
        CREATE TRIGGER policy_content_immutable BEFORE UPDATE OF facility_id,policy_version,knowledge_release_id,effective_at,content_json
            ON policies BEGIN SELECT RAISE(ABORT,'Immutable policy version'); END;
    """)
    # A policy window is exclusive per facility, including updates/retirement.
    for op in ("INSERT", "UPDATE"):
        db.execute(f"""CREATE TRIGGER policy_no_overlap_{op.lower()} BEFORE {op} ON policies
            WHEN EXISTS(SELECT 1 FROM policies p WHERE p.facility_id=NEW.facility_id AND p.policy_version!=NEW.policy_version
              AND (p.retired_at IS NULL OR NEW.effective_at<p.retired_at)
              AND (NEW.retired_at IS NULL OR p.effective_at<NEW.retired_at))
            BEGIN SELECT RAISE(ABORT,'Overlapping policy interval'); END""")
    db.execute("PRAGMA user_version=3")
    db.commit()


class Knowledge:
    def __init__(self, store, index_dir: Path, *, clock=wall_now, source_root=SOURCE_ROOT):
        self.store, self.db, self.index_dir, self.clock = store, store.db, index_dir, clock
        # Trusted configuration, never a tool argument. Tests use an isolated
        # synthetic source directory; product startup fixes SOURCE_ROOT.
        self.source_root = Path(source_root).resolve()
        self._reader = None
        self._reader_done = None

    def _audit(self, facility, kind, detail):
        self.db.execute("INSERT INTO knowledge_audit VALUES (?,?,?,?,?)",
                        (uuid4().hex, facility, self.clock(), kind, encoded(detail)))

    def activate(self, manifest_path: Path):
        """Trusted local administration only. No model/HTTP path or approval input."""
        source = manifest_path.resolve(strict=True)
        if not source.is_relative_to(self.source_root) or source.suffix != ".json":
            raise ValueError("Manifest outside the allowed synthetic-manual directory")
        raw_manifest = source.read_bytes()
        if len(raw_manifest) > 100000:
            raise ValueError("Manifest too large")
        manifest = KnowledgeManifest.model_validate_json(raw_manifest)
        policy = manifest.policy
        if (policy.facility_id != manifest.facility_id or policy.knowledge_release_id != manifest.knowledge_release_id
                or policy.retired_at is not None):
            raise ValueError("Policy/release scope mismatch")
        if not self.db.execute("SELECT 1 FROM facilities WHERE facility_id=?", (manifest.facility_id,)).fetchone():
            raise ValueError("Unknown facility")
        prepared, references, groups, document_keys = [], set(), {}, set()
        for meta in manifest.documents:
            key = (meta.facility_id, meta.document_id, meta.document_version)
            if key in document_keys or meta.facility_id != manifest.facility_id or meta.approval_status != "approved" or meta.reviewed_conflict:
                raise ValueError("Ambiguous/unapproved/conflicting manual")
            document_keys.add(key)
            relative = Path(meta.file)
            # Only named JSON manuals beside this manifest: no nested traversal,
            # symlinks escaping the root, repository-wide scans or URLs.
            if relative.is_absolute() or relative.name != meta.file or not meta.file.endswith(".manual.json"):
                raise ValueError("Invalid manual source")
            path = (source.parent / relative).resolve(strict=True)
            if path.parent != source.parent or not path.is_relative_to(self.source_root):
                raise ValueError("Manual outside allowed directory")
            raw = path.read_bytes()
            if len(raw) > 300000 or sha(raw) != meta.content_digest:
                raise ValueError("Manual digest/size mismatch")
            manual = Manual.model_validate_json(raw)
            for chunk in manual.chunks:
                if chunk.reference_id in references or (chunk.procedure_group_id in groups and groups[chunk.procedure_group_id] != key):
                    raise ValueError("Ambiguous manual references/groups")
                references.add(chunk.reference_id)
                groups[chunk.procedure_group_id] = key
            prepared.append((meta, raw.decode("utf-8"), manual))
        index = {"index_version": manifest.index_version, "manifest_digest": sha(raw_manifest),
                 "entries": {c.reference_id: terms(meta.title + " " + c.section + " " + c.content)
                             for meta, _, manual in prepared for c in manual.chunks}}
        index_bytes = encoded(index).encode("utf-8")
        filename = "idx-" + sha(index_bytes)[7:] + ".json"
        self.index_dir.mkdir(parents=True, exist_ok=True)
        target = self.index_dir / filename
        staging = self.index_dir / (filename + "." + uuid4().hex + ".pending")
        staging.write_bytes(index_bytes)
        staging.replace(target)  # Publish derivative first; DB switch remains atomic.
        with transaction(self.db):
            for meta, content, manual in prepared:
                key = (meta.facility_id, meta.document_id, meta.document_version)
                old = self.db.execute("SELECT content_digest,metadata_json FROM knowledge_documents WHERE facility_id=? AND document_id=? AND document_version=?", key).fetchone()
                if old:
                    if old[0] != meta.content_digest or old[1] != encoded(meta.model_dump()):
                        raise ValueError("Existing manual version changed")
                    continue  # Never restore withdrawn access from a seed file.
                self.db.execute("INSERT INTO knowledge_documents VALUES (?,?,?,?,?,?,?,?,?,?,?)", (*key,
                    encoded(meta.model_dump()), content, meta.content_digest, meta.approval_status,
                    encoded(meta.allowed_roles), meta.effective_at, meta.retired_at, int(meta.reviewed_conflict)))
                for c in manual.chunks:
                    self.db.execute("INSERT INTO knowledge_chunks VALUES (?,?,?,?,?,?,?,?,?)", (c.reference_id, *key,
                        c.section, c.topic, c.procedure_group_id, c.content, sha(c.content)))
            release_key = (manifest.facility_id, manifest.knowledge_release_id)
            old_release = self.db.execute("SELECT manifest_digest,index_digest FROM knowledge_releases WHERE facility_id=? AND knowledge_release_id=?", release_key).fetchone()
            if old_release:
                if tuple(old_release) != (sha(raw_manifest), sha(index_bytes)):
                    raise ValueError("Existing release changed")
            else:
                self.db.execute("INSERT INTO knowledge_releases VALUES (?,?,?,?,?,?,?)", (*release_key,
                    sha(raw_manifest), manifest.index_version, sha(index_bytes), filename, self.clock()))
                for meta, _, _ in prepared:
                    self.db.execute("INSERT INTO knowledge_release_documents VALUES (?,?,?,?)", (*release_key, meta.document_id, meta.document_version))
            # A backdated start must not make an already expired bundle current.
            evaluated = max(policy.effective_at, self.clock())
            rows = self._eligible(manifest.facility_id, manifest.knowledge_release_id, "test_operator", evaluated)
            available_topics = {r["topic"] for r in rows}
            if any(r["reviewed_conflict"] for r in rows) or any(not set(r.required_topics) <= available_topics for r in policy.knowledge_requirements):
                raise ValueError("Incomplete/conflicting policy evidence")
            # Do not permit driver instructions as the authority for operator actions.
            if any(not {"owner", "test_operator"} <= set(json.loads(r["allowed_roles_json"])) for r in rows):
                raise ValueError("Operator manual access incomplete")
            old_policy = self.db.execute("SELECT content_json FROM policies WHERE facility_id=? AND policy_version=?", (policy.facility_id, policy.policy_version)).fetchone()
            if old_policy:
                if old_policy[0] != encoded(policy.model_dump()):
                    raise ValueError("Existing policy version changed")
            else:
                self.db.execute("UPDATE policies SET retired_at=? WHERE facility_id=? AND retired_at IS NULL AND effective_at<?",
                                (policy.effective_at, policy.facility_id, policy.effective_at))
                self.db.execute("INSERT INTO policies VALUES (?,?,?,?,?,?)", (policy.facility_id, policy.policy_version,
                    policy.knowledge_release_id, policy.effective_at, policy.retired_at, encoded(policy.model_dump())))
                self._audit(policy.facility_id, "release_activated", {"policy_version": policy.policy_version, "knowledge_release_id": manifest.knowledge_release_id})

    def document_access(self, facility_id, document_id, document_version, *, status=None, roles=None, retired_at=None, conflict=None):
        """Trusted administration; capability is not exposed to the product tools."""
        key = (facility_id, document_id, document_version)
        row = self.db.execute("SELECT * FROM knowledge_documents WHERE facility_id=? AND document_id=? AND document_version=?", key).fetchone()
        if not row:
            raise ValueError("Unknown manual")
        update = json.loads(row["metadata_json"])
        update.update(approval_status=row["approval_status"], allowed_roles=json.loads(row["allowed_roles_json"]),
                      retired_at=row["retired_at"], reviewed_conflict=bool(row["reviewed_conflict"]))
        for name, value in (("approval_status", status), ("allowed_roles", roles), ("retired_at", retired_at), ("reviewed_conflict", conflict)):
            if value is not None:
                update[name] = value
        from contracts.knowledge import ManifestDocument
        validated = ManifestDocument.model_validate(update)
        with self.db:
            self.db.execute("UPDATE knowledge_documents SET approval_status=?,allowed_roles_json=?,retired_at=?,reviewed_conflict=? WHERE facility_id=? AND document_id=? AND document_version=?",
                            (validated.approval_status, encoded(validated.allowed_roles), validated.retired_at, int(validated.reviewed_conflict), *key))
            self._audit(facility_id, "manual_access_changed", {"document_id": document_id, "document_version": document_version})

    def current_policy(self, facility_id, now=None):
        now = now or self.clock()
        row = self.db.execute("SELECT * FROM policies WHERE facility_id=? AND effective_at<=? AND (retired_at IS NULL OR retired_at>?)",
                              (facility_id, now, now)).fetchall()
        if len(row) != 1:
            raise ValueError("Current policy unavailable/ambiguous")
        data = json.loads(row[0]["content_json"])
        data["retired_at"] = row[0]["retired_at"]
        return OperatingPolicy.model_validate(data)

    def _eligible(self, facility, release, role, now):
        return self.db.execute("""SELECT c.*,d.metadata_json,d.content_digest,d.allowed_roles_json,
            d.effective_at,d.retired_at,d.reviewed_conflict FROM knowledge_chunks c
            JOIN knowledge_documents d USING(facility_id,document_id,document_version)
            JOIN knowledge_release_documents rd USING(facility_id,document_id,document_version)
            WHERE rd.facility_id=? AND rd.knowledge_release_id=? AND d.approval_status='approved'
            AND d.effective_at<=? AND (d.retired_at IS NULL OR d.retired_at>?)
            AND EXISTS(SELECT 1 FROM json_each(d.allowed_roles_json) WHERE value=?) ORDER BY c.reference_id""",
                               (facility, release, now, now, role)).fetchall()

    def _read_index_file(self, expected):
        # File operations run in a bounded reader; this function never uses DB,
        # writes files, changes world state or publishes late results.
        path = (self.index_dir / expected["index_file"]).resolve(strict=True)
        if path.parent != self.index_dir.resolve():
            raise ValueError("Index outside derived-data directory")
        with path.open("rb") as stream:
            raw = stream.read(1000001)
        if len(raw) > 1000000 or sha(raw) != expected["index_digest"]:
            raise ValueError("Index digest mismatch")
        index = json.loads(raw)
        if index["manifest_digest"] != expected["manifest_digest"] or index["index_version"] != expected["index_version"]:
            raise ValueError("Index version mismatch")
        return expected["index_version"], index["entries"]

    def _index(self, facility, release, *, expires=None):
        expires = expires if expires is not None else time.monotonic() + LIMITS["timeout_s"]
        row = self.db.execute("SELECT * FROM knowledge_releases WHERE facility_id=? AND knowledge_release_id=?", (facility, release)).fetchone()
        if not row or not re.fullmatch(r"idx-[0-9a-f]{64}\.json", row["index_file"]):
            raise ValueError("Index unavailable")
        if self._reader is not None and self._reader.is_alive() and not self._reader_done.is_set():
            raise TimeoutError("Previous index read has not returned")
        completed, result, expected = threading.Event(), [], dict(row)
        def read():
            try:
                result.append((True, self._read_index_file(expected)))
            except Exception:
                result.append((False, ValueError("Index read unavailable")))
            finally:
                completed.set()
        self._reader = threading.Thread(target=read, daemon=True, name="knowledge-index-reader")
        self._reader_done = completed
        self._reader.start()
        if not completed.wait(max(0, expires - time.monotonic())) or time.monotonic() >= expires:
            raise TimeoutError("Index read deadline exceeded")
        if not result[0][0]:
            raise result[0][1]
        return result[0][1]

    def scope(self, username):
        role = self.store.registry.role(username)
        if not role:
            raise ApiError(403, "FORBIDDEN", "시설 지식 조회 권한이 없습니다.")
        return role, sha(encoded({"username": username, "role": role}))

    def _reference(self, row):
        # Do not duplicate a whole manual in every eligible chunk row. Fetch its
        # immutable source only when constructing/rechecking a returned citation.
        source = self.db.execute("SELECT content FROM knowledge_documents WHERE facility_id=? AND document_id=? AND document_version=?",
                                 (row["facility_id"], row["document_id"], row["document_version"])).fetchone()
        if not source or sha(row["content"]) != row["chunk_digest"] or sha(source[0]) != row["content_digest"]:
            raise ValueError("Manual content integrity failure")
        metadata = json.loads(row["metadata_json"])
        original = next((c for c in Manual.model_validate_json(source[0]).chunks if c.reference_id == row["reference_id"]), None)
        if original is None or any(getattr(original, name) != row[name] for name in ("content", "section", "topic", "procedure_group_id")):
            raise ValueError("Manual chunk provenance mismatch")
        return {"reference_id": row["reference_id"], "document_id": row["document_id"], "document_version": row["document_version"],
                "title": metadata["title"], "section": row["section"], "topic": row["topic"],
                "procedure_group_id": row["procedure_group_id"], "excerpt": row["content"], "content_digest": row["content_digest"],
                "chunk_digest": row["chunk_digest"], "effective_at": row["effective_at"], "retired_at": row["retired_at"]}

    def search(self, username, query: KnowledgeQuery):
        role, scope = self.scope(username)
        started, now = time.monotonic(), self.clock()
        result = {"retrieval_id": "ret-" + uuid4().hex, "facility_id": query.facility_id, "run_id": query.run_id,
                  "status": "unavailable", "evaluated_at": now, "retrieved_at": now, "references": []}
        def deadline():
            if time.monotonic() - started >= LIMITS["timeout_s"]:
                raise TimeoutError("Retrieval timeout")
        prior_busy = self.db.execute("PRAGMA busy_timeout").fetchone()[0]
        self.db.execute("PRAGMA busy_timeout=100")
        self.db.set_progress_handler(lambda: int(time.monotonic()-started >= LIMITS["timeout_s"]), 1000)
        try:
            with transaction(self.db):
                policy = self.current_policy(query.facility_id, now)
                deadline()
                result.update(policy_version=policy.policy_version, knowledge_release_id=policy.knowledge_release_id)
                version, index = self._index(query.facility_id, policy.knowledge_release_id, expires=started+LIMITS["timeout_s"])
                deadline()
                result["index_version"] = version
                rows = self._eligible(query.facility_id, policy.knowledge_release_id, role, now)
                deadline()
                groups = defaultdict(list)
                for row in rows:
                    groups[row["procedure_group_id"]].append(row)
                ranked = []
                query_terms = set(terms(query.query))
                for group, members in groups.items():
                    deadline()
                    if query.topic is not None and not any(r["topic"] == query.topic for r in members):
                        continue
                    score = sum(len(query_terms & set(index[r["reference_id"]])) for r in members)
                    if score:
                        ranked.append((score, group, members))
                ranked.sort(key=lambda x: (-x[0], x[1]))
                if any(r["reviewed_conflict"] for _, _, members in ranked for r in members):
                    result.update(status="conflict", reason_code="reviewed_conflict")
                else:
                    selected, length = [], 0
                    for _, _, members in ranked[:LIMITS["groups"]]:
                        amount = sum(len(r["content"]) for r in members)
                        if length+amount > LIMITS["chars"] or len(selected)+len(members)>100:
                            result["reason_code"] = "incomplete_context"
                            continue  # Never truncate a condition/exception group.
                        selected.extend(self._reference(r) for r in members)
                        length += amount
                    result.update(status="matched" if selected else "no_match", references=selected)
                    if result.get("reason_code") == "incomplete_context":
                        result.update(status="no_match", references=[])
                deadline()
                # Current role and wall-time eligibility are checked again before
                # publishing. The retrieval row pins exactly the returned references.
                role_after, scope_after = self.scope(username)
                if scope_after != scope or self.current_policy(query.facility_id).policy_version != policy.policy_version:
                    raise ValueError("Scope/policy changed")
                current_ids = {r["reference_id"] for r in self._eligible(query.facility_id, policy.knowledge_release_id, role_after, self.clock())}
                if any(r["reference_id"] not in current_ids for r in result["references"]):
                    raise ValueError("Manual validity changed")
                result["retrieved_at"] = self.clock()
                validated = KnowledgeResult.model_validate(result)
                deadline()
                self.db.execute("INSERT INTO knowledge_retrievals VALUES (?,?,?,?,?,?,?)", (validated.retrieval_id,
                    query.facility_id, query.run_id, username, scope, sha(query.query), validated.model_dump_json()))
                deadline()
            return validated
        except ApiError:
            raise
        except (OSError, ValueError, KeyError, sqlite3.Error, TimeoutError):
            result.update(status="unavailable", references=[], reason_code="retrieval_unavailable", retrieved_at=self.clock())
            validated = KnowledgeResult.model_validate(result)
            # Best effort within the same deadline. Failed audit storage can never
            # create matched evidence or authorise a new operation.
            if time.monotonic()-started < LIMITS["timeout_s"]:
                try:
                    with transaction(self.db):
                        self.db.execute("INSERT INTO knowledge_retrievals VALUES (?,?,?,?,?,?,?)", (validated.retrieval_id,
                            query.facility_id, query.run_id, username, scope, sha(query.query), validated.model_dump_json()))
                except sqlite3.Error:
                    pass
            return validated
        finally:
            self.db.set_progress_handler(None, 0)
            self.db.execute(f"PRAGMA busy_timeout={int(prior_busy)}")

    def validate_evidence(self, username, facility_id, run_id, evidence: KnowledgeEvidence | None, *, tool_name, purpose):
        """Server-derived action/purpose, to be called at acceptance AND dispatch."""
        role, scope = self.scope(username)
        expires = time.monotonic() + LIMITS["timeout_s"]
        try:
            with bounded_database(self.db, expires), transaction(self.db):
                policy = self.current_policy(facility_id)
                requirements = next((r.required_topics for r in policy.knowledge_requirements
                                     if (r.tool_name, r.purpose) == (tool_name, purpose)), None)
                if requirements is None:
                    raise ApiError(409, "KNOWLEDGE_REQUIRED", "분류된 업무 목적과 운영 근거가 필요합니다.")
                if requirements == []:
                    return {"current_validity": "valid", "validity_checked_at": self.clock(), "policy_version": policy.policy_version}
                if evidence is None:
                    raise ApiError(409, "KNOWLEDGE_REQUIRED", "실제 검색한 운영 근거가 필요합니다.")
                row = self.db.execute("SELECT * FROM knowledge_retrievals WHERE retrieval_id=? AND requester_ref=? AND facility_id=? AND run_id=?",
                                      (evidence.retrieval_id, username, facility_id, run_id)).fetchone()
                if not row or row["access_scope_digest"] != scope:
                    raise ApiError(403, "FORBIDDEN", "현재 요청의 운영 근거를 확인할 수 없습니다.")
                result = KnowledgeResult.model_validate_json(row["result_json"])
                if result.status != "matched":
                    raise ApiError(409, "KNOWLEDGE_REQUIRED", "일치한 운영 근거가 필요합니다.")
                if result.policy_version != policy.policy_version or result.knowledge_release_id != policy.knowledge_release_id:
                    raise ApiError(409, "KNOWLEDGE_CHANGED", "운영 정책/문서가 바뀌었습니다. 다시 조회하세요.")
                version, _ = self._index(facility_id, policy.knowledge_release_id, expires=expires)
                if version != result.index_version:
                    raise ValueError("Index changed")
                ids = set(evidence.reference_ids)
                returned = {r.reference_id: r for r in result.references}
                if not ids <= returned.keys():
                    raise ApiError(403, "FORBIDDEN", "검색에서 반환한 참조만 사용할 수 있습니다.")
                group_ids = {returned[ref].procedure_group_id for ref in ids}
                rows = self._eligible(facility_id, policy.knowledge_release_id, role, self.clock())
                current = {r["reference_id"]: r for r in rows}
                selected_groups = {r["reference_id"] for r in rows if r["procedure_group_id"] in group_ids}
                if ids != selected_groups:
                    raise ApiError(409, "KNOWLEDGE_CHANGED", "완전하고 유효한 절차 묶음이 필요합니다.")
                for ref in ids:
                    if self._reference(current[ref]) != returned[ref].model_dump():
                        raise ApiError(409, "KNOWLEDGE_CHANGED", "문서 근거가 바뀌었습니다.")
                    if current[ref]["reviewed_conflict"]:
                        raise ApiError(409, "KNOWLEDGE_CONFLICT", "검토된 문서 충돌로 실행을 보류합니다.")
                if not set(requirements) <= {returned[ref].topic for ref in ids}:
                    raise ApiError(409, "KNOWLEDGE_REQUIRED", "업무 목적의 필수 문서 주제가 필요합니다.")
                checked = self.clock()
                if time.monotonic() >= expires:
                    raise TimeoutError("Evidence validation deadline exceeded")
                current_policy = self.current_policy(facility_id, checked)
                latest = {r["reference_id"]: r for r in self._eligible(facility_id, policy.knowledge_release_id, role, checked)}
                if (current_policy.policy_version != policy.policy_version or self.scope(username)[1] != scope
                        or any(ref not in latest or self._reference(latest[ref]) != returned[ref].model_dump()
                               or latest[ref]["reviewed_conflict"] for ref in ids)):
                    raise ApiError(409, "KNOWLEDGE_CHANGED", "검증 중 운영 근거가 바뀌었습니다.")
                return {"current_validity": "valid", "validity_checked_at": checked, "policy_version": policy.policy_version}
        except ApiError:
            raise
        except (OSError, ValueError, KeyError, sqlite3.Error, TimeoutError):
            raise ApiError(503, "KNOWLEDGE_UNAVAILABLE", "운영 근거를 확인할 수 없습니다.") from None
