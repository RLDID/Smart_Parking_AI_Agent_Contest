"""Read-only business history with bounded immutable snapshots and live authority."""
from dataclasses import dataclass
from datetime import datetime
import base64
import hashlib
import heapq
import hmac
import json
import secrets
import time

from fastapi import Query, Request
from agent.tools import validate_session
from backend.auth import ApiError
from contracts.history_views import (CommandsHistory, ExecutionsHistory, IncidentTimeline,
    CommandProgress, HistoryCommand, HistoryExecution, TimelineRecord, PlanProgress,
    PlanStepProgress, StepAttempt)
from simulator.world import utc_now


LIMITS = ['Mutable rows show the latest state at as_of_utc, not every intermediate transition.',
          'Movement and incident-resolution times are not inferred from receipts or responses.',
          'Snapshots expire after 300 seconds and are lost on process restart.',
          'Ambiguous audit target IDs shared by different record types are omitted.']
TABLE_KEYS = {'commands': 'command_id', 'executions': 'execution_id', 'incidents': 'incident_id',
    'notifications': 'notification_id', 'delivery_attempts': 'attempt_id',
    'notification_receipts': 'receipt_id', 'notification_responses': 'response_id',
    'followups': 'followup_id', 'audit_events': 'audit_id'}
COMMAND_FIELDS = tuple(HistoryCommand.model_fields)
EXECUTION_FIELDS = tuple(HistoryExecution.model_fields)


def projection(row, fields):
    return {field: row[field] for field in fields}


def load_steps(raw):
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except (TypeError, ValueError):
        return []


@dataclass
class Snapshot:
    binding: tuple
    expires: float
    as_of: str
    rows: list


