# SPDX-License-Identifier: GPL-2.0-or-later
"""GHOSTroom device-side runtime coverage: RoomClient, attachments, recovery and
the full round trip against a real relay Store/Room/AgentRouter.
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
sys.path.insert(0, str(ROOT / 'agent/runtime'))
from store import Store  # noqa: E402
from room import Room  # noqa: E402
from router import AgentAdapter, AgentRouter  # noqa: E402
import ghostroom  # noqa: E402

SCENE = 'c' * 32
BOOT = 'b' * 32


def hb(boot=BOOT, scene=SCENE):
    return {'boot_id': boot, 'scene_id': scene, 'blender_version': '5.2.0', 'native': {'foreground': True}}


def wait_until(condition, timeout=3.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return condition()


class Native:
    def __init__(self):
        self.out = []
        self.pushed = None

    def room_take(self):
        out, self.out = self.out, []
        return out

    def room_update(self, encoded):
        self.pushed = json.loads(encoded)


class FakeRuntime:
    def __init__(self):
        self.selected = []
        self.journal = {}

    def inspect_scene(self, offset, limit):
        objects = [{'name': 'Cube'}, {'name': 'Sphere'}]
        return {'scene_name': 'Scene', 'object_count': len(objects), 'objects': objects,
                'selected': self.selected, 'active': self.selected[0] if self.selected else None,
                'mode': 'OBJECT'}

    def capture(self, source, max_size):
        return {'data': base64.b64encode(b'\x89PNG fake capture').decode(), 'width': 8, 'height': 6}


class IntegrationAdapter(AgentAdapter):
    agent_id = 'codex'
    participant_id = 'codex-embedded'
    display_name = 'Codex'
    provider = 'codex'

    def __init__(self):
        self.paused = threading.Event()
        self.resume = threading.Event()

    def probe(self):
        return {'availability': 'available', 'auth': 'signed_in', 'quota': 'ok'}

    def run_turn(self, turn):
        job = self.store.submit(self.device_id, 'execute_python', {'code': '# Build the body\nresult=1'},
                                 'req-int-1', SCENE, self.participant_id)
        self.paused.set()
        self.resume.wait(5)
        self.store.exchange(self.device_id, hb())
        self.store.exchange(self.device_id, hb(), {'job_id': job['job_id'], 'boot_id': BOOT,
                                                    'result': {'ok': True, 'value': {}}})
        cap = self.store.submit(self.device_id, 'capture', {'source': 'screenshot'}, 'req-int-2', SCENE,
                                self.participant_id)
        self.store.exchange(self.device_id, hb())
        data = base64.b64encode(b'\x89PNG fake capture bytes').decode()
        self.store.exchange(self.device_id, hb(), {'job_id': cap['job_id'], 'boot_id': BOOT,
                                                    'result': {'ok': True, 'value': {'mime_type': 'image/png',
                                                                                     'data': data, 'width': 4,
                                                                                     'height': 3,
                                                                                     'source': 'screenshot'}}})
        return 'Car body blocked out.'


class RoomClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def new_client(self, name='client', runtime=None):
        root = Path(self.temp.name) / name
        root.mkdir(parents=True, exist_ok=True)
        native = Native()
        runtime_getter = (lambda: runtime) if runtime is not None else None
        client = ghostroom.RoomClient(str(root), native, runtime_getter=runtime_getter)
        return client, native

    # ---------------------------------------------------------------- 1. send/payload/apply

    def test_send_shows_pending_item_and_leaves_outbox_after_ack(self):
        client, _ = self.new_client()
        message_id = client.send('Hello world')
        self.assertIsNotNone(message_id)

        snapshot = ghostroom.build_snapshot(client)
        pending = [item for item in snapshot['items'] if item['type'] == 'user' and item.get('pending')]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['text'], 'Hello world')

        payload = client.payload()
        self.assertEqual(payload['messages'][0]['id'], message_id)

        client.apply({'v': 1, 'ack_ids': [{'id': message_id, 'delivery': 'queued'}]})
        self.assertEqual(client.state['outbox']['messages'], [])

    # ---------------------------------------------------------------- 2. attachments

    def test_attach_scene_selection_and_viewport_upload_flow(self):
        runtime = FakeRuntime()
        client, _ = self.new_client(runtime=runtime)

        scene_item = client.attach('scene')
        self.assertEqual(scene_item['state'], 'ready')
        self.assertIn('object_count', scene_item['text'])

        runtime.selected = []
        selection_item = client.attach('selection')
        self.assertEqual(selection_item['state'], 'failed')

        runtime.selected = ['Cube']
        viewport_item = client.attach('viewport')
        self.assertEqual(viewport_item['state'], 'ready')
        self.assertTrue(Path(viewport_item['path']).exists())

        message_id = client.send('Look at this', attachment_ids=[viewport_item['id']])
        payload = client.payload()
        self.assertIn('artifacts', payload)
        # The message must wait for its upload before it is sent.
        self.assertNotIn('messages', payload)

        upload = client.state['outbox']['uploads'][0]
        client.apply({'v': 1, 'artifact_acks': [upload['artifact_id']]})

        payload2 = client.payload()
        self.assertIn('messages', payload2)
        self.assertEqual(payload2['messages'][0]['id'], message_id)
        self.assertEqual(payload2['messages'][0]['context'][0]['artifact_id'], upload['artifact_id'])

    # ---------------------------------------------------------------- 3. full integration

    def test_full_integration_with_relay_store_room_and_router(self):
        store = Store(str(Path(self.temp.name) / 'relay.sqlite3'))
        store.exchange('ipad', hb())
        router = AgentRouter(store, 'ipad', 'http://127.0.0.1:1', poll_interval=0.01)
        adapter = IntegrationAdapter()
        router.register(adapter)
        room = Room(store, 'ipad', router)
        client, _ = self.new_client(name='device')

        client.send('Build a car', agent_id='codex')
        client.apply(room.exchange(client.payload()))

        router.start()
        try:
            self.assertTrue(wait_until(lambda: adapter.paused.is_set()))
            client.apply(room.exchange(client.payload()))
            snapshot = ghostroom.build_snapshot(client)
            self.assertEqual(snapshot['activity']['state'], 'executing')
            self.assertEqual(snapshot['activity']['detail'], 'Build the body')

            adapter.resume.set()

            def caught_up():
                client.apply(room.exchange(client.payload()))
                return store.task('ipad', client.state['tasks'][0]['task_id'])['state'] == 'completed' \
                    if client.state['tasks'] else False

            self.assertTrue(wait_until(caught_up))
        finally:
            router.close()

        snapshot = ghostroom.build_snapshot(client)
        types = {item['type'] for item in snapshot['items']}
        self.assertIn('user', types)
        self.assertIn('task', types)
        self.assertIn('agent', types)
        task_item = next(item for item in snapshot['items'] if item['type'] == 'task')
        self.assertTrue(task_item['phases'])
        self.assertTrue(task_item['evidence'])
        self.assertEqual(snapshot['activity']['state'], 'idle')

    # ---------------------------------------------------------------- 4. recovery

    def test_recovery_detected_after_crash_and_continue_action(self):
        root = Path(self.temp.name) / 'recover'
        root.mkdir()
        native = Native()
        client = ghostroom.RoomClient(str(root), native)
        message_id = client.send('Do a thing')
        running_reply = {
            'v': 1, 'stream_id': 's1', 'ledger_stream_id': 'l1', 'events': [], 'ledger': [],
            'ack_ids': [{'id': message_id, 'delivery': 'queued'}], 'agents': [], 'leases': [],
            'tasks': [{'task_id': 'tid1', 'agent_id': 'codex', 'state': 'running', 'title': 'Do a thing',
                      'mode': 'do', 'role': 'primary', 'created': time.time(), 'started': time.time(),
                      'finished': None, 'read_only': False, 'stop_requested': False}],
        }
        client.apply(running_reply)
        # A normal periodic disk flush happens during use; simulate a crash by never
        # calling client.close(), so session.open stays True on disk.
        client.save(force=True)
        del client

        client2 = ghostroom.RoomClient(str(root), Native())
        snapshot = ghostroom.build_snapshot(client2)
        self.assertIsNotNone(snapshot['recovery'])
        self.assertEqual(snapshot['recovery']['title'], 'Do a thing')

        client2.handle({'type': 'recovery', 'action': 'continue'})
        self.assertTrue(client2.state['recovery']['dismissed'])
        self.assertTrue(client2.state['outbox']['messages'])

    def test_recovery_mutation_possible_from_runtime_journal(self):
        root = Path(self.temp.name) / 'recover2'
        root.mkdir()
        runtime = FakeRuntime()
        native = Native()
        client = ghostroom.RoomClient(str(root), native, runtime_getter=lambda: runtime)
        message_id = client.send('Do a risky thing')
        running_reply = {
            'v': 1, 'stream_id': 's1', 'ledger_stream_id': 'l1', 'events': [], 'ledger': [],
            'ack_ids': [{'id': message_id, 'delivery': 'queued'}], 'agents': [], 'leases': [],
            'tasks': [{'task_id': 'tid2', 'agent_id': 'codex', 'state': 'running', 'title': 'Do a risky thing',
                      'mode': 'do', 'role': 'primary', 'created': time.time(), 'started': time.time(),
                      'finished': None, 'read_only': False, 'stop_requested': False}],
        }
        client.apply(running_reply)
        client.save(force=True)
        del client

        runtime.journal = {'job-1': {'state': 'uncertain', 'error': 'app_restarted_during_command',
                                     'operation': 'execute_python'}}
        client2 = ghostroom.RoomClient(str(root), Native(), runtime_getter=lambda: runtime)
        info = ghostroom.recovery_info(client2)
        self.assertIsNotNone(info)
        self.assertTrue(info['mutation_possible'])

    # ---------------------------------------------------------------- 5. capture evidence

    def test_job_executed_stores_and_finds_capture_evidence(self):
        client, _ = self.new_client()
        job = {'job_id': 'jobabc123', 'operation': 'capture'}
        outbox = {'result': {'ok': True, 'value': {'mime_type': 'image/png',
                                                    'data': base64.b64encode(b'\x89PNG evidence bytes').decode()}}}
        client.job_executed(job, outbox)
        path = client.evidence_path(job_id='jobabc123')
        self.assertIsNotNone(path)
        self.assertTrue(Path(path).exists())

    # ---------------------------------------------------------------- 6. stream reset

    def test_apply_with_different_stream_id_clears_cached_events(self):
        client, _ = self.new_client()
        client.apply({'v': 1, 'stream_id': 's1',
                      'events': [{'seq': 1, 'type': 'status', 'text': 'hi', 'time': time.time()}]})
        self.assertTrue(client.state['events'])

        client.apply({'v': 1, 'stream_id': 's2', 'events': []})
        self.assertEqual(client.state['events'], [])
        self.assertEqual(client.state['cursor'], 0)


if __name__ == '__main__':
    unittest.main()
