import json
import shutil

import pytest

from backend.auth import ApiError
from backend.runtime import Runtime
from backend.storage import Store
from backend.knowledge import Knowledge, DEFAULT_MANIFEST
from simulator.world import FACILITY


def test_original_approved_bundle_can_upgrade_once(tmp_path):
    runtime = Runtime(tmp_path / 'policy.sqlite3')
    try:
        assert runtime.knowledge.current_policy(FACILITY).policy_version == 2
        runtime.ensure_autonomous_policy()
        runtime.ensure_autonomous_policy()
        assert runtime.knowledge.current_policy(FACILITY).policy_version == 3
    finally:
        runtime.store.close()


@pytest.mark.parametrize('change', [
    {'conflict': True}, {'roles': ['owner']}, {'status': 'withdrawn'},
])
def test_modified_or_withdrawn_manual_is_not_restored_by_upgrade(tmp_path, change):
    runtime = Runtime(tmp_path / 'policy.sqlite3')
    try:
        runtime.knowledge.document_access(FACILITY, 'manual-parking-order', 'v1', **change)
        with pytest.raises(ApiError) as caught:
            runtime.ensure_autonomous_policy()
        assert caught.value.code == 'KNOWLEDGE_CHANGED'
        assert runtime.knowledge.current_policy(FACILITY).policy_version == 2
        assert not runtime.store.db.execute('SELECT 1 FROM policies WHERE policy_version=3').fetchone()
    finally:
        runtime.store.close()


def test_modified_structured_policy_requires_explicit_bundle_upgrade(tmp_path):
    manual_dir = tmp_path / 'manuals'
    shutil.copytree(DEFAULT_MANIFEST.parent, manual_dir)
    custom = manual_dir / 'manifest.json'
    data = json.loads(custom.read_text(encoding='utf-8'))
    data['policy']['execution_rules']['allowed_tools'].remove('notify_vehicle_user')
    custom.write_text(json.dumps(data), encoding='utf-8')
    store = Store(tmp_path / 'policy.sqlite3')
    try:
        Knowledge(store, tmp_path / 'knowledge/index', source_root=manual_dir).activate(custom)
    finally:
        store.close()
    runtime = Runtime(tmp_path / 'policy.sqlite3')
    try:
        with pytest.raises(ApiError) as caught:
            runtime.ensure_autonomous_policy()
        assert caught.value.code == 'KNOWLEDGE_CHANGED'
        assert runtime.knowledge.current_policy(FACILITY).policy_version == 2
    finally:
        runtime.store.close()