class HistoryViews:
    TTL = 300
    MAX_ROWS = 2000
    MAX_SNAPSHOTS = 32
    MAX_CACHED_ROWS = 20000

    def __init__(self, runtime, clock=time.monotonic):
        self.runtime, self.clock = runtime, clock
        self.secret = secrets.token_bytes(32)
        self.generation = secrets.token_urlsafe(12)
        self.snapshots = {}

    @property
    def db(self):
        return self.runtime.store.db

    def scope(self, session, facility_id, run_id=None, operator=False):
        validate_session(self.runtime, session)
        if operator and session.role not in ('owner', 'test_operator'):
            raise ApiError(403, 'FORBIDDEN', '운영 이력 조회 권한이 필요합니다.')
        memberships = self.db.execute('''SELECT m.rowid,m.* FROM memberships m JOIN users u USING(user_id)
            WHERE u.username=? AND u.disabled_at IS NULL AND m.facility_id=?
            AND m.role=? AND m.revoked_at IS NULL ORDER BY m.rowid''',
            (session.username, facility_id, session.role)).fetchall()
        if not memberships:
            raise ApiError(404, 'NOT_FOUND', '현재 권한에서 시설을 찾을 수 없습니다.')
        if run_id is not None and not self.db.execute(
                'SELECT 1 FROM run_facilities WHERE facility_id=? AND run_id=?',
                (facility_id, run_id)).fetchone():
            raise ApiError(404, 'NOT_FOUND', '시설에 연결된 회차를 찾을 수 없습니다.')
        grant = repr([tuple(row) for row in memberships])
        if session.role == 'driver':
            grant += self.runtime.store.registry.scope_stamp(session.username)
        return (session.username, session.role, id(session), hashlib.sha256(grant.encode()).hexdigest())

    def scoped(self, table, record_id):
        key = TABLE_KEYS[table]
        row = self.db.execute(f'SELECT * FROM {table} WHERE {key}=?', (record_id,)).fetchone()
        if row is None:
            raise ApiError(404, 'NOT_FOUND', '현재 권한에서 자료를 찾을 수 없습니다.')
        return row

    def _token(self, snapshot_id, key):
        data = json.dumps([1, self.generation, snapshot_id, key], separators=(',', ':')).encode()
        signature = hmac.digest(self.secret, data, 'sha256')
        return base64.urlsafe_b64encode(data + signature).decode().rstrip('=')

    def _decode(self, cursor):
        try:
            if len(cursor) > 512:
                raise ValueError()
            raw = base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True)
            data, signature = raw[:-32], raw[-32:]
            version, generation, snapshot_id, key = json.loads(data)
            if version != 1 or not isinstance(generation, str) or not isinstance(snapshot_id, str) or type(key) is not int:
                raise ValueError()
            if generation != self.generation:
                # A well-formed token from another ephemeral process can never resume here.
                # No payload or snapshot is accepted before authenticating this generation.
                raise ApiError(409, 'HISTORY_CURSOR_EXPIRED', '새 이력 조회를 시작하세요.')
            if not hmac.compare_digest(signature, hmac.digest(self.secret, data, 'sha256')):
                raise ValueError()
            return snapshot_id, key
        except (ValueError, TypeError, UnicodeError):
            raise ApiError(422, 'INVALID_HISTORY_CURSOR', '이력 조회 cursor를 확인하세요.') from None

    def page(self, binding, cursor, limit, capture):
        now = self.clock()
        self.snapshots = {key: value for key, value in self.snapshots.items() if value.expires > now}
        if cursor:
            snapshot_id, last_key = self._decode(cursor)
            snapshot = self.snapshots.get(snapshot_id)
            if snapshot is None:
                raise ApiError(409, 'HISTORY_CURSOR_EXPIRED', '새 이력 조회를 시작하세요.')
            if snapshot.binding != binding:
                raise ApiError(409, 'HISTORY_CURSOR_SCOPE_CHANGED', '동일 권한·필터로 새 조회를 시작하세요.')
            if not 0 <= last_key < len(snapshot.rows):
                raise ApiError(422, 'INVALID_HISTORY_CURSOR', '이력 조회 위치를 확인하세요.')
        else:
            rows = capture()
            if len(rows) > self.MAX_ROWS:
                raise ApiError(409, 'HISTORY_WINDOW_TOO_LARGE', '회차 필터로 이력 범위를 줄이세요.')
            while self.snapshots and (len(self.snapshots) >= self.MAX_SNAPSHOTS
                    or sum(len(value.rows) for value in self.snapshots.values()) + len(rows) > self.MAX_CACHED_ROWS):
                del self.snapshots[next(iter(self.snapshots))]
            snapshot_id, last_key = secrets.token_urlsafe(18), 0
            snapshot = Snapshot(binding, now + self.TTL, utc_now(), rows)
            self.snapshots[snapshot_id] = snapshot
        # The immutable sequence number is the snapshot keyset and upper bound.
        # Advance even if a current row loses authority; an empty page may have a cursor.
        end = min(last_key + limit, len(snapshot.rows))
        items = [value for table, record_id, relation, value in snapshot.rows[last_key:end]
                 if self._visible(table, record_id, relation, binding)]
        return {'as_of_utc': snapshot.as_of, 'items': items,
                'next_cursor': self._token(snapshot_id, end) if end < len(snapshot.rows) else None,
                'preservation_limits': LIMITS}

    def _visible(self, table, record_id, relation, binding):
        key = TABLE_KEYS[table]
        facility, run_id = binding[5], binding[6]
        row = self.db.execute(f'''SELECT * FROM {table} WHERE {key}=? AND facility_id=?
            AND EXISTS (SELECT 1 FROM run_facilities rf WHERE rf.run_id={table}.run_id
                        AND rf.facility_id={table}.facility_id)''', (record_id, facility)).fetchone()
        if row is None or run_id is not None and row['run_id'] != run_id:
            return False
        if table == 'commands' and binding[1] == 'driver' and row['requester_id'] != self.db.execute('SELECT user_id FROM users WHERE username=?', (binding[0],)).fetchone()[0]:
            return False
        if relation is not None:
            if relation[0] == 'audit_target':
                _, target_table, target_id, target_relation = relation
                return row['target_ref'] == target_id and self._visible(target_table, target_id, target_relation, binding)
            column, expected = relation
            if row[column] != expected:
                return False
            if column == 'notification_id':
                parent = self.db.execute('''SELECT 1 FROM notifications WHERE notification_id=?
                    AND facility_id=? AND run_id=? AND incident_id=?''',
                    (expected, facility, row['run_id'], binding[7])).fetchone()
                if not parent:
                    return False
        return True

    def listing(self, table, session, facility, run_id, cursor, limit, authority):
        binding = authority + (table, facility, run_id, None, limit)
        def capture():
            driver = ' AND requester_id=(SELECT user_id FROM users WHERE username=?)' if session.role == 'driver' else ''
            run = ' AND run_id=?' if run_id is not None else ''
            args = [facility] + ([run_id] if run_id is not None else []) + ([session.username] if driver else [])
            rows = self.db.execute(f'''SELECT * FROM {table} WHERE facility_id=? {run} {driver}
                AND EXISTS (SELECT 1 FROM run_facilities rf WHERE rf.facility_id={table}.facility_id
                            AND rf.run_id={table}.run_id) ORDER BY rowid LIMIT ?''',
                            (*args, self.MAX_ROWS + 1)).fetchall()
            fields = COMMAND_FIELDS if table == 'commands' else EXECUTION_FIELDS
            return [(table, row[TABLE_KEYS[table]], None, projection(row, fields)) for row in rows]
        return self.page(binding, cursor, limit, capture)

    def timeline(self, incident, authority, cursor, limit):
        facility, run_id, iid = incident['facility_id'], incident['run_id'], incident['incident_id']
        binding = authority + ('timeline', facility, run_id, iid, limit)
        def capture():
            entries = []
            def add(table, row, kind, at, fields, parent=None, relation=None, sim=None):
                rid = row[TABLE_KEYS[table]]
                value = TimelineRecord(event_id=table + ':' + rid, kind=kind, record_id=rid,
                    parent_record_id=parent, recorded_at_utc=at, sim_time_ms=sim,
                    details={field: row[field] for field in fields}).model_dump()
                entries.append((table, rid, relation, value))
            common = ('created_at', 'updated_at', 'resource_version')
            add('incidents', incident, 'incident.latest_state', incident['created_at'], ('status',) + common)
            for table, kind, fields in (
                ('executions', 'execution.latest_state', ('status', 'mode', 'tool_name', 'error_code') + common),
                ('notifications', 'notification.latest_state', ('delivery_status', 'mode', 'purpose') + common),
                ('followups', 'followup.latest_state', ('status', 'clock', 'due_sim_time_ms', 'due_at') + common)):
                rows = self.db.execute(f'''SELECT * FROM {table} WHERE facility_id=? AND run_id=?
                    AND incident_id=? ORDER BY rowid LIMIT ?''',
                    (facility, run_id, iid, self.MAX_ROWS + 1)).fetchall()
                for row in rows:
                    parent = row['execution_id'] if table == 'notifications' else iid
                    add(table, row, kind, row['created_at'], fields, parent, ('incident_id', iid),
                        row['applied_sim_time_ms'] if table == 'executions' else None)
            for table, kind, at, fields in (
                ('delivery_attempts', 'delivery_attempt.latest_state', 'requested_at',
                    ('status', 'attempt_number', 'error_code') + common),
                ('notification_receipts', 'notification.receipt', 'recorded_at', ('received_at',)),
                ('notification_responses', 'notification.response', 'responded_at', ('response',))):
                rows = self.db.execute(f'''SELECT x.* FROM {table} x JOIN notifications n USING(notification_id)
                    WHERE x.facility_id=? AND x.run_id=? AND n.facility_id=x.facility_id
                    AND n.run_id=x.run_id AND n.incident_id=? ORDER BY x.rowid LIMIT ?''',
                    (facility, run_id, iid, self.MAX_ROWS + 1)).fetchall()
                for row in rows:
                    add(table, row, kind, row[at], fields, row['notification_id'], ('notification_id', row['notification_id']))
            targets = {entry[1] for entry in entries}
            target_entries = {}
            for entry in entries:
                target_entries.setdefault(entry[1], []).append(entry)
            # An explicit target reference is required; text/correlation proximity is insufficient.
            audits = []
            target_ids = sorted(targets)
            for start in range(0, len(target_ids), 400):
                chunk = target_ids[start:start + 400]
                marks = ','.join('?' for _ in chunk)
                audits.extend(self.db.execute(f'''SELECT * FROM audit_events WHERE facility_id=? AND run_id=?
                    AND target_ref IN ({marks}) ORDER BY rowid LIMIT ?''',
                    (facility, run_id, *chunk, self.MAX_ROWS + 1)).fetchall())
            for row in audits:
                parents = target_entries[row['target_ref']]
                if len(parents) != 1:
                    continue  # The untyped audit target is ambiguous across tables.
                target = parents[0]
                add('audit_events', row, 'audit', row['occurred_at'], ('action', 'outcome', 'reason_code'),
                    row['target_ref'], ('audit_target', target[0], target[1], target[2]))
            if len(entries) > self.MAX_ROWS:
                raise ApiError(409, 'HISTORY_WINDOW_TOO_LARGE', '이 사건의 보존 이력 범위를 초과했습니다.')
            # Topological order preserves explicit parent causes, with stable UTC/ID ordering
            # among independent records. Client receipt timestamps are never used as ordering clocks.
            nodes = {entry[3]['event_id']: entry for entry in entries}
            by_id = {}
            for entry in entries:
                by_id.setdefault(entry[1], []).append(entry[3]['event_id'])
            pending, children, queue, ordered = {}, {}, [], []
            for entry in entries:
                rid, value = entry[3]['event_id'], entry[3]
                parent_id = value['parent_record_id']
                parent_table = {'executions': 'incidents', 'followups': 'incidents',
                    'notifications': 'executions', 'delivery_attempts': 'notifications',
                    'notification_receipts': 'notifications', 'notification_responses': 'notifications'}.get(entry[0])
                parent = parent_table + ':' + parent_id if parent_table and parent_id else None
                if entry[0] == 'audit_events' and len(by_id.get(parent_id, [])) == 1:
                    parent = by_id[parent_id][0]
                pending[rid] = int(parent in nodes and parent != rid)
                if pending[rid]:
                    children.setdefault(parent, []).append(rid)
                else:
                    heapq.heappush(queue, (datetime.fromisoformat(value['recorded_at_utc']), value['event_id'], rid))
            while queue:
                _, _, rid = heapq.heappop(queue)
                ordered.append(nodes[rid])
                for child in children.get(rid, []):
                    pending[child] -= 1
                    if not pending[child]:
                        value = nodes[child][3]
                        heapq.heappush(queue, (datetime.fromisoformat(value['recorded_at_utc']), value['event_id'], child))
            if len(ordered) != len(entries):
                raise ApiError(409, 'HISTORY_REFERENCE_CYCLE', '이력 참조 관계를 확인하세요.')
            return ordered
        return self.page(binding, cursor, limit, capture) | {'incident_id': iid, 'facility_id': facility, 'run_id': run_id}

    def progress(self, command):
        plans = []
        for plan in self.db.execute('''SELECT * FROM plans WHERE command_id=? AND facility_id=?
            AND run_id=? ORDER BY rowid''', (command['command_id'], command['facility_id'], command['run_id'])):
            steps = []
            raw_steps = load_steps(plan['steps_json'])
            if not raw_steps:
                raw_steps = [None]
            seen = set()
            for index, raw in enumerate(raw_steps):
                step = raw if isinstance(raw, dict) else {}
                sid = step.get('step_id')
                valid_id = isinstance(sid, str) and 0 < len(sid) <= 128 and sid not in seen
                if valid_id:
                    seen.add(sid)
                retained = 'execution_ids' in step or 'execution_id' in step
                refs = step.get('execution_ids', [])
                malformed_refs = not isinstance(refs, list)
                if malformed_refs:
                    refs = []
                if isinstance(step.get('execution_id'), str) and step['execution_id'] not in refs:
                    refs = refs + [step['execution_id']]
                invalid_refs = malformed_refs or any(not isinstance(ref, str) or not 0 < len(ref) <= 128 for ref in refs)
                if 'execution_id' in step and not isinstance(step['execution_id'], str):
                    invalid_refs = True
                refs = list(dict.fromkeys(ref for ref in refs if isinstance(ref, str) and 0 < len(ref) <= 128))
                attempts = []
                for ref in refs:
                    execution = self.db.execute('''SELECT * FROM executions WHERE execution_id=? AND plan_id=?
                        AND command_id=? AND facility_id=? AND run_id=?''',
                        (ref, plan['plan_id'], command['command_id'], command['facility_id'], command['run_id'])).fetchone()
                    # Explicit legacy execution_id is valid even when step_id was not retained.
                    mismatch = False
                    if execution is not None and valid_id:
                        payload = json.loads(execution['payload_json'])
                        mismatch = isinstance(payload, dict) and payload.get('step_id') not in (None, sid)
                    if execution is None or mismatch:
                        attempts.append(StepAttempt(execution_id=ref, linkage_status='unknown', reason_code='STEP_EXECUTION_REFERENCE_MISMATCH' if mismatch else 'EXECUTION_REFERENCE_UNAVAILABLE'))
                    else:
                        attempts.append(StepAttempt(execution_id=ref, linkage_status='linked',
                            execution=HistoryExecution(**projection(execution, EXECUTION_FIELDS))))
                if invalid_refs or any(attempt.linkage_status == 'unknown' for attempt in attempts):
                    status, reason = 'unknown', 'EXECUTION_REFERENCE_UNAVAILABLE'
                elif attempts:
                    status, reason = attempts[-1].execution.status, None
                elif not valid_id:
                    status, reason = 'unknown', 'LEGACY_STEP_REFERENCE_MISSING'
                elif not retained:
                    status, reason = 'unknown', 'EXECUTION_REFERENCES_NOT_RETAINED'
                else:
                    status = 'cancelled' if plan['status'] == 'cancelled' else 'pending'
                    reason = None
                if sid is not None and not valid_id:
                    status, reason = 'unknown', 'INVALID_STEP_ID'
                tool, zone = step.get('tool'), step.get('zone_id')
                steps.append(PlanStepProgress(index=index, step_id=sid if valid_id else None,
                    tool_name=tool if isinstance(tool, str) else None,
                    zone_id=zone if isinstance(zone, str) and 0 < len(zone) <= 128 else None,
                    status=status, reason_code=reason, attempts=attempts))
            plans.append(PlanProgress(plan_id=plan['plan_id'], status=plan['status'],
                                      resource_version=plan['resource_version'], steps=steps))
        return CommandProgress(as_of_utc=utc_now(), command=HistoryCommand(**projection(command, COMMAND_FIELDS)),
            plans=plans, preservation_limits=LIMITS + ['Only explicit execution references are linked; legacy gaps remain unknown.'])


