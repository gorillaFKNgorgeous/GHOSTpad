"""Participant identity (B1.5), shared workspace ledger (B2) and edit leases (B3).

Standard library only: this suite also runs in the IPA build workflow.
"""
import http.client
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'agent/relay'))
from server import App, Server  # noqa: E402
import store as relay  # noqa: E402

PY = {'code': 'result = 1'}


def heartbeat(scene='scene_one', boot='boot_one'):
    return {'boot_id': boot, 'scene_id': scene, 'blender_version': '5.2.0', 'native': {'foreground': True}}


class RelayCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'relay.sqlite3')
        self.store = relay.Store(self.path)
        self.store.exchange('ipad', heartbeat())
        self.claude = self.store.register_participant('claude', 'agent', 'Claude', provider='claude')

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def ledger(self, **match):
        entries = self.store.read_ledger('ipad', 0, 200)['entries']
        return [e for e in entries if all(e.get(k) == v for k, v in match.items())]

    def job_entries(self, job_id):
        return [e for e in self.ledger() if any(ev.get('job_id') == job_id for ev in e.get('evidence', []))]

    def deliver(self, job):
        reply = self.store.exchange('ipad', heartbeat())
        self.assertEqual(reply['job']['job_id'], job['job_id'])
        return reply['job']

    def complete(self, job, result):
        return self.store.exchange('ipad', heartbeat(), {'job_id': job['job_id'], 'boot_id': 'boot_one',
                                                         'result': result})

    def failure_code(self, call):
        with self.assertRaises(relay.RelayFailure) as caught:
            call()
        return caught.exception.failure['code']


class IdentityTests(RelayCase):
    def test_capability_maps_to_participant_and_is_stored_only_as_a_digest(self):
        self.assertEqual(self.store.participant_for_capability(self.claude), 'claude')
        self.assertIsNone(self.store.participant_for_capability(self.claude[:-1] + 'x'))
        stored = self.store.db.execute('SELECT capability_sha256 FROM participants WHERE participant_id=?',
                                       ('claude',)).fetchone()[0]
        self.assertEqual(stored, relay.sha256_text(self.claude))
        self.assertNotIn(self.claude, Path(self.path).read_bytes().decode('latin-1'))

    def test_rotation_and_revocation(self):
        rotated = self.store.rotate_capability('claude')
        self.assertIsNone(self.store.participant_for_capability(self.claude))
        self.assertEqual(self.store.participant_for_capability(rotated), 'claude')
        self.store.revoke_participant('claude')
        self.assertIsNone(self.store.participant_for_capability(rotated))
        with self.assertRaises(ValueError):
            self.store.revoke_participant(relay.LEGACY)

    def test_embedded_worker_capability_is_fresh_each_start(self):
        first = self.store.ensure_participant_capability('codex-embedded', 'agent', 'Codex', 'codex')
        second = self.store.ensure_participant_capability('codex-embedded', 'agent', 'Codex', 'codex')
        self.assertIsNone(self.store.participant_for_capability(first))
        self.assertEqual(self.store.participant_for_capability(second), 'codex-embedded')

    def test_reserved_and_invalid_ids(self):
        with self.assertRaises(ValueError):
            self.store.register_participant(relay.LEGACY, 'agent', 'x')
        with self.assertRaises(ValueError):
            self.store.register_participant('Bad Id', 'agent', 'x')
        with self.assertRaises(ValueError):
            self.store.register_participant('claude', 'agent', 'again')

    def test_idempotency_key_is_not_shared_across_participants(self):
        self.store.submit('ipad', 'inspect_scene', {}, 'shared-key-01', 'scene_one', 'claude')
        with self.assertRaisesRegex(ValueError, 'another_participant'):
            self.store.submit('ipad', 'inspect_scene', {}, 'shared-key-01', 'scene_one')


class HttpIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = App(str(Path(self.temp.name) / 'state.sqlite'), 'https://relay.example', 'ipad',
                       'd' * 40, 'a' * 40, 'client', 'c' * 40, 'o' * 40, ['https://client.example/callback'])
        self.server = Server(('127.0.0.1', 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.app.store.exchange('ipad', heartbeat())
        self.capability = self.app.store.register_participant('claude', 'agent', 'Claude', provider='claude')

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.app.store.db.close()
        self.temp.cleanup()

    def post(self, path, body, token=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        headers = {'Host': 'relay.example', 'Accept': 'application/json, text/event-stream',
                   'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        conn.request('POST', path, json.dumps(body), headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response.status, json.loads(raw) if raw else None

    def tool(self, path, name, arguments, token=None):
        status, reply = self.post(path, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                         'params': {'name': name, 'arguments': arguments}}, token)
        self.assertEqual(status, 200)
        return reply['result']

    def initialize(self, path, client_name, token=None):
        return self.post(path, {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                                'params': {'protocolVersion': '2025-11-25', 'capabilities': {},
                                           'clientInfo': {'name': client_name, 'version': '1'}}}, token)

    def test_identity_comes_from_the_capability_not_client_info(self):
        path = '/mcp/p/' + self.capability
        # A client claiming to be someone else changes only its unverified label.
        self.assertEqual(self.initialize(path, 'legacy-unattributed')[0], 200)
        result = self.tool(path, 'inspect_scene', {'request_id': 'claude-look-01', 'scene_id': 'scene_one'})
        self.assertEqual(json.loads(result['content'][0]['text'])['participant_id'], 'claude')
        row = next(p for p in self.app.store.participants() if p['participant_id'] == 'claude')
        self.assertEqual(row['client_label_unverified'], 'legacy-unattributed 1')

    def test_legacy_route_is_unchanged_and_unattributed(self):
        self.assertEqual(self.initialize('/mcp', 'claude', token='a' * 40)[0], 200)
        result = self.tool('/mcp', 'inspect_scene', {'request_id': 'legacy-look-01', 'scene_id': 'scene_one'},
                           token='a' * 40)
        self.assertEqual(json.loads(result['content'][0]['text'])['participant_id'], relay.LEGACY)
        self.assertEqual(self.post('/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})[0], 401)

    def test_unknown_and_revoked_capabilities_are_not_found(self):
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}
        self.assertEqual(self.post('/mcp/p/' + 'x' * 43, request)[0], 404)
        self.app.store.revoke_participant('claude')
        self.assertEqual(self.post('/mcp/p/' + self.capability, request)[0], 404)

    def test_lease_conflict_reaches_the_client_as_a_typed_failure(self):
        path = '/mcp/p/' + self.capability
        lease = json.loads(self.tool(path, 'acquire_lease', {'scene_id': 'scene_one'})['content'][0]['text'])
        self.assertEqual(lease['holder'], 'claude')
        result = self.tool('/mcp', 'execute_python', {'request_id': 'legacy-edit-01', 'scene_id': 'scene_one',
                                                      'code': 'result = 1'}, token='a' * 40)
        self.assertTrue(result['isError'])
        self.assertEqual(json.loads(result['content'][0]['text'])['failure']['code'], 'lease_conflict')
        ledger = json.loads(self.tool(path, 'read_ledger', {})['content'][0]['text'])
        self.assertIn('lease_conflict', [e.get('failure', {}).get('code') for e in ledger['entries']])


class LedgerTests(RelayCase):
    def test_success_is_recorded_only_from_a_device_result(self):
        job = self.store.submit('ipad', 'execute_python', PY, 'edit-001', 'scene_one')
        self.deliver(job)
        # Dispatch, heartbeats without a result, and connection loss are not success.
        self.store.exchange('ipad', heartbeat())
        outcomes = [e['outcome'] for e in self.job_entries(job['job_id'])]
        self.assertEqual(outcomes, ['requested', 'dispatched'])
        self.complete(job, {'ok': True, 'value': {}, 'scene_id': 'scene_one'})
        self.assertEqual([e['outcome'] for e in self.job_entries(job['job_id'])],
                         ['requested', 'dispatched', 'completed'])

    def test_failed_python_keeps_possible_partial_edits(self):
        job = self.store.submit('ipad', 'execute_python', PY, 'edit-002', 'scene_one')
        self.deliver(job)
        self.complete(job, {'ok': False, 'error': 'RuntimeError: KeyError: Cube', 'traceback': ''})
        entry = self.job_entries(job['job_id'])[-1]
        self.assertEqual(entry['outcome'], 'failed')
        self.assertTrue(entry['failure']['mutation_possible'])
        self.assertEqual(entry['failure']['retry'], 'after_inspect')

    def test_rejected_before_running_is_known_not_to_have_mutated(self):
        job = self.store.submit('ipad', 'execute_python', PY, 'edit-003', 'scene_one')
        self.deliver(job)
        self.complete(job, {'ok': False, 'error': 'scene_or_session_changed; inspect the current scene again'})
        failure = self.job_entries(job['job_id'])[-1]['failure']
        self.assertEqual((failure['code'], failure['mutation_possible']), ('scene_changed', False))

    def test_issued_then_silent_is_interrupted_after_possible_mutation(self):
        job = self.store.submit('ipad', 'execute_python', PY, 'edit-004', 'scene_one')
        self.deliver(job)
        self.store.db.execute('UPDATE jobs SET expires=? WHERE job_id=?', (time.time() - 1, job['job_id']))
        self.store.status('ipad')
        uncertain = self.job_entries(job['job_id'])[-1]
        self.assertEqual(uncertain['outcome'], 'uncertain')
        self.assertEqual(uncertain['failure']['code'], 'interrupted_after_possible_mutation')
        # A late device result is recorded as new evidence that supersedes it.
        self.complete(job, {'ok': True, 'value': {}, 'scene_id': 'scene_one'})
        late = self.job_entries(job['job_id'])[-1]
        self.assertEqual((late['outcome'], late['supersedes']), ('completed', uncertain['entry_id']))

    def test_duplicate_delivery_reports_unknown_outcome(self):
        job = self.store.submit('ipad', 'execute_python', PY, 'edit-005', 'scene_one')
        self.deliver(job)
        self.complete(job, {'ok': False, 'error': 'duplicate_command_not_reexecuted', 'record': {}})
        entry = self.job_entries(job['job_id'])[-1]
        self.assertEqual((entry['outcome'], entry['failure']['code']),
                         ('uncertain', 'interrupted_after_possible_mutation'))

    def test_queued_expiry_and_cancel_never_ran(self):
        expired = self.store.submit('ipad', 'execute_python', PY, 'edit-006', 'scene_one')
        self.store.db.execute('UPDATE jobs SET expires=? WHERE job_id=?', (time.time() - 1, expired['job_id']))
        self.store.status('ipad')
        entry = self.job_entries(expired['job_id'])[-1]
        self.assertEqual((entry['outcome'], entry['failure']['mutation_possible']), ('expired', False))
        cancelled = self.store.submit('ipad', 'execute_python', PY, 'edit-007', 'scene_one')
        self.store.cancel('ipad', cancelled['job_id'])
        self.assertEqual(self.job_entries(cancelled['job_id'])[-1]['outcome'], 'cancelled')

    def test_only_the_submitter_can_cancel(self):
        job = self.store.submit('ipad', 'execute_python', PY, 'edit-008', 'scene_one')
        with self.assertRaisesRegex(ValueError, 'another_participant'):
            self.store.cancel('ipad', job['job_id'], 'claude')

    def test_ledger_write_and_state_change_commit_together(self):
        def broken(*args, **kwargs):
            raise RuntimeError('ledger unavailable')
        original = self.store._ledger
        self.store._ledger = broken
        try:
            with self.assertRaises(RuntimeError):
                self.store.submit('ipad', 'execute_python', PY, 'edit-009', 'scene_one')
        finally:
            self.store._ledger = original
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM jobs WHERE request_id='edit-009'").fetchone())
        self.assertIsNone(self.store.db.execute('SELECT 1 FROM leases').fetchone())

        job = self.store.submit('ipad', 'execute_python', PY, 'edit-010', 'scene_one')
        self.deliver(job)
        self.store._ledger = broken
        try:
            with self.assertRaises(RuntimeError):
                self.complete(job, {'ok': True, 'value': {}, 'scene_id': 'scene_one'})
        finally:
            self.store._ledger = original
        self.assertEqual(self.store.result('ipad', job['job_id'])['state'], 'issued')

    def test_ledger_is_append_only_and_survives_job_retention(self):
        job = self.store.submit('ipad', 'inspect_scene', {}, 'look-001', 'scene_one')
        self.deliver(job)
        self.complete(job, {'ok': True, 'value': {}, 'scene_id': 'scene_one'})
        count = len(self.ledger())
        self.store.db.execute('UPDATE jobs SET created=? WHERE job_id=?', (time.time() - 8 * 86400, job['job_id']))
        self.store.status('ipad')
        self.assertIsNone(self.store.db.execute('SELECT 1 FROM jobs WHERE job_id=?', (job['job_id'],)).fetchone())
        self.assertEqual(len(self.ledger()), count)
        self.assertNotIn('result', json.dumps(self.ledger()).replace('result_sha256', ''))


class LeaseTests(RelayCase):
    def test_legacy_mutation_takes_an_implicit_lease_for_exactly_that_job(self):
        job = self.store.submit('ipad', 'execute_python', PY, 'edit-101', 'scene_one')
        lease = self.store.lease('ipad', job['lease_id'])
        self.assertTrue(lease['implicit'])
        self.assertEqual((lease['holder'], lease['job_id']), (relay.LEGACY, job['job_id']))
        job_row = self.store.db.execute('SELECT expires FROM jobs WHERE job_id=?', (job['job_id'],)).fetchone()
        lease_row = self.store.db.execute('SELECT expires FROM leases WHERE lease_id=?', (job['lease_id'],)).fetchone()
        self.assertLessEqual(lease_row[0], job_row[0] + 0.01)
        self.deliver(job)
        self.complete(job, {'ok': True, 'value': {}, 'scene_id': 'scene_one'})
        self.assertEqual(self.store.lease('ipad', job['lease_id'])['state'], 'released')
        second = self.store.submit('ipad', 'execute_python', PY, 'edit-102', 'scene_one')
        self.assertNotEqual(second['lease_id'], job['lease_id'])

    def test_other_participant_is_rejected_while_scope_is_held(self):
        self.store.submit('ipad', 'execute_python', PY, 'edit-103', 'scene_one')
        self.assertEqual(self.failure_code(
            lambda: self.store.submit('ipad', 'execute_python', PY, 'claude-103', 'scene_one', 'claude')),
            'lease_conflict')
        self.assertEqual(self.failure_code(lambda: self.store.acquire_lease('ipad', 'claude', 'scene_one')),
                         'lease_conflict')
        rejected = [e for e in self.ledger(category='lease') if e['lease']['event'] == 'rejected']
        self.assertEqual(rejected[0]['lease']['requested_by'], 'claude')

    def test_inspection_stays_concurrent(self):
        lease = self.store.acquire_lease('ipad', 'claude', 'scene_one')
        look = self.store.submit('ipad', 'inspect_scene', {}, 'look-101', 'scene_one')
        self.assertIsNone(look['lease_id'])
        self.store.release_lease('ipad', 'claude', lease['lease_id'])

    def test_explicit_lease_is_used_and_validated(self):
        lease = self.store.acquire_lease('ipad', 'claude', 'scene_one', 60)
        job = self.store.submit('ipad', 'execute_python', PY, 'claude-104', 'scene_one', 'claude', lease['lease_id'])
        self.assertEqual(job['lease_id'], lease['lease_id'])
        implicit = self.store.submit('ipad', 'execute_python', PY, 'claude-105', 'scene_one', 'claude')
        self.assertEqual(implicit['lease_id'], lease['lease_id'])  # own explicit lease covers it
        self.assertEqual(self.failure_code(
            lambda: self.store.submit('ipad', 'execute_python', PY, 'edit-106', 'scene_one',
                                      relay.LEGACY, lease['lease_id'])), 'lease_invalid')
        with self.assertRaisesRegex(ValueError, 'only_applies'):
            self.store.submit('ipad', 'inspect_scene', {}, 'look-106', 'scene_one', 'claude', lease['lease_id'])

    def test_lease_id_never_reaches_the_device(self):
        lease = self.store.acquire_lease('ipad', 'claude', 'scene_one')
        app = App.__new__(App)
        app.store, app.device_id = self.store, 'ipad'
        app.call('execute_python', {'request_id': 'claude-107', 'scene_id': 'scene_one', 'code': 'result = 1',
                                    'lease_id': lease['lease_id']}, 'claude')
        delivered = self.store.exchange('ipad', heartbeat())['job']
        self.assertEqual(delivered['arguments'], {'code': 'result = 1'})

    def test_explicit_lease_ends_on_scene_change_and_expiry(self):
        lease = self.store.acquire_lease('ipad', 'claude', 'scene_one')
        self.store.exchange('ipad', heartbeat('scene_two'))
        self.assertEqual(self.store.lease('ipad', lease['lease_id'])['state'], 'expired')
        other = self.store.acquire_lease('ipad', 'claude', 'scene_two', 10)
        self.store.db.execute('UPDATE leases SET expires=? WHERE lease_id=?', (time.time() - 1, other['lease_id']))
        self.assertEqual(self.store.status('ipad')['leases'], [])
        self.assertEqual(self.store.lease('ipad', other['lease_id'])['state'], 'expired')

    def test_status_shows_who_holds_authority(self):
        lease = self.store.acquire_lease('ipad', 'claude', 'scene_one')
        self.assertEqual([(l['holder'], l['lease_id']) for l in self.store.status('ipad')['leases']],
                         [('claude', lease['lease_id'])])


class ScriptWorkspaceTests(RelayCase):
    def write(self, code, expected, key, participant=relay.LEGACY):
        return self.store.submit('ipad', 'write_script', {'name': 'tool.py', 'code': code,
                                                          'expected_sha256': expected}, key, 'scene_one', participant)

    def test_write_script_ignores_the_scene_lease_but_is_serialized(self):
        self.store.acquire_lease('ipad', 'claude', 'scene_one')
        first = self.write('x = 1\n', None, 'write-001')  # scene lease held by claude: not a conflict
        self.assertEqual(self.failure_code(lambda: self.write('x = 2\n', None, 'write-002', 'claude')),
                         'script_workspace_busy')
        self.deliver(first)
        self.complete(first, {'ok': True, 'value': {'name': 'tool.py', 'sha256': relay.sha256_text('x = 1\n'),
                                                    'executed': False}, 'scene_id': 'scene_one'})
        self.write('x = 2\n', relay.sha256_text('x = 1\n'), 'write-003', 'claude')

    def test_every_write_is_a_recorded_persistent_code_risk(self):
        previous = relay.sha256_text('x = 1\n')
        job = self.write('x = 2\n', previous, 'write-004')
        self.deliver(job)
        self.complete(job, {'ok': True, 'value': {'name': 'tool.py', 'sha256': relay.sha256_text('x = 2\n'),
                                                  'executed': False}, 'scene_id': 'scene_one'})
        entries = self.job_entries(job['job_id'])
        self.assertEqual([e['outcome'] for e in entries], ['requested', 'dispatched', 'completed'])
        for entry in entries:
            code = entry['persistent_code']
            self.assertEqual(entry['category'], 'persistent_code')
            self.assertEqual(entry['origin']['id'], relay.LEGACY)
            self.assertTrue(code['risk'])
            self.assertEqual((code['script'], code['previous_sha256'], code['replaces_existing']),
                             ('tool.py', previous, True))
            self.assertEqual(code['resulting_sha256'], relay.sha256_text('x = 2\n'))
            self.assertFalse(code['loaded_by_bridge_at_startup'])
            self.assertEqual(code['device_auto_load'], 'none')
            self.assertEqual((code['request_id'], code['job_id']), ('write-004', job['job_id']))
        self.assertTrue(entries[-1]['persistent_code']['confirmed_by_device'])
        self.assertNotIn('x = 2', json.dumps(entries))

    def test_invalid_previous_digest_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'expected_sha256'):
            self.write('x = 1\n', 'not-a-digest', 'write-005')


class MigrationTests(unittest.TestCase):
    def test_existing_relay_database_is_upgraded_in_place(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / 'old.sqlite3')
            db = sqlite3.connect(path)
            db.executescript('''
                CREATE TABLE jobs (job_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    digest TEXT NOT NULL, operation TEXT NOT NULL, arguments TEXT NOT NULL,
                    boot_id TEXT NOT NULL, scene_id TEXT NOT NULL, state TEXT NOT NULL,
                    created REAL NOT NULL, expires REAL NOT NULL, result TEXT, UNIQUE(device_id, request_id));''')
            now = time.time()
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       ('f' * 32, 'ipad', 'old-request', 'd' * 64, 'inspect_scene', '{}', 'boot_one',
                        'scene_one', 'completed', now, now + 90, None))
            db.commit()
            db.close()
            store = relay.Store(path)
            try:
                self.assertEqual(store.result('ipad', 'f' * 32)['participant_id'], relay.LEGACY)
            finally:
                store.db.close()


if __name__ == '__main__':
    unittest.main()
