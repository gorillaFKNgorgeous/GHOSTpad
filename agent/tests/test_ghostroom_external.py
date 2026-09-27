# SPDX-License-Identifier: GPL-2.0-or-later
"""External-participant GHOSTroom coverage: auto-sessions, idle close, Room.exchange
message/control routing, connectors, agent_setup secrets and the runtime RoomClient
side of the same flows. See agent/relay/store.py, room.py, router.py, server.py,
agent_secrets.py, claude_agent.py and agent/runtime/ghostroom.py.
"""
import json
import os
import stat
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'agent/relay'))
sys.path.insert(0, str(ROOT / 'agent/runtime'))
from store import LEGACY, OWNER, SESSION_IDLE_SECONDS, RelayFailure, Store  # noqa: E402
from room import Room, external_descriptors  # noqa: E402
from router import AgentAdapter, AgentRouter  # noqa: E402
from server import App  # noqa: E402
from agent_secrets import AgentSecrets  # noqa: E402
from claude_agent import ClaudeAdapter  # noqa: E402
import ghostroom  # noqa: E402

try:
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
    HAVE_JSONSCHEMA = True
except ImportError:
    HAVE_JSONSCHEMA = False

SCENE = 'c' * 32
BOOT = 'b' * 32
SCHEMAS = ROOT / 'ghostroom/protocol/schemas'
BASE_ID = 'https://schemas.ghostpad.invalid/ghostroom/v0/'


def hb(boot=BOOT, scene=SCENE):
    return {'boot_id': boot, 'scene_id': scene, 'blender_version': '5.2.0', 'native': {'foreground': True}}


def schema_validator(name):
    resources = [(json.loads(p.read_text())['$id'], Resource.from_contents(json.loads(p.read_text())))
                 for p in SCHEMAS.rglob('*.schema.json')]
    return Draft202012Validator({'$ref': BASE_ID + name}, registry=Registry().with_resources(resources))


class FakeAdapter(AgentAdapter):
    """A minimal embedded adapter: registering it must exempt it from auto-sessions."""

    agent_id = 'fake'
    provider = 'fake'
    display_name = 'Fake'
    participant_id = 'fake-embedded'
    setup_methods = ('api_key',)

    def __init__(self):
        self._key = None

    def probe(self):
        return {'availability': 'available', 'auth': 'signed_in', 'quota': 'ok'}

    def setup(self, op, value=None):
        if op == 'api_key':
            if not isinstance(value, str) or len(value) < 8:
                raise ValueError('invalid_api_key')
            self._key = value
            return 'saved'
        raise ValueError('setup_not_supported')

    def setup_state(self):
        return {'state': 'done' if self._key else 'idle', 'message': 'ok'}

    def run_turn(self, turn):
        return 'done'


def new_app(temp):
    return App(str(Path(temp) / 'db.sqlite3'), 'https://relay.example', 'ipad',
               'd' * 40, 'a' * 40, 'client', 'c' * 40, 'o' * 40, ['https://x.example/cb'])


class AutoSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.temp.name) / 'relay.sqlite3'))
        self.store.exchange('ipad', hb())

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_external_participant_first_job_auto_creates_running_session(self):
        job = self.store.submit('ipad', 'inspect_scene', {}, 'req-ext-1', SCENE)
        task_id = self.store.db.execute('SELECT task_id FROM jobs WHERE job_id=?', (job['job_id'],)).fetchone()[0]
        self.assertIsNotNone(task_id)
        task = self.store.task('ipad', task_id)
        self.assertEqual(task['state'], 'running')
        self.assertIn('working through GhostBlender', task['text'])
        self.assertEqual(job['task_id'] if 'task_id' in job else task_id, task_id)

    def test_embedded_participant_does_not_get_auto_session(self):
        router = AgentRouter(self.store, 'ipad', 'http://127.0.0.1:1')
        adapter = router.register(FakeAdapter())
        job = self.store.submit('ipad', 'inspect_scene', {}, 'req-emb-1', SCENE, adapter.participant_id)
        task_id = self.store.db.execute('SELECT task_id FROM jobs WHERE job_id=?', (job['job_id'],)).fetchone()[0]
        self.assertIsNone(task_id)

    def test_idle_session_closes_as_completed(self):
        job = self.store.submit('ipad', 'inspect_scene', {}, 'req-idle-1', SCENE)
        task_id = self.store.db.execute('SELECT task_id FROM jobs WHERE job_id=?', (job['job_id'],)).fetchone()[0]
        old = time.time() - SESSION_IDLE_SECONDS - 60
        self.store.db.execute('UPDATE jobs SET created=? WHERE job_id=?', (old, job['job_id']))
        self.store.db.execute('UPDATE chat_messages SET started=? WHERE device_id=? AND message_id=?',
                              (old, 'ipad', task_id))
        self.store.status('ipad')
        self.assertEqual(self.store.task('ipad', task_id)['state'], 'completed')


class RoomExchangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = new_app(self.temp.name)
        self.app.store.exchange('ipad', hb())
        self.app.store.touch_participant(LEGACY)

    def tearDown(self):
        self.app.store.db.close()
        self.temp.cleanup()

    def test_message_to_legacy_participant_is_queued(self):
        reply = self.app.room.exchange({'v': 1, 'messages': [{'id': 'e' * 32, 'text': 'Optimise it',
                                                              'agent_id': LEGACY}]})
        self.assertEqual(reply['ack_ids'], [{'id': 'e' * 32, 'delivery': 'queued'}])
        self.assertEqual(reply['problems'], [])

    def test_message_to_unknown_agent_is_a_problem(self):
        reply = self.app.room.exchange({'v': 1, 'messages': [{'id': 'f' * 32, 'text': 'hi',
                                                              'agent_id': 'no-such-agent'}]})
        self.assertEqual(reply['ack_ids'], [])
        self.assertEqual(len(reply['problems']), 1)
        self.assertEqual(reply['problems'][0]['error'], 'unknown_agent')

    def test_room_read_start_post_stop_reply_flow(self):
        self.app.room.exchange({'v': 1, 'messages': [{'id': 'e' * 32, 'text': 'Optimise it', 'agent_id': LEGACY}]})

        read = self.app.call('room_read', {})
        self.assertTrue(any(t['task_id'] == 'e' * 32 for t in read['inbox']))
        self.assertTrue(any(item['type'] == 'user' for item in read['conversation']))

        started = self.app.call('room_start', {'task_id': 'e' * 32})
        self.assertEqual(started['state'], 'running')

        job = self.app.store.submit('ipad', 'execute_python', {'code': '# work\nresult=1'}, 'req-flow-1', SCENE)
        self.assertEqual(self.app.store.db.execute(
            'SELECT task_id FROM jobs WHERE job_id=?', (job['job_id'],)).fetchone()[0], 'e' * 32)

        progress = self.app.call('room_post', {'text': 'Working now', 'kind': 'progress'})
        self.assertEqual(progress, {'task_id': 'e' * 32, 'posted': 'progress'})
        events = self.app.store.room_events('ipad', 0, 200)
        self.assertTrue(any(e['type'] == 'status' and e['text'] == 'Working now' for e in events))

        stop_reply = self.app.room.exchange({'v': 1, 'controls': [{'id': 'c1', 'action': 'stop',
                                                                    'task_id': 'e' * 32}]})
        self.assertEqual(stop_reply['control_acks'][0]['result'], 'stopping')

        with self.assertRaises(RelayFailure) as ctx:
            self.app.store.submit('ipad', 'execute_python', {'code': 'x=1'}, 'req-flow-2', SCENE)
        self.assertEqual(ctx.exception.failure['code'], 'stopped_by_user')

        read2 = self.app.call('room_read', {})
        self.assertTrue(read2['current_task']['stop_requested'])

        posted = self.app.call('room_post', {'text': 'Stopped as asked.'})
        self.assertEqual(posted['task_state'], 'finished')
        self.assertEqual(self.app.store.task('ipad', 'e' * 32)['state'], 'stopped')
        events = self.app.store.room_events('ipad', 0, 200)
        self.assertTrue(any(e['type'] == 'final' and e['text'] == 'Stopped as asked.' for e in events))

    def test_room_post_reply_without_running_task_is_standalone_final(self):
        posted = self.app.call('room_post', {'text': 'Standalone reply'})
        self.assertEqual(posted, {'task_id': None, 'posted': 'reply'})
        events = self.app.store.room_events('ipad', 0, 200)
        self.assertTrue(any(e['type'] == 'final' and e['text'] == 'Standalone reply' and e['task_id'] is None
                            for e in events))
        entries = self.app.store.read_ledger('ipad', 0, 200)['entries']
        self.assertTrue(any(e['category'] == 'response' and e['summary'] == 'Standalone reply' for e in entries))

    def test_room_start_with_title_creates_then_renames(self):
        created = self.app.call('room_start', {'title': 'First title'})
        self.assertIn('First title', created['text'])
        renamed = self.app.call('room_start', {'title': 'Second title'})
        self.assertEqual(renamed['task_id'], created['task_id'])
        self.assertIn('Second title', renamed['text'])


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = new_app(self.temp.name)
        self.app.store.exchange('ipad', hb())

    def tearDown(self):
        self.app.store.db.close()
        self.temp.cleanup()

    def test_connector_create_resolves_and_appears_then_revoke(self):
        reply = self.app.room.exchange({'v': 1, 'controls': [{'id': 'c1', 'action': 'connector_create',
                                                              'name': 'ChatGPT Desktop'}]})
        ack = reply['control_acks'][0]['result']
        self.assertTrue(ack['url'].startswith(self.app.origin + '/mcp/p/'))
        capability = ack['url'].rsplit('/', 1)[-1]

        participant = self.app.store.participant_for_capability(capability)
        self.assertEqual(participant, ack['participant_id'])
        self.assertTrue(any(p['participant_id'] == participant for p in self.app.store.external_participants()))

        self.app.store.touch_participant(participant)
        descriptors = external_descriptors(self.app.store)
        found = next(d for d in descriptors if d['agent_id'] == participant)
        self.assertEqual(found['provider'], 'external')

        revoke = self.app.room.exchange({'v': 1, 'controls': [{'id': 'c2', 'action': 'connector_revoke',
                                                               'participant_id': participant}]})
        self.assertEqual(revoke['control_acks'][0]['result'], 'revoked')
        self.assertFalse(any(p['participant_id'] == participant for p in self.app.store.external_participants()))

    def test_revoking_legacy_is_rejected(self):
        reply = self.app.room.exchange({'v': 1, 'controls': [{'id': 'c3', 'action': 'connector_revoke',
                                                              'participant_id': LEGACY}]})
        ack = reply['control_acks'][0]
        self.assertEqual(ack['result'], 'rejected')
        self.assertIn('error', ack)


class AgentSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.temp.name) / 'relay.sqlite3'))
        self.store.exchange('ipad', hb())
        self.router = AgentRouter(self.store, 'ipad', 'http://127.0.0.1:1')
        self.adapter = self.router.register(FakeAdapter())
        self.room = Room(self.store, 'ipad', self.router, public_origin='https://relay.example')
        self.secret = 'sk-' + 'x' * 40

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_setup_receives_op_and_secret(self):
        reply = self.room.exchange({'v': 1, 'controls': [{'id': 's1', 'action': 'agent_setup', 'agent_id': 'fake',
                                                          'op': 'api_key', 'secret': self.secret}]})
        self.assertEqual(reply['control_acks'][0]['result'], 'saved')
        self.assertEqual(self.adapter._key, self.secret)

    def test_unsupported_op_is_rejected(self):
        reply = self.room.exchange({'v': 1, 'controls': [{'id': 's2', 'action': 'agent_setup', 'agent_id': 'fake',
                                                          'op': 'sign_in'}]})
        ack = reply['control_acks'][0]
        self.assertEqual(ack['result'], 'rejected')
        self.assertIn('setup_not_supported', ack['error'])

    def test_setup_states_lists_methods(self):
        states = self.router.setup_states()
        self.assertEqual(states['fake']['methods'], ['api_key'])

    def test_ledger_summary_entry_never_contains_the_secret(self):
        self.room.exchange({'v': 1, 'controls': [{'id': 's3', 'action': 'agent_setup', 'agent_id': 'fake',
                                                  'op': 'api_key', 'secret': self.secret}]})
        entries = self.store.read_ledger('ipad', 0, 200)['entries']
        self.assertTrue(any(e['category'] == 'summary' and 'requested from GHOSTroom' in e['summary']
                            for e in entries))
        for entry in entries:
            self.assertNotIn(self.secret, json.dumps(entry))


class AgentSecretsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'secrets.json'
        self.secrets = AgentSecrets(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_set_get_delete_round_trip(self):
        self.assertIsNone(self.secrets.get('claude.api_key'))
        self.secrets.set('claude.api_key', 'sk-abc12345')
        self.assertEqual(self.secrets.get('claude.api_key'), 'sk-abc12345')
        self.secrets.set('claude.api_key', None)
        self.assertIsNone(self.secrets.get('claude.api_key'))

    def test_file_mode_is_owner_only(self):
        self.secrets.set('claude.api_key', 'sk-abc12345')
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_claude_adapter_prefers_stored_secret_over_env(self):
        adapter = ClaudeAdapter()
        adapter.secrets = self.secrets
        old_env = os.environ.get('ANTHROPIC_API_KEY')
        # Synthetic values assembled at runtime, so the secret scan never sees a key-shaped literal.
        env_key, stored_key = 'e' * 30, 's' * 30
        os.environ['ANTHROPIC_API_KEY'] = env_key
        try:
            self.secrets.set('claude.api_key', stored_key)
            self.assertEqual(adapter.api_key(), stored_key)
        finally:
            if old_env is None:
                os.environ.pop('ANTHROPIC_API_KEY', None)
            else:
                os.environ['ANTHROPIC_API_KEY'] = old_env

    def test_claude_adapter_setup_rejects_short_key(self):
        adapter = ClaudeAdapter()
        adapter.secrets = self.secrets
        with self.assertRaises(ValueError):
            adapter.setup('api_key', 'short')


class RuntimeRoomClientSecretTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def new_client(self):
        root = Path(self.temp.name) / 'device'
        root.mkdir(parents=True, exist_ok=True)

        class Native:
            def room_take(self):
                return []

            def room_update(self, encoded):
                pass

        return ghostroom.RoomClient(str(root), Native())

    def test_agent_setup_secret_reaches_payload_not_disk_then_ok(self):
        client = self.new_client()
        secret = 'sk-' + 'x' * 40
        client.handle({'type': 'agent_setup', 'agent_id': 'claude', 'op': 'api_key', 'secret': secret})

        payload = client.payload()
        self.assertTrue(any(c.get('secret') == secret for c in payload.get('controls', [])))

        client.save(force=True)
        on_disk = (Path(client.state_path)).read_text()
        self.assertNotIn(secret, on_disk)

        control_id = client.volatile_controls[0]['id']
        client.apply({'v': 1, 'control_acks': [{'id': control_id, 'result': 'Claude API key saved on the relay'}],
                     'setup': {'claude': {'state': 'done', 'methods': ['api_key'],
                                         'message': 'API key set'}}})
        agents = [{'id': 'claude', 'name': 'Claude', 'provider': 'claude', 'state': 'available',
                  'auth': 'signed_in'}]
        snapshot = ghostroom.setup_snapshot(client, agents)
        row = next(r for r in snapshot['agents'] if r['id'] == 'claude')
        self.assertEqual(row['result']['state'], 'ok')

        on_disk_after = Path(client.state_path).read_text()
        self.assertNotIn(secret, on_disk_after)

    def test_connector_create_ack_shows_url_not_persisted_to_disk(self):
        client = self.new_client()
        client.handle({'type': 'connector_create', 'name': 'ChatGPT'})
        control_id = client.volatile_controls[0]['id']
        url = 'https://relay.example/mcp/p/' + 'a' * 40
        client.apply({'v': 1, 'control_acks': [{'id': control_id,
                                                'result': {'participant_id': 'chatgpt-abcdef', 'name': 'ChatGPT',
                                                          'url': url}}],
                     'connectors': [{'participant_id': 'chatgpt-abcdef', 'name': 'ChatGPT', 'client': None,
                                    'last_seen': None, 'created': time.time()}]})
        self.assertEqual(client.revealed['chatgpt-abcdef']['url'], url)

        snapshot = ghostroom.setup_snapshot(client, [])
        connector = next(c for c in snapshot['connectors'] if c['participant_id'] == 'chatgpt-abcdef')
        self.assertEqual(connector['url'], url)

        client.save(force=True)
        on_disk = Path(client.state_path).read_text()
        self.assertNotIn(url, on_disk)


@unittest.skipUnless(HAVE_JSONSCHEMA, 'jsonschema is not installed')
class SchemaValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = new_app(self.temp.name)
        self.app.store.exchange('ipad', hb())

    def tearDown(self):
        self.app.store.db.close()
        self.temp.cleanup()

    def test_ledger_entries_from_external_flows_match_schema(self):
        self.app.store.touch_participant(LEGACY)
        self.app.room.exchange({'v': 1, 'messages': [{'id': 'e' * 32, 'text': 'Optimise it', 'agent_id': LEGACY}]})
        self.app.call('room_start', {'task_id': 'e' * 32})
        job = self.app.store.submit('ipad', 'execute_python', {'code': '# work\nresult=1'}, 'req-sc-1', SCENE)
        self.app.store.exchange('ipad', hb())
        self.app.store.exchange('ipad', hb(), {'job_id': job['job_id'], 'boot_id': BOOT,
                                               'result': {'ok': True, 'value': {'result': 1}, 'scene_id': SCENE}})
        self.app.call('room_post', {'text': 'Done.'})
        self.app.room.exchange({'v': 1, 'controls': [{'id': 'c1', 'action': 'connector_create', 'name': 'Codex'}]})

        entries = self.app.store.read_ledger('ipad', 0, 200)['entries']
        self.assertTrue(entries)
        check = schema_validator('ledger.schema.json')
        problems = [(e['summary'], err.message) for e in entries for err in check.iter_errors(e)]
        self.assertFalse(problems, problems[:5])

    def test_router_describe_matches_agent_schema(self):
        router = AgentRouter(self.app.store, 'ipad', 'http://127.0.0.1:1')
        router.register(FakeAdapter())
        router.probe_now()
        self.app.store.touch_participant(LEGACY)
        check = schema_validator('agent.schema.json')
        for descriptor in router.describe():
            problems = list(check.iter_errors(descriptor))
            self.assertFalse(problems, (descriptor, [p.message for p in problems]))


if __name__ == '__main__':
    unittest.main()
