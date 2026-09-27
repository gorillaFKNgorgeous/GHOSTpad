# SPDX-License-Identifier: GPL-2.0-or-later
"""The relay's real ledger output must satisfy ghostroom/protocol/schemas/ledger.schema.json.

Drives agent/relay/store.py through a multi-participant session (leases,
conflicts, completed/failed/expired/uncertain/late jobs, script writes) and
validates every entry it wrote.
"""
import json
from pathlib import Path
import sys
import tempfile
import time

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'agent/relay'))
import store as relay  # noqa: E402

SCHEMAS = ROOT / 'ghostroom/protocol/schemas'
BASE = 'https://schemas.ghostpad.invalid/ghostroom/v0/'
BOOT, SCENE = 'b' * 32, 'c' * 32


def validator(name):
    resources = [(json.loads(p.read_text())['$id'], Resource.from_contents(json.loads(p.read_text())))
                 for p in SCHEMAS.rglob('*.schema.json')]
    return Draft202012Validator({'$ref': BASE + name}, registry=Registry().with_resources(resources))


def heartbeat(scene=SCENE):
    return {'boot_id': BOOT, 'scene_id': scene, 'blender_version': '5.2.0', 'native': {'foreground': True}}


def run_job(store, job, result):
    store.exchange('ipad', heartbeat())
    store.exchange('ipad', heartbeat(), {'job_id': job['job_id'], 'boot_id': BOOT, 'result': result})


def expect_failure(call, code):
    try:
        call()
    except relay.RelayFailure as exc:
        assert exc.failure['code'] == code, exc.failure
        return exc.failure
    raise AssertionError(f'expected {code}')


def session(store):
    store.exchange('ipad', heartbeat())
    store.register_participant('claude', 'agent', 'Claude', provider='claude')
    py = {'code': 'result = 1'}

    # Legacy caller, no lease_id: implicit per-job lease, then a completed result.
    job = store.submit('ipad', 'execute_python', py, 'legacy-run-001', SCENE)
    run_job(store, job, {'ok': True, 'value': {'stdout': '', 'result': 1}, 'scene_id': SCENE})

    # Claude cannot take the lease while the legacy caller's job is still queued.
    pending = store.submit('ipad', 'execute_python', py, 'legacy-run-002', SCENE)
    expect_failure(lambda: store.acquire_lease('ipad', 'claude', SCENE), 'lease_conflict')
    store.cancel('ipad', pending['job_id'])

    # Explicit lease: others are rejected, inspection stays concurrent.
    lease = store.acquire_lease('ipad', 'claude', SCENE, 60)
    store.acquire_lease('ipad', 'claude', SCENE, 90)  # renew
    expect_failure(lambda: store.submit('ipad', 'execute_python', py, 'legacy-run-003', SCENE), 'lease_conflict')
    expect_failure(lambda: store.submit('ipad', 'execute_python', py, 'claude-run-000', SCENE,
                                        'claude', 'not-a-real-lease'), 'lease_invalid')
    look = store.submit('ipad', 'inspect_scene', {}, 'legacy-look-001', SCENE)
    run_job(store, look, {'ok': True, 'value': {'objects': []}, 'scene_id': SCENE})
    failed = store.submit('ipad', 'execute_python', py, 'claude-run-001', SCENE, 'claude', lease['lease_id'])
    run_job(store, failed, {'ok': False, 'error': 'RuntimeError: TimeoutError: Python time limit reached; '
                                                  'partial edits may exist', 'traceback': '...'})
    store.release_lease('ipad', 'claude', lease['lease_id'])

    # Persistent script workspace: separate lock, persistent-code risk entries.
    write = store.submit('ipad', 'write_script', {'name': 'tool.py', 'code': 'x = 1\n', 'expected_sha256': None},
                         'claude-write-001', SCENE, 'claude')
    expect_failure(lambda: store.submit('ipad', 'write_script', {'name': 'tool.py', 'code': 'x = 2\n',
                                                                 'expected_sha256': 'a' * 64},
                                        'legacy-write-001', SCENE), 'script_workspace_busy')
    run_job(store, write, {'ok': True, 'value': {'name': 'tool.py', 'sha256': relay.sha256_text('x = 1\n'),
                                                 'executed': False}, 'scene_id': SCENE})

    # Issued, then no result before expiry: uncertain. A late result supersedes it.
    lost = store.submit('ipad', 'execute_python', py, 'legacy-run-004', SCENE)
    store.exchange('ipad', heartbeat())
    store.db.execute('UPDATE jobs SET expires=? WHERE job_id=?', (time.time() - 1, lost['job_id']))
    store.status('ipad')
    store.exchange('ipad', heartbeat(), {'job_id': lost['job_id'], 'boot_id': BOOT,
                                         'result': {'ok': True, 'value': {}, 'scene_id': SCENE}})

    # Queued past expiry: expired, never ran. Scene change drops queued work.
    stale = store.submit('ipad', 'execute_python', py, 'legacy-run-005', SCENE)
    store.db.execute('UPDATE jobs SET expires=? WHERE job_id=?', (time.time() - 1, stale['job_id']))
    store.status('ipad')
    dropped = store.submit('ipad', 'execute_python', py, 'legacy-run-006', SCENE)
    store.exchange('ipad', heartbeat('d' * 32))
    assert store.result('ipad', dropped['job_id'])['state'] == 'expired'

    # An explicit lease left to run out.
    store.exchange('ipad', heartbeat())
    held = store.acquire_lease('ipad', 'claude', SCENE, 10)
    store.db.execute('UPDATE leases SET expires=? WHERE lease_id=?', (time.time() - 1, held['lease_id']))
    store.status('ipad')


def test_every_relay_ledger_entry_matches_the_schema():
    with tempfile.TemporaryDirectory() as temp:
        store = relay.Store(str(Path(temp) / 'relay.sqlite3'))
        session(store)
        entries = store.read_ledger('ipad', 0, 200)['entries']
        store.db.close()
    check = validator('ledger.schema.json')
    problems = [(e['summary'], err.message) for e in entries for err in check.iter_errors(e)]
    assert not problems, problems[:5]

    outcomes = {(e['category'], e.get('outcome')) for e in entries}
    for expected in [('mutation', 'requested'), ('mutation', 'dispatched'), ('mutation', 'completed'),
                     ('mutation', 'failed'), ('mutation', 'uncertain'), ('mutation', 'expired'),
                     ('mutation', 'cancelled'), ('inspection', 'completed'),
                     ('persistent_code', 'requested'), ('persistent_code', 'completed')]:
        assert expected in outcomes, expected
    events = {e['lease']['event'] for e in entries if e['category'] == 'lease'}
    assert events >= {'acquired', 'renewed', 'released', 'expired', 'rejected'}
    codes = {e['failure']['code'] for e in entries if 'failure' in e}
    assert codes >= {'lease_conflict', 'lease_invalid', 'script_workspace_busy', 'tool_timeout',
                     'interrupted_after_possible_mutation', 'scene_changed'}
    assert any('supersedes' in e for e in entries)
