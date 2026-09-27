# SPDX-License-Identifier: GPL-2.0-or-later
"""GHOSTroom relay-side coverage: store task methods, Room.exchange, AgentRouter,
capture evidence, notes, cursor/reset semantics and the HTTP device exchange.
"""
import base64
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'agent/relay'))
from store import LEGACY, RelayFailure, Store  # noqa: E402
from room import Room  # noqa: E402
import room as room_module  # noqa: E402
from router import AgentAdapter, AgentRouter, classify  # noqa: E402
from server import App, Server  # noqa: E402

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


def heartbeat(boot=BOOT, scene=SCENE):
    return {'boot_id': boot, 'scene_id': scene, 'blender_version': '5.2.0', 'native': {'foreground': True}}


def wait_until(condition, timeout=3.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return condition()


def schema_validator(name):
    resources = [(json.loads(p.read_text())['$id'], Resource.from_contents(json.loads(p.read_text())))
                 for p in SCHEMAS.rglob('*.schema.json')]
    return Draft202012Validator({'$ref': BASE_ID + name}, registry=Registry().with_resources(resources))


class RecordingAdapter(AgentAdapter):
    """Runs one no-op turn and records which participant its jobs were submitted under."""

    def __init__(self, agent_id, reply='ok'):
        self.agent_id = agent_id
        self.participant_id = f'{agent_id}-embedded'
        self.display_name = agent_id.title()
        self.provider = agent_id
        self.reply = reply
        self.jobs = []
        self.ran = threading.Event()

    def probe(self):
        return {'availability': 'available', 'auth': 'signed_in', 'quota': 'ok', 'model': 'test-model'}

    def run_turn(self, turn):
        job = self.store.submit(self.device_id, 'inspect_scene', {}, f'req-{self.agent_id}-1', SCENE,
                                 self.participant_id)
        self.jobs.append(job)
        self.ran.set()
        return self.reply


class BlockingAdapter(AgentAdapter):
    agent_id = 'codex'
    participant_id = 'codex-embedded'
    display_name = 'Codex'
    provider = 'codex'
    can_steer = False

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.interrupted = threading.Event()

    def probe(self):
        return {'availability': 'available', 'auth': 'signed_in', 'quota': 'ok'}

    def run_turn(self, turn):
        self.started.set()
        self.release.wait(5)
        return 'done'

    def interrupt(self):
        self.interrupted.set()


class SteerAdapter(BlockingAdapter):
    can_steer = True

    def __init__(self):
        super().__init__()
        self.steer_texts = []

    def steer(self, text):
        self.steer_texts.append(text)


class NoSteerAdapter(BlockingAdapter):
    can_steer = False


class FailingAdapter(AgentAdapter):
    agent_id = 'codex'
    participant_id = 'codex-embedded'
    display_name = 'Codex'
    provider = 'codex'

    def probe(self):
        return {'availability': 'available', 'auth': 'signed_in', 'quota': 'ok'}

    def run_turn(self, turn):
        raise RuntimeError('429 rate limit exceeded')


class GhostroomRelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'relay.sqlite3')
        self.store = Store(self.path)
        self.store.exchange('ipad', heartbeat())
        self.router = None

    def tearDown(self):
        if self.router is not None:
            self.router.close()
        self.store.db.close()
        self.temp.cleanup()

    def make_router(self, poll_interval=0.01):
        self.router = AgentRouter(self.store, 'ipad', 'http://127.0.0.1:1', poll_interval=poll_interval)
        return self.router

    # ---------------------------------------------------------------- 1. room_submit

    def test_room_submit_queues_task_emits_events_and_ledger(self):
        message_id = 'a' * 32
        result = self.store.room_submit('ipad', message_id, 'Build a car', 'codex')
        self.assertEqual(result, 'queued')

        events = self.store.room_events('ipad', 0)
        kinds = [e['type'] for e in events]
        self.assertIn('user', kinds)
        self.assertIn('task', kinds)

        task = self.store.task('ipad', message_id)
        self.assertEqual(task['state'], 'queued')

        ledger = self.store.read_ledger('ipad', 0, 50)['entries']
        self.assertTrue(any(e['category'] == 'instruction' for e in ledger))

        self.assertEqual(self.store.room_submit('ipad', message_id, 'Build a car', 'codex'), 'duplicate')
        with self.assertRaises(ValueError):
            self.store.room_submit('ipad', message_id, 'Build a different car', 'codex')

    # ---------------------------------------------------------------- 2. routing

    def test_router_routes_to_requested_agent_and_default_for_legacy(self):
        router = self.make_router()
        codex = RecordingAdapter('codex')
        claude = RecordingAdapter('claude')
        router.register(codex)
        router.register(claude)

        message_id = 'b' * 32
        self.store.room_submit('ipad', message_id, 'Only claude should do this', 'claude')
        router.start()
        self.assertTrue(wait_until(lambda: claude.ran.is_set()))
        wait_until(lambda: self.store.task('ipad', message_id)['state'] == 'completed')
        self.assertFalse(codex.ran.is_set())
        self.assertEqual(claude.jobs[0]['participant_id'], 'claude-embedded')

        legacy_id = 'c' * 32
        self.store.chat_exchange('ipad', {'cursor': 0, 'messages': [{'id': legacy_id, 'text': 'legacy hello'}]})
        legacy_task = self.store.task('ipad', legacy_id)
        self.assertEqual(legacy_task['agent_id'], 'codex')  # first-registered adapter is the default

    # ---------------------------------------------------------------- 3. stop

    def test_stop_interrupts_running_turn_cancels_jobs_and_blocks_further_submits(self):
        router = self.make_router()
        adapter = BlockingAdapter()
        router.register(adapter)
        room = Room(self.store, 'ipad', router)

        message_id = 'd' * 32
        self.store.room_submit('ipad', message_id, 'Build a house', 'codex')
        router.start()
        self.assertTrue(wait_until(lambda: adapter.started.is_set()))

        job = self.store.submit('ipad', 'execute_python', {'code': 'result=1'}, 'req-stop-1', SCENE,
                                 adapter.participant_id)
        self.assertEqual(self.store.result('ipad', job['job_id'])['state'], 'queued')

        reply = room.exchange({'v': 1, 'controls': [{'id': 'ctl1', 'action': 'stop', 'task_id': message_id}]})
        self.assertEqual(reply['control_acks'][0]['result'], 'stopping')
        self.assertTrue(wait_until(lambda: adapter.interrupted.is_set()))

        self.assertEqual(self.store.result('ipad', job['job_id'])['state'], 'cancelled')
        ledger = self.store.read_ledger('ipad', 0, 200)['entries']
        cancelled = [e for e in ledger if e.get('outcome') == 'cancelled']
        self.assertTrue(cancelled)

        failure = None
        try:
            self.store.submit('ipad', 'execute_python', {'code': 'result=2'}, 'req-stop-2', SCENE,
                               adapter.participant_id)
        except RelayFailure as exc:
            failure = exc.failure
        self.assertIsNotNone(failure)
        self.assertEqual(failure['code'], 'stopped_by_user')

        adapter.release.set()
        self.assertTrue(wait_until(lambda: self.store.task('ipad', message_id)['state'] == 'stopped'))

    # ---------------------------------------------------------------- 4. read-only

    def test_read_only_mode_and_role_block_mutation_but_allow_inspection(self):
        message_id = 'e' * 32
        self.store.room_submit('ipad', message_id, 'Explain the scene', 'codex', mode='explain')
        task = self.store.chat_claim('ipad', 'codex', 'codex-embedded')
        self.assertEqual(task['task_id'], message_id)
        self.assertTrue(task['read_only'])

        failure = None
        try:
            self.store.submit('ipad', 'execute_python', {'code': 'result=1'}, 'req-ro-1', SCENE, 'codex-embedded')
        except RelayFailure as exc:
            failure = exc.failure
        self.assertEqual(failure['code'], 'read_only_role')

        # inspect_scene is accepted for a read-only task.
        job = self.store.submit('ipad', 'inspect_scene', {}, 'req-ro-2', SCENE, 'codex-embedded')
        self.assertEqual(self.store.result('ipad', job['job_id'])['state'], 'queued')
        self.store.chat_complete('ipad', message_id, 'Explained.')

        message_id2 = 'f' * 32
        self.store.room_submit('ipad', message_id2, 'Review this work', 'codex', role='reviewer')
        self.store.chat_claim('ipad', 'codex', 'codex-embedded')
        failure = None
        try:
            self.store.submit('ipad', 'execute_python', {'code': 'result=1'}, 'req-ro-3', SCENE, 'codex-embedded')
        except RelayFailure as exc:
            failure = exc.failure
        self.assertEqual(failure['code'], 'read_only_role')

    # ---------------------------------------------------------------- 5. steering

    def test_steering_delivers_into_running_turn_or_queues_when_unsupported(self):
        router = self.make_router()
        adapter = SteerAdapter()
        router.register(adapter)
        room = Room(self.store, 'ipad', router)

        first_id = 'a1' * 16
        self.store.room_submit('ipad', first_id, 'Build a car', 'codex')
        router.start()
        self.assertTrue(wait_until(lambda: adapter.started.is_set()))

        second_id = 'a2' * 16
        reply = room.exchange({'v': 1, 'messages': [{'id': second_id, 'text': 'Make it red', 'agent_id': 'codex'}]})
        self.assertEqual(reply['ack_ids'][0]['delivery'], 'steer')

        self.assertTrue(wait_until(lambda: bool(adapter.steer_texts), timeout=3.0))
        self.assertIn('Make it red', adapter.steer_texts[0])
        adapter.release.set()
        wait_until(lambda: self.store.task('ipad', first_id)['state'] == 'completed')
        router.close()
        self.router = None

    def test_steering_queues_a_separate_task_when_agent_cannot_steer(self):
        router = self.make_router()
        adapter = NoSteerAdapter()
        router.register(adapter)
        room = Room(self.store, 'ipad', router)

        first_id = 'b1' * 16
        self.store.room_submit('ipad', first_id, 'Build a car', 'codex')
        router.start()
        self.assertTrue(wait_until(lambda: adapter.started.is_set()))

        second_id = 'b2' * 16
        reply = room.exchange({'v': 1, 'messages': [{'id': second_id, 'text': 'Also add wheels', 'agent_id': 'codex'}]})
        self.assertEqual(reply['ack_ids'][0]['delivery'], 'queued')
        self.assertEqual(self.store.task('ipad', second_id)['state'], 'queued')
        adapter.release.set()

    def test_requeue_steers_turns_undelivered_redirects_into_queued_tasks(self):
        first_id = 'c1' * 16
        self.store.room_submit('ipad', first_id, 'Build a car', 'codex')
        self.store.chat_claim('ipad', 'codex', 'codex-embedded')
        second_id = 'c2' * 16
        result = self.store.room_submit('ipad', second_id, 'Make it blue', 'codex', running_steerable=True)
        self.assertEqual(result, 'steer')
        self.assertEqual(self.store.requeue_steers('ipad', first_id), 1)
        self.assertEqual(self.store.task('ipad', second_id)['state'], 'queued')

    # ---------------------------------------------------------------- 6. capture evidence

    def test_capture_creates_artifact_and_is_readable_as_mcp_image(self):
        app = App(str(Path(self.temp.name) / 'app.sqlite3'), 'https://relay.example', 'ipad', 'd' * 40, 'a' * 40,
                  'client', 'c' * 40, 'o' * 40, ['https://client.example/callback'])
        app.store.exchange('ipad', heartbeat())
        app.store.register_participant('codex-embedded', 'agent', 'Codex', provider='codex')
        job = app.store.submit('ipad', 'capture', {'source': 'screenshot'}, 'req-cap-1', SCENE, 'codex-embedded')
        data = base64.b64encode(b'\x89PNG fixture bytes').decode()
        app.store.exchange('ipad', heartbeat())
        app.store.exchange('ipad', heartbeat(), {'job_id': job['job_id'], 'boot_id': BOOT,
                                                 'result': {'ok': True, 'value': {'mime_type': 'image/png',
                                                                                  'data': data, 'width': 4,
                                                                                  'height': 3,
                                                                                  'source': 'screenshot'}}})
        ledger = app.store.read_ledger('ipad', 0, 200)['entries']
        completed = [e for e in ledger if e.get('outcome') == 'completed' and e.get('artifacts')]
        self.assertEqual(len(completed), 1)
        artifact_id = completed[0]['artifacts'][0]

        fetched = app.call('read_artifact', {'artifact_id': artifact_id}, participant='codex-embedded')
        self.assertEqual(fetched['media']['media_type'], 'image/png')
        self.assertEqual(fetched['data'], data)

        rpc_reply = app.rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                             'params': {'name': 'read_artifact', 'arguments': {'artifact_id': artifact_id}}},
                            participant='codex-embedded')
        content = rpc_reply['result']['content']
        images = [c for c in content if c['type'] == 'image']
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]['data'], data)
        app.store.db.close()

    # ---------------------------------------------------------------- 7. post_note

    def test_post_note_creates_ledger_entry_and_room_event_and_requires_rationale_for_decision(self):
        app = App(str(Path(self.temp.name) / 'app2.sqlite3'), 'https://relay.example', 'ipad', 'd' * 40, 'a' * 40,
                  'client', 'c' * 40, 'o' * 40, ['https://client.example/callback'])
        app.store.exchange('ipad', heartbeat())
        app.store.register_participant('codex-embedded', 'agent', 'Codex', provider='codex')

        result = app.call('post_note', {'category': 'review', 'summary': 'Topology looks fine.'},
                          participant='codex-embedded')
        self.assertEqual(result['category'], 'review')
        ledger = app.store.read_ledger('ipad', 0, 50)['entries']
        self.assertTrue(any(e['category'] == 'review' and e['summary'] == 'Topology looks fine.' for e in ledger))
        events = app.store.room_events('ipad', 0)
        self.assertTrue(any(e['type'] == 'note' for e in events))

        with self.assertRaises(ValueError):
            app.call('post_note', {'category': 'decision', 'summary': 'Ship it.'}, participant='codex-embedded')
        app.store.db.close()

    # ---------------------------------------------------------------- 8. cursor/reset/ledger noise/labels

    def test_room_exchange_cursor_reset_and_ledger_noise_exclusion(self):
        room = Room(self.store, 'ipad', None)
        message_id = 'a' * 32
        self.store.room_submit('ipad', message_id, '# Shape the wheel arches\nBuild a car', 'codex')
        self.store.chat_claim('ipad', 'codex', 'codex-embedded')
        job = self.store.submit('ipad', 'execute_python', {'code': '# Shape the wheel arches\nresult=1'},
                                'req-cur-1', SCENE, 'codex-embedded')
        self.store.exchange('ipad', heartbeat())
        self.store.exchange('ipad', heartbeat(), {'job_id': job['job_id'], 'boot_id': BOOT,
                                                  'result': {'ok': True, 'value': {}}})

        reply = room.exchange({'v': 1, 'cursor': 0, 'stream_id': 'not-the-real-stream'})
        self.assertTrue(reply['reset'])
        self.assertTrue(reply['events'])

        for entry in reply['ledger']:
            if entry['category'] == 'lease':
                self.assertFalse(entry['lease'].get('implicit', False))
            self.assertNotEqual(entry.get('outcome'), 'dispatched')

        job_entries = [e for e in reply['ledger'] if 'job_id' in e]
        self.assertTrue(job_entries)
        for entry in job_entries:
            self.assertIn('label', entry)
            self.assertIn('kind', entry)
            self.assertEqual(entry['task_id'], message_id)

    def test_activity_label_leading_comment_and_code_hint_fallback(self):
        self.assertEqual(room_module.activity_label('execute_python', {'code': '# Shape the wheel arches\nresult=1'}),
                         'Shape the wheel arches')
        self.assertEqual(room_module.activity_label('execute_python',
                                                     {'code': 'bpy.ops.mesh.primitive_cube_add()'}),
                         'Building geometry')
        self.assertEqual(room_module.activity_label('execute_python', {'code': 'x = 1'}), 'Working in Blender')

    # ---------------------------------------------------------------- 9. failure mapping

    def test_agent_failure_maps_to_quota_exhausted_and_marks_agent_unavailable(self):
        router = self.make_router()
        adapter = FailingAdapter()
        router.register(adapter)

        message_id = 'b' * 32
        self.store.room_submit('ipad', message_id, 'Do something', 'codex')
        router.start()
        self.assertTrue(wait_until(lambda: self.store.task('ipad', message_id)['state'] == 'failed'))

        events = self.store.room_events('ipad', 0)
        errors = [e for e in events if e['type'] == 'error']
        self.assertTrue(errors)
        self.assertEqual(errors[-1]['payload']['failure']['code'], 'quota_exhausted')

        described = router.describe()
        entry = next(a for a in described if a['agent_id'] == 'codex')
        self.assertEqual(entry['availability']['state'], 'unavailable')
        self.assertEqual(entry['quota']['state'], 'exhausted')

        self.assertEqual(classify(RuntimeError('429 rate limit'), 'codex')['code'], 'quota_exhausted')

    # ---------------------------------------------------------------- 10. describe() schema

    @unittest.skipUnless(HAVE_JSONSCHEMA, 'jsonschema is not importable')
    def test_describe_output_matches_agent_schema(self):
        router = self.make_router()
        adapter = RecordingAdapter('codex')
        router.register(adapter)
        check = schema_validator('agent.schema.json')
        for descriptor in router.describe():
            errors = list(check.iter_errors(descriptor))
            self.assertEqual(errors, [], errors)

    # ---------------------------------------------------------------- 11. HTTP device exchange 'room'

    def test_http_device_exchange_room_key_and_malformed_room_payload(self):
        app = App(str(Path(self.temp.name) / 'http.sqlite3'), 'https://relay.example', 'ipad', 'd' * 40, 'a' * 40,
                  'client', 'c' * 40, 'o' * 40, ['https://client.example/callback'])
        server = Server(('127.0.0.1', 0), app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            import http.client

            def request(body):
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                headers = {'Host': 'relay.example', 'Accept': 'application/json, text/event-stream',
                          'Content-Type': 'application/json', 'Authorization': 'Bearer ' + 'd' * 40}
                conn.request('POST', '/device/exchange', json.dumps(body), headers)
                r = conn.getresponse()
                raw = r.read()
                conn.close()
                return r.status, json.loads(raw)

            status, reply = request({'protocol': 1, 'device_id': 'ipad', 'heartbeat': heartbeat(),
                                     'room': {'v': 1}})
            self.assertEqual(status, 200)
            self.assertIn('room', reply)
            self.assertNotIn('error', reply['room'])

            status, reply = request({'protocol': 1, 'device_id': 'ipad', 'heartbeat': heartbeat(),
                                     'room': {'v': 99}})
            self.assertEqual(status, 200)
            self.assertIn('error', reply['room'])
            self.assertIn('ack', reply)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            app.store.db.close()

    # ---------------------------------------------------------------- 12. restart while running

    def test_restart_while_task_running_marks_uncertain_with_interrupted_event(self):
        message_id = 'c' * 32
        self.store.room_submit('ipad', message_id, 'Do something long', 'codex')
        task = self.store.chat_claim('ipad', 'codex', 'codex-embedded')
        self.assertEqual(task['state'], 'running')
        self.store.db.close()

        self.store = Store(self.path)
        reloaded = self.store.task('ipad', message_id)
        self.assertEqual(reloaded['state'], 'uncertain')

        events = self.store.room_events('ipad', 0)
        errors = [e for e in events if e['type'] == 'error']
        self.assertTrue(errors)
        self.assertEqual(errors[-1]['payload']['failure']['code'], 'interrupted_after_possible_mutation')


if __name__ == '__main__':
    unittest.main()