def install_history_view_routes(app, authenticate, facility_check, settings):
    """Main integrates this additive installer in app.py; reads need no test control."""
    def service():
        runtime = app.state.runtime
        if not hasattr(runtime, 'history_views'):
            runtime.history_views = HistoryViews(runtime)
        return runtime, runtime.history_views

    async def listing(request, facility_id, table, run_id, cursor, limit):
        runtime, views = service()
        async with runtime.lock:
            session = authenticate(request, ['owner', 'test_operator'] if table == 'executions' else None)
            facility_check(facility_id)
            authority = views.scope(session, facility_id, run_id, operator=table == 'executions')
            return views.listing(table, session, facility_id, run_id, cursor, limit, authority)

    @app.get('/api/v1/facilities/{facility_id}/commands', response_model=CommandsHistory)
    async def commands(request: Request, facility_id: str, run_id: str | None = None,
                       cursor: str | None = None, limit: int = Query(50, ge=1, le=100)):
        return await listing(request, facility_id, 'commands', run_id, cursor, limit)

    @app.get('/api/v1/facilities/{facility_id}/executions', response_model=ExecutionsHistory)
    async def executions(request: Request, facility_id: str, run_id: str | None = None,
                         cursor: str | None = None, limit: int = Query(50, ge=1, le=100)):
        return await listing(request, facility_id, 'executions', run_id, cursor, limit)

    @app.get('/api/v1/incidents/{incident_id}/timeline', response_model=IncidentTimeline)
    async def timeline(request: Request, incident_id: str, cursor: str | None = None,
                       limit: int = Query(50, ge=1, le=100)):
        runtime, views = service()
        async with runtime.lock:
            session = authenticate(request, ['owner', 'test_operator'])
            incident = views.scoped('incidents', incident_id)
            facility_check(incident['facility_id'])
            authority = views.scope(session, incident['facility_id'], incident['run_id'], operator=True)
            return views.timeline(incident, authority, cursor, limit)

    @app.get('/api/v1/commands/{command_id}/progress', response_model=CommandProgress)
    async def progress(request: Request, command_id: str):
        runtime, views = service()
        async with runtime.lock:
            session = authenticate(request, ['owner', 'test_operator'])
            command = views.scoped('commands', command_id)
            facility_check(command['facility_id'])
            views.scope(session, command['facility_id'], command['run_id'], operator=True)
            return views.progress(command)
