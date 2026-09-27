#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Produce real GHOSTroom snapshots for the native simulator harness.

The snapshots come from the actual relay (store, router, room exchange) and the
actual GhostBlender runtime presentation model (ghostroom.py), driven by a fake
agent. The native harness then renders exactly what the device would render.
"""
import base64
import json
from pathlib import Path
import struct
import sys
import tempfile
import threading
import zlib

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'agent/relay'))
sys.path.insert(0, str(ROOT / 'agent/runtime'))

from room import Room  # noqa: E402
from router import AgentAdapter, AgentRouter  # noqa: E402
from store import Store  # noqa: E402
import ghostroom  # noqa: E402

BOOT, SCENE = 'b' * 32, 'c' * 32


def png(width=480, height=300, tint=(111, 227, 207)):
    rows = b''
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            shade = (x * 255 // width + y * 64 // height) % 256
            row += bytes((min(255, tint[0] * shade // 255 + 20), min(255, tint[1] * shade // 255 + 24),
                          min(255, tint[2] * shade // 255 + 32)))
        rows += bytes(row)

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows, 9)) + chunk(b'IEND', b''))


def heartbeat():
    return {'boot_id': BOOT, 'scene_id': SCENE, 'blender_version': '5.2.0', 'native': {'foreground': True}}


def complete(store, job, value=None, ok=True):
    store.exchange('ipad', heartbeat())
    result = {'ok': ok, 'value': value or {}} if ok else {'ok': False, 'error': value}
    store.exchange('ipad', heartbeat(), {'job_id': job['job_id'], 'boot_id': BOOT, 'result': result})


class Builder(AgentAdapter):
    agent_id, provider, display_name, participant_id = 'codex', 'codex', 'Codex', 'codex-embedded'
    can_steer = True

    def __init__(self):
        self.release = threading.Event()
        self.started = threading.Event()

    def probe(self):
        return {'availability': 'available', 'auth': 'signed_in', 'quota': 'ok', 'model': 'gpt-5-codex'}

    def steer(self, text):
        pass

    def run_turn(self, turn):
        s, n = self.store, 0

        def job(op, args):
            nonlocal n
            n += 1
            return s.submit('ipad', op, args, f'harness-{n:05d}', SCENE, self.participant_id)
        complete(s, job('inspect_scene', {}), {'objects': []})
        turn.narrate('I will block out the body proportions first, then the wheels and materials.')
        for step in ('# Block out body proportions\nresult=1', '# Block out body proportions\nresult=2',
                     '# Shape the wheel arches\nresult=3', 'bpy.data.materials.new("Paint")\nresult=4'):
            complete(s, job('execute_python', {'code': step}))
        complete(s, job('capture', {'source': 'screenshot'}),
                 {'mime_type': 'image/png', 'data': base64.b64encode(png()).decode(), 'width': 480,
                  'height': 300, 'source': 'screenshot'})
        job('execute_python', {'code': '# Add headlights\nresult=5'})  # still running
        self.started.set()
        self.release.wait(10)
        return ('The **car body** is blocked out and the wheel arches are shaped.\n\n'
                '- Paint material added\n- Verified with a viewport capture\n\nShall I refine the headlights?')


class Reviewer(AgentAdapter):
    agent_id, provider, display_name, participant_id = 'claude', 'claude', 'Claude', 'claude-embedded'

    def probe(self):
        return {'availability': 'unavailable', 'auth': 'signed_out', 'quota': 'unknown',
                'reason': {'code': 'auth_expired', 'layer': 'agent_provider', 'retry': 'after_user_action',
                           'mutation_possible': False,
                           'message': 'Claude is not configured on the relay: set ANTHROPIC_API_KEY.'}}

    def run_turn(self, turn):
        raise RuntimeError('unused')


class Native:
    def __init__(self):
        self.pushed = None

    def room_take(self):
        return []

    def room_update(self, value):
        self.pushed = value


def sync(client, room):
    for _ in range(4):
        client.apply(room.exchange(client.payload()))


def main(out):
    out = Path(out)
    snapshots, evidence = out / 'snapshots', out / 'evidence'
    snapshots.mkdir(parents=True, exist_ok=True)
    evidence.mkdir(parents=True, exist_ok=True)
    temp = tempfile.mkdtemp()
    store = Store(temp + '/relay.sqlite3')
    store.exchange('ipad', heartbeat())
    router = AgentRouter(store, 'ipad', 'http://127.0.0.1:1', poll_interval=0.01)
    builder = router.register(Builder())
    router.register(Reviewer())
    room = Room(store, 'ipad', router)
    native = Native()
    client = ghostroom.RoomClient(temp + '/device', native)

    def write(name, open_request=0):
        client.mark()
        client.open_requests = open_request
        client.push()
        text = native.pushed.replace(str(client.evidence), '@EVIDENCE@')
        (snapshots / f'{name}.json').write_text(text)
        return json.loads(text)

    write('empty')
    # External agent activity through GhostBlender Simple (legacy participant).
    complete(store, store.submit('ipad', 'inspect_scene', {}, 'external-0001', SCENE), {'objects': []})
    client.send('Build a stylised sports car from the reference', agent_id='codex')
    sync(client, room)
    router.start()
    builder.started.wait(5)
    sync(client, room)
    store.post_note('ipad', 'codex-embedded', 'decision', 'Use a subdivision-surface workflow for the body.',
                    rationale='Keeps the silhouette editable while proportions are still changing.')
    sync(client, room)
    working = write('working', open_request=1)
    builder.release.set()
    router.close()
    sync(client, room)
    write('done', open_request=1)

    # A failure, then an interrupted session with recovery.
    client.send('Render a check from the camera', agent_id='claude', mode='review_my_work', role='reviewer')
    sync(client, room)
    task = store.chat_claim('ipad', 'claude', 'claude-embedded')
    store.chat_fail('ipad', task['task_id'], 'Claude has reached its usage limit. Another agent can continue.',
                    {'code': 'quota_exhausted', 'layer': 'agent_provider', 'message': '429 usage limit',
                     'mutation_possible': False, 'retry': 'switch_agent', 'agent_id': 'claude'})
    client.send('Now add a spoiler', agent_id='codex')
    sync(client, room)
    store.chat_claim('ipad', 'codex', 'codex-embedded')
    store.submit('ipad', 'execute_python', {'code': '# Model the rear spoiler\nresult=1'}, 'spoiler-0001', SCENE,
                 'codex-embedded')
    sync(client, room)
    client.save(force=True)
    crashed = ghostroom.RoomClient(temp + '/device', native)  # previous session never closed: recovery
    sync(crashed, room)
    crashed.mark()
    crashed.open_requests = 1
    crashed.push()
    (snapshots / 'recovery.json').write_text(native.pushed.replace(str(crashed.evidence), '@EVIDENCE@'))

    # A long session for layout and performance checks.
    big = json.loads(json.dumps(working))
    items = []
    while len(items) < 160:
        items.extend(json.loads(json.dumps(working['items'])))
    for index, item in enumerate(items[:160]):
        item['id'] = f"{item['id']}-{index}"
    big['items'] = items[:160]
    (snapshots / 'long.json').write_text(json.dumps(big))
    for path in client.evidence.glob('*.png'):
        (evidence / path.name).write_bytes(path.read_bytes())
    print('snapshots:', sorted(p.name for p in snapshots.iterdir()), 'evidence:', len(list(evidence.iterdir())))
    counts = {name: len(json.loads((snapshots / f'{name}.json').read_text())['items'])
              for name in ('empty', 'working', 'done', 'recovery', 'long')}
    (snapshots / 'counts.json').write_text(json.dumps(counts))
    print(counts)


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'build/harness')
