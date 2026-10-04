"""Provider-free API history, authority, snapshot and transaction regressions."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json

import pytest
from httpx import ASGITransport, AsyncClient

from backend.app import Settings, create_app
from backend.auth import ApiError
from backend.business import encoded
from backend.history_views import HistoryViews, install_history_view_routes
from contracts.autonomous import AutonomousControl
from simulator.world import FACILITY, initial_world, advance

STAMP = '2026-10-04T00:00:00.000Z'


@asynccontextmanager
async def harness(tmp_path, fixture='s1a-foundation-v1'):
    app = create_app(Settings(database=tmp_path / 'history.sqlite3', background_ticks=False,
                             test_control=True, origins=('http://testserver',)))
    def authenticate(request, roles=None, mutation=False):
        return app.state.auth.require(request.cookies.get('parking_session'), roles)
    def facility_check(facility):
        if facility != FACILITY:
            raise ApiError(404, 'NOT_FOUND', 'facility')
    install_history_view_routes(app, authenticate, facility_check, None)
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        runtime.world = initial_world(3, fixture)
        for _ in range(60):
            advance(runtime.world)
        runtime.store.commit(runtime.world, runtime.event(runtime.world))
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
            yield app, runtime, client
        assert not runtime.autonomous.active
        assert runtime.autonomous.enabled is None
        assert runtime.queries.live_models is None


def login(app, client, username='demo-owner'):
    token, session = app.state.auth.login(username, 'parking-demo-only', 'test')
    client.cookies.set('parking_session', token)
    return token, session


def command(r, cid, requester='demo-owner', run_id=None, status='pending'):
    r.business.insert('commands', command_id=cid, facility_id=FACILITY,
        run_id=run_id or r.world['run_id'], requester_id=requester,
        request_text='private free text', purpose='operational_goal', aggregate_status=status,
        normalized_goal_json=encoded({'kind': 'closing', 'confirmed': True}),
        created_at=STAMP, updated_at=STAMP)


def plan(r, pid, cid, steps, status='active'):
    r.business.insert('plans', plan_id=pid, facility_id=FACILITY, run_id=r.world['run_id'],
        command_id=cid, steps_json=encoded(steps), model_ref='mock',
        policy_version=r.knowledge.current_policy(FACILITY).policy_version,
        budget_json='{}', status=status, created_at=STAMP, updated_at=STAMP)


def execution(r, eid, *, cid=None, pid=None, iid=None, status='succeeded', payload=None, sim=None):
    r.business.insert('executions', execution_id=eid, facility_id=FACILITY, run_id=r.world['run_id'],
        command_id=cid, plan_id=pid, incident_id=iid, tool_name='play_announcement',
        target_ref='announcement-a', requester_ref='demo-owner', idempotency_key=eid,
        payload_hash='test', payload_json=encoded(payload or {'private': 'not returned'}),
        result_json=encoded({'private': 'not returned'}), status=status,
        based_on_state_version=0, policy_version=r.knowledge.current_policy(FACILITY).policy_version,
        mode='synthetic_demo', applied_sim_time_ms=sim, created_at=STAMP, updated_at=STAMP)


def incident(r, iid):
    r.business.insert('incidents', incident_id=iid, facility_id=FACILITY, run_id=r.world['run_id'],
        status='resolved', primary_object_id='obj-car-02', dedup_key=iid,
        policy_version=r.knowledge.current_policy(FACILITY).policy_version,
        reason_summary='private reason', created_at=STAMP, updated_at=STAMP)


def test_pagination_keeps_snapshot_and_historical_runs_read_only(tmp_path):
    async def exercise():
        async with harness(tmp_path) as (app, r, client):
            login(app, client)
            old_run = r.world['run_id']
            for index in range(3):
                command(r, f'c-{index}')
            execution(r, 'e-first', cid='c-0')
            r.store.db.commit()
            r.world = initial_world(4)
            r.store.commit(r.world, r.event(r.world))
            # History ignores current-run recovery/failure and test-control execution guards.
            r.world['recovery_required'], r.failure = True, True
            app.state.runtime.ensure_run = lambda _: pytest.fail('History must not call current-run guard')
            url = f'/api/v1/facilities/{FACILITY}/commands'
            params = {'run_id': old_run, 'limit': 1}
            first = (await client.get(url, params=params)).json()
            assert [item['command_id'] for item in first['items']] == ['c-0']
            r.store.db.execute("UPDATE commands SET aggregate_status='partial',resource_version=2 WHERE command_id='c-1'")
            command(r, 'c-new', run_id=old_run)
            r.store.db.commit()
            second = (await client.get(url, params=params | {'cursor': first['next_cursor']})).json()
            assert second['items'][0]['aggregate_status'] == 'pending'
            assert second['as_of_utc'] == first['as_of_utc']
            third = (await client.get(url, params=params | {'cursor': second['next_cursor']})).json()
            assert third['items'][0]['command_id'] == 'c-2' and third['next_cursor'] is None
            fresh = (await client.get(url, params={'run_id': old_run})).json()
            assert len(fresh['items']) == 4 and fresh['items'][1]['aggregate_status'] == 'partial'
            assert 'private' not in json.dumps(fresh)
            records = await client.get(f'/api/v1/facilities/{FACILITY}/executions', params={'run_id': old_run})
            assert records.status_code == 200 and records.json()['items'][0]['execution_id'] == 'e-first'
            assert 'private' not in records.text and 'payload_json' not in records.text
            missing = await client.get(url, params={'run_id': 'not-associated'})
            assert missing.status_code == 404
            assert r.store.db.execute('SELECT count(*) FROM commands').fetchone()[0] == 4
    asyncio.run(exercise())


def test_driver_scope_current_row_filter_and_empty_page_progress(tmp_path):
    async def exercise():
        async with harness(tmp_path) as (app, r, client):
            login(app, client, 'demo-driver')
            for index in range(3):
                command(r, f'own-{index}', 'demo-driver')
            command(r, 'other', 'demo-driver-2')
            r.store.db.commit()
            url = f'/api/v1/facilities/{FACILITY}/commands'
            first = (await client.get(url, params={'limit': 1})).json()
            r.store.db.execute("UPDATE commands SET requester_id='demo-driver-2' WHERE command_id='own-1'")
            r.store.db.commit()
            second = (await client.get(url, params={'limit': 1, 'cursor': first['next_cursor']})).json()
            assert second['items'] == [] and second['next_cursor']
            third = (await client.get(url, params={'limit': 1, 'cursor': second['next_cursor']})).json()
            assert third['items'][0]['command_id'] == 'own-2' and third['next_cursor'] is None
            assert (await client.get(f'/api/v1/facilities/{FACILITY}/executions')).status_code == 403
            assert (await client.get('/api/v1/commands/other/progress')).status_code == 403
            assert (await client.get('/api/v1/incidents/missing/timeline')).status_code == 403
            # Request ownership is user_id, not an assumed username equality.
            r.store.db.execute("UPDATE users SET username='renamed-driver' WHERE user_id='demo-driver'")
            r.store.db.commit()
            login(app, client, 'renamed-driver')
            fresh = (await client.get(url)).json()
            assert [item['command_id'] for item in fresh['items']] == ['own-0', 'own-2']
    asyncio.run(exercise())


def test_cursor_binding_tampering_expiry_capacity_and_live_revocation(tmp_path):
    async def exercise():
        async with harness(tmp_path) as (app, r, client):
            token, session = login(app, client)
            for index in range(3):
                command(r, f'c-{index}')
            r.store.db.commit()
            url = f'/api/v1/facilities/{FACILITY}/commands'
            first = (await client.get(url, params={'limit': 1})).json()
            cursor = first['next_cursor']
            changed = await client.get(url, params={'limit': 2, 'cursor': cursor})
            assert changed.status_code == 409
            assert (await client.get(url, params={'limit': 1, 'cursor': cursor + '!'})).status_code == 422
            login(app, client, 'demo-operator')
            assert (await client.get(url, params={'limit': 1, 'cursor': cursor})).status_code == 409
            client.cookies.set('parking_session', token)
            views = r.history_views
            views.clock = lambda: next(iter(views.snapshots.values())).expires + 1
            assert (await client.get(url, params={'limit': 1, 'cursor': cursor})).status_code == 409
            views.clock = __import__('time').monotonic
            views.MAX_SNAPSHOTS = 1
            old = (await client.get(url, params={'limit': 1})).json()['next_cursor']
            await client.get(url, params={'limit': 1})
            assert len(views.snapshots) == 1
            assert (await client.get(url, params={'limit': 1, 'cursor': old})).status_code == 409
            active = (await client.get(url, params={'limit': 1})).json()['next_cursor']
            # Restart invalidates the signature; never silently creates a fresh snapshot.
            r.history_views = HistoryViews(r)
            assert (await client.get(url, params={'limit': 1, 'cursor': active})).status_code == 409
            r.store.db.execute("UPDATE memberships SET revoked_at=? WHERE user_id='demo-owner'", (STAMP,))
            r.store.db.commit()
            assert (await client.get(url)).status_code == 401
    asyncio.run(exercise())


def test_waiting_read_rechecks_authentication_inside_lock(tmp_path):
    async def exercise():
        async with harness(tmp_path) as (app, r, client):
            token, session = login(app, client)
            command(r, 'secret-command')
            r.store.db.commit()
            await r.lock.acquire()
            task = asyncio.create_task(client.get(f'/api/v1/facilities/{FACILITY}/commands'))
            await asyncio.sleep(0)
            app.state.auth.sessions.pop(token)
            r.lock.release()
            response = await task
            assert response.status_code == 401 and 'secret-command' not in response.text
    asyncio.run(exercise())


def test_facility_run_membership_is_required_and_page_limits_are_bounded(tmp_path):
    async def exercise():
        async with harness(tmp_path) as (app, r, client):
            login(app, client)
            command(r, 'c')
            r.store.db.commit()
            url = f'/api/v1/facilities/{FACILITY}/commands'
            assert (await client.get(url, params={'limit': 0})).status_code == 422
            assert (await client.get(url, params={'limit': 101})).status_code == 422
            assert (await client.get('/api/v1/facilities/foreign/commands')).status_code == 404
            r.history_views = HistoryViews(r)
            r.history_views.MAX_ROWS = 0
            assert (await client.get(url)).status_code == 409
            # Actual run relationship, rather than a world ID match, controls history.
            r.store.db.execute('PRAGMA foreign_keys=OFF')
            r.store.db.execute('DELETE FROM run_facilities WHERE run_id=?', (r.world['run_id'],))
            r.store.db.commit()
            assert (await client.get(url, params={'run_id': r.world['run_id']})).status_code == 404
    asyncio.run(exercise())


def test_timeline_explicit_references_causal_order_and_distinct_clocks(tmp_path):
    async def exercise():
        async with harness(tmp_path) as (app, r, client):
            login(app, client)
            incident(r, 'i-1')
            incident(r, 'i-other')
            execution(r, 'e-1', iid='i-1', sim=600)
            b = r.business
            b.insert('notifications', notification_id='n-1', facility_id=FACILITY, run_id=r.world['run_id'],
                execution_id='e-1', incident_id='i-1', context_key='i-1', recipient_user_id='demo-owner',
                purpose='owner_report', contact_sequence=1, delivery_status='client_received',
                message_template='private', message_json='{"secret":"omit"}', mode='synthetic_demo',
                created_at=STAMP, updated_at=STAMP)
            b.insert('delivery_attempts', attempt_id='a-1', facility_id=FACILITY, run_id=r.world['run_id'],
                notification_id='n-1', attempt_number=1, channel='web_inbox', status='accepted',
                requested_at=STAMP, completed_at=STAMP, created_at=STAMP, updated_at=STAMP)
            b.insert('notification_receipts', receipt_id='r-1', facility_id=FACILITY, run_id=r.world['run_id'],
                notification_id='n-1', user_id='demo-owner', client_request_id='receipt',
                received_at='2020-01-01T00:00:00Z', recorded_at=STAMP)
            b.insert('notification_responses', response_id='s-1', facility_id=FACILITY, run_id=r.world['run_id'],
                notification_id='n-1', user_id='demo-owner', client_request_id='response',
                response='will_move', text='private reply', responded_at=STAMP)
            b.insert('followups', followup_id='f-1', facility_id=FACILITY, run_id=r.world['run_id'],
                incident_id='i-1', requester_ref='demo-owner', clock='sim', due_sim_time_ms=700,
                condition_json='{}', status='scheduled', max_attempts=3,
                policy_version=r.knowledge.current_policy(FACILITY).policy_version,
                created_at=STAMP, updated_at=STAMP)
            b.audit('demo-owner', 'incident.checked', 'i-1', r.world['run_id'])
            b.audit('demo-owner', 'unrelated', 'i-other', r.world['run_id'])
            r.store.db.commit()
            url = '/api/v1/incidents/i-1/timeline'
            first = (await client.get(url, params={'limit': 2})).json()
            items, cursor = first['items'], first['next_cursor']
            b.changed('notifications', 'notification_id', 'n-1', r.world['run_id'], delivery_status='unknown')
            r.store.db.commit()
            while cursor:
                page = (await client.get(url, params={'limit': 2, 'cursor': cursor})).json()
                assert page['as_of_utc'] == first['as_of_utc']
                items += page['items']
                cursor = page['next_cursor']
            ids = [item['record_id'] for item in items]
            assert ids.index('e-1') < ids.index('n-1') < ids.index('r-1')
            assert ids.index('n-1') < ids.index('s-1')
            by_id = {item['record_id']: item for item in items}
            assert by_id['e-1']['sim_time_ms'] == 600
            assert by_id['r-1']['sim_time_ms'] is None
            assert by_id['r-1']['recorded_at_utc'] == STAMP
            assert by_id['r-1']['details']['received_at'].startswith('2020-')
            assert by_id['s-1']['details']['response'] == 'will_move'
            assert by_id['n-1']['details']['delivery_status'] == 'client_received'
            assert by_id['f-1']['details']['due_sim_time_ms'] == 700
            assert by_id['f-1']['sim_time_ms'] is None
            serialized = json.dumps(items)
            assert 'private' not in serialized and 'i-other' not in serialized and 'unrelated' not in serialized
            assert 'resolved_at' not in serialized and 'movement' not in serialized
            assert len({item['event_id'] for item in items}) == len(items) == 8
    asyncio.run(exercise())


def test_progress_explicit_attempts_legacy_gaps_replans_and_missing_refs(tmp_path):
    async def exercise():
        async with harness(tmp_path) as (app, r, client):
            login(app, client)
            command(r, 'c', status='partial')
            plan(r, 'old-plan', 'c', [{'tool': 'play_announcement'}], status='cancelled')
            plan(r, 'new-plan', 'c', [
                {'step_id': 'step-a', 'tool': 'play_announcement', 'zone_id': 'announcement-a', 'execution_ids': ['held', 'ok']},
                {'step_id': 'step-b', 'tool': 'play_announcement', 'zone_id': 'announcement-b', 'execution_ids': ['unknown']},
                {'step_id': 'step-c', 'tool': 'set_entry_policy', 'execution_ids': []},
                {'step_id': 'step-gap', 'tool': 'set_entry_policy', 'execution_ids': ['missing']},
                {'step_id': 'wrong', 'tool': 'play_announcement', 'execution_ids': ['ok']},
                {'step_id': 'not-retained', 'tool': 'play_announcement'},
                {'step_id': 'bad-refs', 'tool': 'play_announcement', 'execution_ids': {}}])
            for eid, status, sid in [('held', 'held', 'step-a'), ('ok', 'succeeded', 'step-a'), ('unknown', 'unknown', 'step-b')]:
                execution(r, eid, cid='c', pid='new-plan', status=status, payload={'step_id': sid})
            # Same tool/time/zone is insufficient to attach this unrelated execution.
            execution(r, 'unlinked', cid='c', pid='old-plan')
            r.store.db.commit()
            response = await client.get('/api/v1/commands/c/progress')
            assert response.status_code == 200
            body = response.json()
            assert body['command']['aggregate_status'] == 'partial'
            assert body['plans'][0]['steps'][0]['reason_code'] == 'LEGACY_STEP_REFERENCE_MISSING'
            steps = body['plans'][1]['steps']
            assert [attempt['execution_id'] for attempt in steps[0]['attempts']] == ['held', 'ok']
            assert [step['status'] for step in steps] == ['succeeded', 'unknown', 'pending', 'unknown', 'unknown', 'unknown', 'unknown']
            assert steps[5]['reason_code'] == 'EXECUTION_REFERENCES_NOT_RETAINED'
            assert steps[4]['attempts'][0]['reason_code'] == 'STEP_EXECUTION_REFERENCE_MISMATCH'
            assert 'unlinked' not in response.text and 'private' not in response.text
            before = r.store.db.total_changes
            await client.get('/api/v1/commands/c/progress')
            assert r.store.db.total_changes == before
    asyncio.run(exercise())


def test_step_link_commit_rollback_idempotency_and_duplicate_guard(tmp_path, monkeypatch):
    async def exercise():
        async with harness(tmp_path, 's3-closing-v1') as (app, r, client):
            token, session = login(app, client)
            r.ensure_autonomous_policy()
            command(r, 'c')
            r.store.db.commit()
            body = AutonomousControl(run_id=r.world['run_id'], action='process', mode='mock', scenario='s3', command_id='c')
            row = r.business.scoped('commands', 'c', 'command_id')
            # Generate stable IDs through the real S3 plan creator.
            created = r.autonomous._command_plan(body, row, {'kind': 'closing', 'confirmed': False})
            pid = created['plan_id']
            steps = json.loads(created['steps_json'])
            assert len({step['step_id'] for step in steps}) == 3
            r.business.changed('plans', 'plan_id', pid, r.world['run_id'], status='active')
            r.business.changed('commands', 'command_id', 'c', r.world['run_id'],
                normalized_goal_json=encoded({'kind': 'closing', 'confirmed': True}))
            r.store.db.commit()
            monkeypatch.setattr(r.knowledge, 'validate_evidence', lambda *args, **kwargs: None)
            args = {'run_id': r.world['run_id'], 'command_id': 'c', 'plan_id': pid,
                'action': 'play_announcement', 'zone_id': 'announcement-a', 'message_id': 'closing_notice',
                'step_id': steps[0]['step_id'], 'knowledge_evidence': None, 'key': 'same-key'}
            checkpoint = deepcopy(r.world)
            original_commit = r.store.commit
            def fail_commit(*_args, **_kwargs):
                raise RuntimeError('injected checkpoint failure')
            monkeypatch.setattr(r.store, 'commit', fail_commit)
            with pytest.raises(RuntimeError, match='injected checkpoint failure'):
                await r.devices.execute(session, **args)
            assert r.store.db.execute('SELECT count(*) FROM executions').fetchone()[0] == 0
            assert json.loads(r.business.scoped('plans', pid, 'plan_id')['steps_json']) == steps
            assert r.world == checkpoint
            monkeypatch.setattr(r.store, 'commit', original_commit)
            accepted = await r.devices.execute(session, **args)
            replay = await r.devices.execute(session, **args)
            assert accepted == replay
            linked = json.loads(r.business.scoped('plans', pid, 'plan_id')['steps_json'])
            assert linked[0]['execution_ids'] == [accepted['execution_id']]
            assert linked[0]['execution_id'] == accepted['execution_id']
            assert accepted['result']['step_id'] == steps[0]['step_id']
            with pytest.raises(ApiError) as error:
                await r.devices.execute(session, **(args | {'key': 'different-key'}))
            assert error.value.code == 'STEP_ALREADY_EXECUTED'
            with pytest.raises(ApiError) as wrong_step:
                await r.devices.execute(session, **(args | {'key': 'wrong-step', 'step_id': steps[1]['step_id']}))
            assert wrong_step.value.code == 'PLAN_STEP_CHANGED'
            assert r.store.db.execute('SELECT count(*) FROM executions').fetchone()[0] == 1
    asyncio.run(exercise())


def test_timeline_current_target_scope_filters_its_audit_after_snapshot(tmp_path):
    async def exercise():
        async with harness(tmp_path) as (app, r, client):
            login(app, client)
            incident(r, 'i')
            incident(r, 'other-i')
            execution(r, 'e', iid='i')
            r.business.audit('demo-owner', 'execution.checked', 'e', r.world['run_id'])
            r.store.db.commit()
            first = (await client.get('/api/v1/incidents/i/timeline', params={'limit': 1})).json()
            assert first['items'][0]['record_id'] == 'i'
            r.store.db.execute("UPDATE executions SET incident_id='other-i' WHERE execution_id='e'")
            r.store.db.commit()
            second = (await client.get('/api/v1/incidents/i/timeline', params={'limit': 1, 'cursor': first['next_cursor']})).json()
            assert second['items'] == [] and second['next_cursor']
            third = (await client.get('/api/v1/incidents/i/timeline', params={'limit': 1, 'cursor': second['next_cursor']})).json()
            assert third['items'] == [] and third['next_cursor'] is None
    asyncio.run(exercise())


def test_timeline_cross_table_id_collision_preserves_records_without_guessing_audit_parent(tmp_path):
    async def exercise():
        async with harness(tmp_path) as (app, r, client):
            login(app, client)
            incident(r, 'same-id')
            execution(r, 'same-id', iid='same-id')
            r.business.audit('demo-owner', 'ambiguous.target', 'same-id', r.world['run_id'])
            r.store.db.commit()
            response = await client.get('/api/v1/incidents/same-id/timeline')
            assert response.status_code == 200
            items = response.json()['items']
            assert [item['event_id'] for item in items] == ['incidents:same-id', 'executions:same-id']
            assert 'ambiguous.target' not in response.text
    asyncio.run(exercise())


def test_timeline_orders_equivalent_utc_formats_by_instant_not_text(tmp_path):
    async def exercise():
        async with harness(tmp_path) as (app, r, client):
            login(app, client)
            incident(r, 'i')
            for fid, stamp in [('z-earlier', '2026-10-04T00:00:01Z'),
                               ('a-later', '2026-10-04T00:00:01.000001Z')]:
                r.business.insert('followups', followup_id=fid, facility_id=FACILITY, run_id=r.world['run_id'],
                    incident_id='i', requester_ref='demo-owner', clock='sim', due_sim_time_ms=700,
                    condition_json='{}', status='scheduled', max_attempts=3,
                    policy_version=r.knowledge.current_policy(FACILITY).policy_version,
                    created_at=stamp, updated_at=stamp)
            r.store.db.commit()
            response = await client.get('/api/v1/incidents/i/timeline')
            assert response.status_code == 200
            assert [item['record_id'] for item in response.json()['items']] == ['i', 'z-earlier', 'a-later']
    asyncio.run(exercise())
