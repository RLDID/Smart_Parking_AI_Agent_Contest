import asyncio
from copy import deepcopy

import pytest

from agent.manual import ManualS1
from backend.auth import ApiError, Auth
from backend.runtime import Runtime
from contracts.environment_controls import DeviceFaultInput
from simulator.world import initial_world


@pytest.mark.parametrize('entry', ['run', 'manual', 'device_fault'])
def test_logout_while_waiting_for_writer_lock_prevents_mutation(tmp_path, entry):
    r = Runtime(tmp_path / 'session.sqlite3')
    try:
        auth = Auth(r.store)
        token, session = auth.login('demo-operator', 'parking-demo-only', 'test')
        r.world = initial_world(1)
        r.store.commit(r.world, r.event(r.world))
        before = deepcopy(r.world)
        changes = r.store.db.total_changes

        def authenticate():
            if auth.lookup(token) is not session:
                raise ApiError(401, 'UNAUTHENTICATED', 'session revoked')

        async def exercise():
            await r.lock.acquire()
            if entry == 'run':
                call = r.mutate(session, 'create', 'create', {'seed': 2}, authenticate=authenticate)
            elif entry == 'manual':
                call = r.manual_s1a(session, ManualS1(run_id=r.world['run_id'], action='notify'),
                                   'manual', authenticate=authenticate)
            else:
                call = r.environment_control(session, r.world['run_id'],
                    DeviceFaultInput(expected_state_version=0, channel='visual', failed=True),
                    'fault', 'device_fault', authenticate=authenticate)
            pending = asyncio.create_task(call)
            await asyncio.sleep(0)
            auth.sessions.pop(token)
            r.lock.release()
            with pytest.raises(ApiError) as caught:
                await pending
            assert caught.value.code == 'UNAUTHENTICATED'

        asyncio.run(exercise())
        assert r.world == before and r.store.db.total_changes == changes
    finally:
        r.store.close()
