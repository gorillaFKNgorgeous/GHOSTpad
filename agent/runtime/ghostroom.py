# SPDX-License-Identifier: GPL-2.0-or-later
"""GHOSTroom inside GhostBlender: the bridge between the native iPadOS workspace and the relay.

Responsibilities, following target.md §23:
- the native shell (ghostroom_ui.mm) owns presentation and input only. It sends
  small JSON commands and renders the snapshot built here;
- this module runs on Blender's main thread inside the existing GhostBlender
  dispatcher. It gathers context through the same Runtime tool layer agents use
  (read-only inspect/capture), keeps a durable local cache for crash recovery,
  and rides the existing authenticated device exchange (`room`);
- the relay owns agents, tasks and the Shared Workspace Ledger.

Nothing here manipulates the Blender scene. Scene changes only ever happen
through relay jobs executed by core.Runtime.execute.
"""
import base64
import json
from pathlib import Path
import re
import time
import uuid

try:
    from .core import atomic_json
except ImportError:  # Loaded standalone by tests.
    from core import atomic_json

PROTOCOL = 1
MAX_EVENTS = 600
MAX_LEDGER = 900
MAX_ITEMS = 160
MAX_TEXT = 16000
MAX_CONTEXT_TEXT = 12000
MAX_UPLOAD = 1_900_000
TEXT_SUFFIXES = ('.py', '.txt', '.md', '.json', '.csv', '.glsl', '.osl', '.yaml', '.yml', '.toml', '.xml')
MODES = (('do', 'Do it for me'), ('with_me', 'Do it with me'), ('teach', 'Teach me'),
         ('explain', 'Explain this'), ('review_my_work', 'Review my work'))
ROLES = (('primary', 'Primary'), ('reviewer', 'Reviewer'), ('specialist', 'Specialist'),
         ('critic', 'Critic'), ('verifier', 'Verifier'))
CONTEXT_TITLES = {'scene': 'Current scene', 'selection': 'Selected objects', 'viewport': 'Viewport',
                  'render': 'Render result', 'image': 'Image', 'file': 'File'}
ACTIVE_STATES = ('queued', 'running', 'stopping')
KIND_STATES = {'inspect': ('inspecting', 'Inspecting'), 'execute': ('executing', 'Working in Blender'),
               'capture': ('reviewing', 'Checking the result'), 'persist': ('executing', 'Saving a script')}


def _now():
    return time.time()


def _hex():
    return uuid.uuid4().hex


class RoomClient:
    def __init__(self, root, native=None, runtime_getter=None, bpy_module=None):
        self.root = Path(root) / 'ghostroom'
        self.root.mkdir(parents=True, exist_ok=True)
        self.evidence = self.root / 'evidence'
        self.evidence.mkdir(exist_ok=True)
        self.state_path = self.root / 'state.json'
        self.native = native
        self.runtime_getter = runtime_getter or (lambda: None)
        self.bpy = bpy_module
        self.connection = {'state': 'offline', 'label': 'Not connected', 'detail': ''}
        self.rev = 0
        self._dirty = True
        self._last_push = None
        self._save_due = 0.0
        self._fetch_wait = 0.0
        self.urgent = False
        self.open_requests = 0
        self.ui_open = False
        self.state = self._load()
        # A task that was still active when the previous app session ended is a
        # recovery candidate: the app may have crashed or been killed mid-task.
        previous = self.state.get('session') or {}
        if previous.get('open'):
            active = [t for t in self.state.get('tasks', []) if t.get('state') in ACTIVE_STATES]
            if active or previous.get('executing'):
                self.state['recovery'] = {
                    'detected_at': _now(), 'reason': 'app_closed',
                    'task_ids': [t['task_id'] for t in active],
                    'executing': previous.get('executing'), 'dismissed': False}
        self.state['session'] = {'open': True, 'started': _now(), 'executing': None}
        self.save(force=True)

    # ------------------------------------------------------------------ persistence

    def _load(self):
        empty = {'v': PROTOCOL, 'stream_id': None, 'cursor': 0, 'ledger_stream_id': None, 'ledger_cursor': 0,
                 'events': [], 'ledger': [], 'agents': [], 'default_agent': None, 'tasks': [], 'leases': [],
                 'prefs': {'agent_id': None, 'mode': 'do', 'role': 'primary'},
                 'outbox': {'messages': [], 'controls': [], 'uploads': []},
                 'attachments': [], 'recovery': None, 'last_sync': None, 'session': {}}
        try:
            value = json.loads(self.state_path.read_text())
            if value.get('v') == PROTOCOL:
                for key, default in empty.items():
                    value.setdefault(key, default)
                return value
        except (OSError, ValueError):
            pass
        return empty

    def save(self, force=False):
        now = time.monotonic()
        if not force and now < self._save_due:
            return
        self._save_due = now + 2.0
        try:
            atomic_json(self.state_path, self.state)
        except (OSError, ValueError, TypeError):
            pass  # The relay is authoritative; a lost cache only costs a resync.

    def close(self):
        self.state['session'] = {'open': False}
        self.save(force=True)

    def mark(self):
        self._dirty = True

    # ------------------------------------------------------------------ native UI

    def request_open(self):
        self.open_requests += 1
        self.mark()
        self.push()

    def take_commands(self):
        if self.native is None or not hasattr(self.native, 'room_take'):
            return []
        commands = []
        for raw in self.native.room_take() or []:
            try:
                value = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                commands.append(value)
        return commands

    def push(self):
        if self.native is None or not hasattr(self.native, 'room_update') or not self._dirty:
            return
        self._dirty = False
        self.rev += 1
        snapshot = build_snapshot(self)
        encoded = json.dumps(snapshot, separators=(',', ':'), allow_nan=False)
        if encoded != self._last_push:
            self._last_push = encoded
            self.native.room_update(encoded)

    def handle(self, command):
        kind = command.get('type')
        if kind == 'ready':
            self._last_push = None
        elif kind == 'visible':
            self.ui_open = bool(command.get('open'))
        elif kind == 'send':
            self.send(command.get('text', ''), command.get('agent_id'), command.get('mode'), command.get('role'),
                      command.get('attachments') or [], command.get('id'))
        elif kind == 'stop' and isinstance(command.get('task_id'), str):
            self.state['outbox']['controls'].append({'id': _hex(), 'action': 'stop',
                                                     'task_id': command['task_id']})
            self.urgent = True
        elif kind == 'select':
            prefs = self.state['prefs']
            if isinstance(command.get('agent_id'), str):
                prefs['agent_id'] = command['agent_id']
            if command.get('mode') in dict(MODES):
                prefs['mode'] = command['mode']
            if command.get('role') in dict(ROLES):
                prefs['role'] = command['role']
        elif kind == 'attach':
            self.attach(command.get('kind'))
        elif kind == 'attach_file':
            self.attach_file(command)
        elif kind == 'remove_attachment':
            self.state['attachments'] = [a for a in self.state['attachments'] if a['id'] != command.get('id')]
        elif kind == 'recovery':
            self.recovery_action(command.get('action'))
        elif kind == 'fetch' and isinstance(command.get('artifact_id'), str):
            self.state.setdefault('fetch_queue', [])
            if command['artifact_id'] not in self.state['fetch_queue']:
                self.state['fetch_queue'].insert(0, command['artifact_id'])
        self.mark()

    # ------------------------------------------------------------------ composing

    def send(self, text, agent_id=None, mode=None, role=None, attachment_ids=(), message_id=None):
        text = str(text or '').strip()
        if not text:
            return None
        prefs = self.state['prefs']
        message_id = message_id if isinstance(message_id, str) and re.fullmatch(r'[a-f0-9]{32}', message_id) \
            else _hex()
        if any(m['id'] == message_id for m in self.state['outbox']['messages']):
            return message_id
        context, uploads = [], []
        drafts = {a['id']: a for a in self.state['attachments']}
        for attach_id in attachment_ids:
            item = drafts.pop(attach_id, None)
            if not item or item.get('state') != 'ready':
                continue
            entry = {'kind': item['kind'], 'title': item['title']}
            if item.get('text'):
                entry['text'] = item['text']
            if item.get('path'):
                artifact_id = 'art' + _hex()
                entry['artifact_id'] = artifact_id
                uploads.append({'artifact_id': artifact_id, 'path': item['path'], 'title': item['title'],
                                'kind': {'viewport': 'screenshot', 'render': 'render'}.get(item['kind'], 'reference'),
                                'media_type': item.get('media_type', 'image/png'), 'width': item.get('width'),
                                'height': item.get('height'), 'state': 'pending'})
            context.append(entry)
        self.state['attachments'] = list(drafts.values())
        self.state['outbox']['uploads'].extend(uploads)
        self.state['outbox']['messages'].append({
            'id': message_id, 'text': text[:MAX_TEXT],
            'agent_id': agent_id or prefs.get('agent_id') or self.state.get('default_agent'),
            'mode': mode if mode in dict(MODES) else prefs.get('mode', 'do'),
            'role': role if role in dict(ROLES) else prefs.get('role', 'primary'),
            'context': context, 'created': _now()})
        self.urgent = True
        self.mark()
        self.save(force=True)  # an unsent instruction must survive a crash
        return message_id

    def recovery_action(self, action):
        recovery = self.state.get('recovery')
        if not recovery:
            return
        recovery['dismissed'] = True
        info = recovery_info(self)
        title = (info or {}).get('title') or 'the previous task'
        agent = (info or {}).get('agent_id')
        if action == 'continue':
            self.send(f'Continue the interrupted task "{title}". First inspect the live Blender scene and the '
                      'workspace brief to establish what was actually applied before the interruption.',
                      agent_id=agent, mode='do', role='primary')
        elif action == 'explain':
            self.send(f'Where did the interrupted task "{title}" reach before GHOSTpad closed? Use the ledger and '
                      'the current Blender scene; say what may be incomplete.', agent_id=agent, mode='explain',
                      role='primary')

    # ------------------------------------------------------------------ context attachments

    def _attachment(self, kind, **values):
        item = {'id': _hex()[:16], 'kind': kind, 'title': CONTEXT_TITLES.get(kind, kind), 'state': 'ready',
                'created': _now()}
        item.update(values)
        self.state['attachments'] = [a for a in self.state['attachments'] if a['kind'] != kind or
                                     kind in ('image', 'file')][-7:] + [item]
        return item

    def attach(self, kind):
        """Gather context through GhostBlender's own read-only tools. Never mutates Blender."""
        runtime = self.runtime_getter()
        try:
            if runtime is None:
                raise RuntimeError('GhostBlender is not running yet')
            if kind == 'scene':
                value = runtime.inspect_scene(0, 60)
                text = json.dumps(value, separators=(',', ':'))[:MAX_CONTEXT_TEXT]
                return self._attachment('scene', title=f"Scene: {value.get('scene_name', '')}"[:80],
                                        subtitle=f"{value.get('object_count', 0)} objects", text=text)
            if kind == 'selection':
                value = runtime.inspect_scene(0, 200)
                selected = set(value.get('selected') or [])
                objects = [o for o in value.get('objects', []) if o.get('name') in selected][:16]
                if not objects:
                    raise ValueError('Nothing is selected')
                detail = {'active': value.get('active'), 'mode': value.get('mode'), 'objects': objects}
                names = ', '.join(o['name'] for o in objects[:3]) + (' …' if len(objects) > 3 else '')
                return self._attachment('selection', title=f'Selected: {names}'[:80],
                                        subtitle=f'{len(objects)} object(s)',
                                        text=json.dumps(detail, separators=(',', ':'))[:MAX_CONTEXT_TEXT])
            if kind in ('viewport', 'render'):
                value = runtime.capture('screenshot' if kind == 'viewport' else 'render_result', 1280)
                data = base64.b64decode(value['data'])
                path = self.evidence / f'attach-{_hex()}.png'
                path.write_bytes(data)
                return self._attachment(kind, path=str(path), media_type='image/png', width=value['width'],
                                        height=value['height'], subtitle=f"{value['width']}×{value['height']}")
            raise ValueError('unknown context kind')
        except Exception as exc:
            message = str(exc).split('\n')[0][:160]
            if 'no_render_result' in message:
                message = 'No render result yet — render first'
            return self._attachment(kind or 'scene', state='failed', subtitle=message)

    def attach_file(self, command):
        path = Path(str(command.get('path') or ''))
        name = str(command.get('name') or path.name)[:80]
        kind = 'image' if command.get('kind') == 'image' else 'file'
        try:
            size = path.stat().st_size
            media_type = command.get('media_type')
            if media_type in ('image/png', 'image/jpeg'):
                if size > MAX_UPLOAD:
                    raise ValueError('Image is too large')
                width, height = int(command.get('width') or 0), int(command.get('height') or 0)
                if width < 1 or height < 1:
                    raise ValueError('Unknown image size')
                return self._attachment(kind, title=name, path=str(path), media_type=media_type, width=width,
                                        height=height, subtitle=f'{width}×{height}')
            if path.suffix.lower() in TEXT_SUFFIXES and size <= 64_000:
                text = path.read_text(encoding='utf-8', errors='replace')[:MAX_CONTEXT_TEXT]
                return self._attachment('file', title=name, text=f'File {name}:\n{text}',
                                        subtitle=f'{size} bytes')
            return self._attachment('file', title=name, subtitle=f'{size} bytes, name only',
                                    text=f'The user attached the file "{name}" ({size} bytes). Its contents are not '
                                         'readable as text here.')
        except Exception as exc:
            return self._attachment(kind, title=name, state='failed', subtitle=str(exc)[:160])

    # ------------------------------------------------------------------ exchange

    def payload(self):
        outbox = self.state['outbox']
        room = {'v': PROTOCOL, 'cursor': self.state['cursor'], 'ledger_cursor': self.state['ledger_cursor']}
        if self.state['stream_id']:
            room['stream_id'] = self.state['stream_id']
        if self.state['ledger_stream_id']:
            room['ledger_stream_id'] = self.state['ledger_stream_id']
        pending = [u for u in outbox['uploads'] if u['state'] == 'pending']
        if pending:
            upload = pending[0]
            try:
                data = Path(upload['path']).read_bytes()
                if len(data) > MAX_UPLOAD:
                    raise ValueError('too large')
                room['artifacts'] = [{'artifact_id': upload['artifact_id'], 'kind': upload['kind'],
                                      'media_type': upload['media_type'], 'width': upload['width'],
                                      'height': upload['height'], 'title': upload['title'],
                                      'data': base64.b64encode(data).decode()}]
            except (OSError, ValueError):
                upload['state'] = 'failed'
        blocked = {u['artifact_id'] for u in outbox['uploads'] if u['state'] == 'pending'}
        ready = []
        for message in outbox['messages']:
            if any(c.get('artifact_id') in blocked for c in message['context']):
                break  # keep order: later messages wait for earlier uploads
            failed = {u['artifact_id'] for u in outbox['uploads'] if u['state'] == 'failed'}
            context = [c for c in message['context'] if c.get('artifact_id') not in failed]
            ready.append({k: v for k, v in message.items() if k not in ('created', 'context')} | {'context': context})
            if len(ready) == 4:
                break
        if ready:
            room['messages'] = ready
        if outbox['controls']:
            room['controls'] = outbox['controls'][:8]
        queue = self.state.get('fetch_queue') or []
        if queue and time.monotonic() >= self._fetch_wait and 'artifacts' not in room:
            room['fetch'] = queue[0]
            self._fetch_wait = time.monotonic() + 3.0
        return room

    def apply(self, room):
        """Consume a relay room reply on Blender's main thread."""
        if not isinstance(room, dict) or room.get('v') != PROTOCOL:
            return
        state = self.state
        if room.get('error'):
            self.connection = {'state': 'degraded', 'label': 'GHOSTroom sync problem', 'detail': room['error']}
            self.mark()
            return
        outbox = state['outbox']
        acked = {a['id'] for a in room.get('ack_ids', []) if isinstance(a, dict)}
        rejected = {p.get('id'): p.get('error') for p in room.get('problems', []) if p.get('id')}
        if rejected:
            for message in outbox['messages']:
                if message['id'] in rejected:
                    state.setdefault('local_errors', []).append(
                        {'id': 'x' + message['id'], 'time': _now(), 'text': f"Not sent: {rejected[message['id']]}",
                         'message': message['text'][:200]})
        outbox['messages'] = [m for m in outbox['messages'] if m['id'] not in acked and m['id'] not in rejected]
        done_controls = {a.get('id') for a in room.get('control_acks', [])}
        outbox['controls'] = [c for c in outbox['controls'] if c['id'] not in done_controls]
        uploaded = set(room.get('artifact_acks', []))
        failed_uploads = {p.get('artifact_id') for p in room.get('problems', []) if p.get('artifact_id')}
        for upload in outbox['uploads']:
            if upload['artifact_id'] in uploaded:
                upload['state'] = 'done'
                self._cache_upload(upload)
            elif upload['artifact_id'] in failed_uploads:
                upload['state'] = 'failed'
        outbox['uploads'] = [u for u in outbox['uploads'] if u['state'] == 'pending' or
                             any(c.get('artifact_id') == u['artifact_id'] for m in outbox['messages']
                                 for c in m['context'])]

        if room.get('reset') or (state['stream_id'] and room.get('stream_id') != state['stream_id']):
            state['events'], state['cursor'] = [], 0
        if room.get('ledger_reset') or (state['ledger_stream_id'] and
                                        room.get('ledger_stream_id') != state['ledger_stream_id']):
            state['ledger'], state['ledger_cursor'] = [], 0
        state['stream_id'], state['ledger_stream_id'] = room.get('stream_id'), room.get('ledger_stream_id')
        seen = {e['seq'] for e in state['events'][-200:]}
        for event in room.get('events', []):
            if isinstance(event, dict) and type(event.get('seq')) is int and event['seq'] not in seen:
                state['events'].append(event)
        del state['events'][:-MAX_EVENTS]
        seen = {e['seq'] for e in state['ledger'][-300:]}
        for entry in room.get('ledger', []):
            if isinstance(entry, dict) and type(entry.get('seq')) is int and entry['seq'] not in seen:
                state['ledger'].append(entry)
        del state['ledger'][:-MAX_LEDGER]
        if type(room.get('cursor')) is int:
            state['cursor'] = room['cursor']
        if type(room.get('ledger_cursor')) is int:
            state['ledger_cursor'] = room['ledger_cursor']
        for key in ('agents', 'tasks', 'leases'):
            if isinstance(room.get(key), list):
                state[key] = room[key]
        if room.get('default_agent'):
            state['default_agent'] = room['default_agent']
        fetched = room.get('fetched')
        if isinstance(fetched, dict) and fetched.get('artifact_id'):
            queue = state.get('fetch_queue') or []
            state['fetch_queue'] = [a for a in queue if a != fetched['artifact_id']]
            if fetched.get('data'):
                suffix = '.jpg' if fetched.get('media', {}).get('media_type') == 'image/jpeg' else '.png'
                try:
                    (self.evidence / (fetched['artifact_id'] + suffix)).write_bytes(base64.b64decode(fetched['data']))
                except (OSError, ValueError):
                    pass
            self._fetch_wait = 0.0
        state['last_sync'] = _now()
        self.urgent = bool(room.get('more')) or bool(outbox['messages']) or bool(outbox['controls'])
        self.connection = {'state': 'connected', 'label': 'Connected', 'detail': ''}
        self._queue_missing_evidence()
        self.mark()
        self.save()

    def _cache_upload(self, upload):
        """Keep the user's own attachment as local evidence under its artifact id."""
        source = Path(upload['path'])
        suffix = '.jpg' if upload.get('media_type') == 'image/jpeg' else '.png'
        target = self.evidence / (upload['artifact_id'] + suffix)
        try:
            if source.exists() and not target.exists():
                target.write_bytes(source.read_bytes())
        except OSError:
            pass

    def _queue_missing_evidence(self):
        queue = self.state.setdefault('fetch_queue', [])
        for entry in self.state['ledger'][-120:]:
            for artifact_id in entry.get('artifacts', []) or []:
                if self.evidence_path(artifact_id, entry.get('job_id')) is None and artifact_id not in queue:
                    queue.append(artifact_id)
        del queue[20:]

    def evidence_path(self, artifact_id=None, job_id=None):
        for name in ([f'{artifact_id}.png', f'{artifact_id}.jpg'] if artifact_id else []) + (
                [f'job-{job_id}.png'] if job_id else []):
            path = self.evidence / name
            if path.exists():
                return str(path)
        return None

    def job_started(self, job):
        """Called just before GhostBlender executes a job: recovery must know it was running."""
        self.state['session']['executing'] = {'job_id': job.get('job_id'), 'operation': job.get('operation'),
                                              'started': _now()}
        self.save(force=True)

    def job_executed(self, job, outbox):
        """Keep a local copy of evidence this device produced, keyed by job_id."""
        self.state['session']['executing'] = None
        try:
            result = (outbox or {}).get('result') or {}
            value = result.get('value') if result.get('ok') else None
            if job.get('operation') == 'capture' and isinstance(value, dict) and value.get('data'):
                (self.evidence / f"job-{job['job_id']}.png").write_bytes(base64.b64decode(value['data']))
                self._prune_evidence()
        except (OSError, ValueError, KeyError):
            pass
        self.save(force=True)
        self.mark()

    def _prune_evidence(self, keep=240):
        files = sorted(self.evidence.glob('*.*'), key=lambda p: p.stat().st_mtime)
        for path in files[:-keep]:
            try:
                path.unlink()
            except OSError:
                pass

    def set_connection(self, state, label, detail=''):
        value = {'state': state, 'label': label, 'detail': detail}
        if value != self.connection:
            self.connection = value
            self.mark()

    def poll_interval(self):
        if self.urgent:
            return 0.05
        if any(t.get('state') in ACTIVE_STATES for t in self.state.get('tasks', [])):
            return 0.5
        return 0.6 if self.ui_open else 1.0


# ---------------------------------------------------------------------- presentation

def _agent_names(state):
    return {a['agent_id']: a.get('display_name', a['agent_id']) for a in state.get('agents', [])}


def _origin_name(origin):
    if not origin:
        return 'Someone'
    if origin.get('kind') == 'user':
        return 'You'
    if origin.get('id') == 'legacy-unattributed':
        return 'External agent (GhostBlender Simple)'
    return origin.get('display_name') or origin.get('id', 'Agent')


def _phase_add(card, entry, client):
    label = entry.get('label') or 'Working'
    phases = card['phases']
    job_id = entry.get('job_id')
    phase = next((p for p in reversed(phases[-3:]) if job_id in p['jobs']), None)
    if phase is None:
        if phases and phases[-1]['label'] == label:
            phase = phases[-1]
        else:
            phase = {'label': label, 'kind': entry.get('kind', 'execute'), 'jobs': {}, 'started': entry['time'],
                     'updated': entry['time'], 'failure': None}
            phases.append(phase)
    outcome = entry.get('outcome')
    if job_id:
        phase['jobs'][job_id] = outcome
    phase['updated'] = entry['time']
    if entry.get('failure') and outcome in ('failed', 'uncertain', 'expired'):
        phase['failure'] = entry['failure']
    for artifact_id in entry.get('artifacts', []) or []:
        card['evidence'].append({'artifact_id': artifact_id, 'job_id': job_id, 'label': label,
                                 'path': client.evidence_path(artifact_id, job_id), 'time': entry['time']})
    card['updated'] = max(card.get('updated') or 0, entry['time'])


def _phase_state(phase):
    outcomes = list(phase['jobs'].values())
    if not outcomes:
        return 'done'
    last = outcomes[-1]
    if last in ('requested', 'dispatched', None):
        return 'running'
    if last == 'uncertain':
        return 'uncertain'
    if last in ('failed', 'expired'):
        return 'failed'
    if last == 'cancelled':
        return 'cancelled'
    return 'done'


def _finish_card(card):
    phases = []
    for phase in card['phases']:
        phases.append({'label': phase['label'], 'kind': phase['kind'], 'count': len(phase['jobs']) or 1,
                       'state': _phase_state(phase), 'started': phase['started'], 'updated': phase['updated'],
                       'failure': phase['failure']})
    card['phases'] = phases
    card['operation_count'] = sum(p['count'] for p in phases)
    evidence, seen = [], set()
    for item in card['evidence']:
        key = item['artifact_id']
        if key not in seen:
            seen.add(key)
            evidence.append(item)
    card['evidence'] = evidence[-12:]
    return card


def build_items(client):
    state = client.state
    names = _agent_names(state)
    tasks = {t['task_id']: t for t in state.get('tasks', [])}
    items, cards, external = [], {}, None

    def card_for(task_id, agent_id, time_value, title=None):
        card = cards.get(task_id)
        if card is None:
            task = tasks.get(task_id, {})
            card = {'id': 't' + task_id, 'type': 'task', 'task_id': task_id,
                    'agent_id': agent_id or task.get('agent_id'),
                    'agent': names.get(agent_id or task.get('agent_id'), agent_id or 'Agent'),
                    'title': title or task.get('title') or 'Task', 'state': task.get('state', 'queued'),
                    'mode': task.get('mode', 'do'), 'role': task.get('role', 'primary'),
                    'time': time_value, 'updated': time_value, 'phases': [], 'evidence': [], 'narration': None,
                    'failure': None, 'mutation_possible': False, 'redirects': 0}
            cards[task_id] = card
            items.append(card)
        return card

    stream = [('e', e['seq'], e.get('time', 0), e) for e in state.get('events', [])]
    stream += [('l', e['seq'], e.get('time', 0), e) for e in state.get('ledger', [])]
    stream.sort(key=lambda x: (x[2], x[0], x[1]))
    for source, seq, when, value in stream:
        if source == 'e':
            kind, payload = value.get('type'), value.get('payload') or {}
            task_id, agent_id = value.get('task_id'), value.get('agent_id')
            if kind == 'user':
                steer = payload.get('steer_into')
                items.append({'id': f'e{seq}', 'type': 'user', 'text': value['text'], 'time': when,
                              'agent': names.get(agent_id, agent_id or ''), 'context': payload.get('context', []),
                              'mode': payload.get('mode', 'do'), 'role': payload.get('role', 'primary'),
                              'redirect': bool(steer)})
                if steer and steer in cards:
                    cards[steer]['redirects'] += 1
                elif task_id and not steer:
                    card_for(task_id, agent_id, when, value['text'].split('\n')[0][:80])
            elif kind == 'task' and task_id:
                card = card_for(task_id, agent_id, when, payload.get('title'))
                card['state'] = payload.get('state', card['state'])
                card['updated'] = when
                if payload.get('failure'):
                    card['failure'] = payload['failure']
                if 'mutation_possible' in payload:
                    card['mutation_possible'] = bool(payload['mutation_possible'])
            elif kind == 'status' and task_id:
                card = card_for(task_id, agent_id, when)
                card['narration'] = value['text'][:600]
                card['updated'] = when
            elif kind == 'final':
                items.append({'id': f'e{seq}', 'type': 'agent', 'text': value['text'], 'time': when,
                              'agent_id': agent_id, 'agent': names.get(agent_id, agent_id or 'Agent'),
                              'task_id': task_id})
            elif kind == 'error':
                items.append({'id': f'e{seq}', 'type': 'failure', 'text': value['text'], 'time': when,
                              'agent': names.get(agent_id, agent_id or 'Agent'), 'task_id': task_id,
                              'failure': payload.get('failure')})
            elif kind == 'note':
                items.append({'id': f'e{seq}', 'type': 'note', 'text': value['text'], 'time': when,
                              'agent': names.get(agent_id, agent_id or 'Agent'),
                              'category': payload.get('category', 'summary'),
                              'rationale': payload.get('rationale'), 'handoff': payload.get('handoff')})
            continue
        entry = value
        if entry.get('job_id'):
            task_id = entry.get('task_id')
            if task_id:
                _phase_add(card_for(task_id, None, when), entry, client)
                external = None
            else:
                who = _origin_name(entry.get('origin'))
                if external is None or external['agent'] != who or when - external['updated'] > 900:
                    external = {'id': f'x{seq}', 'type': 'external', 'agent': who, 'title': 'Working through '
                                'GhostBlender', 'time': when, 'updated': when, 'phases': [], 'evidence': [],
                                'state': 'running'}
                    items.append(external)
                _phase_add(external, entry, client)
        elif entry.get('category') == 'lease' and entry.get('lease'):
            lease = entry['lease']
            if lease.get('event') in ('acquired', 'released', 'expired', 'revoked'):
                holder = lease.get('holder', '')
                verb = {'acquired': 'took editing control of the scene', 'released': 'released editing control',
                        'expired': 'editing control expired', 'revoked': 'editing control was revoked'}
                items.append({'id': f'l{seq}', 'type': 'system', 'time': when,
                              'text': f"{_origin_name(entry.get('origin')) if holder else 'Someone'} "
                                      f"{verb[lease['event']]}"})
    for error in state.get('local_errors', [])[-5:]:
        items.append({'id': error['id'], 'type': 'failure', 'text': error['text'], 'time': error['time'],
                      'agent': 'GHOSTroom', 'failure': None})
    for message in state['outbox']['messages']:
        items.append({'id': 'p' + message['id'], 'type': 'user', 'text': message['text'],
                      'time': message.get('created', _now()), 'agent': names.get(message.get('agent_id'), ''),
                      'context': [c['title'] for c in message['context']], 'mode': message['mode'],
                      'role': message['role'], 'pending': True})
    for card in cards.values():
        _finish_card(card)
        task = tasks.get(card['task_id'])
        if task:
            card['state'] = task.get('state', card['state'])
        card['can_stop'] = card['state'] in ('queued', 'running')
    for card in items:
        if card['type'] == 'external':
            _finish_card(card)
            card['state'] = 'running' if any(p['state'] == 'running' for p in card['phases']) else 'done'
    items.sort(key=lambda item: item.get('time', 0))
    return items[-MAX_ITEMS:]


def activity_state(client, items):
    """What is happening right now, in the vocabulary of target.md §15/§16."""
    state = client.state
    names = _agent_names(state)
    running = [c for c in items if c['type'] == 'task' and c['state'] in ('running', 'stopping')]
    queued = [c for c in items if c['type'] == 'task' and c['state'] == 'queued']
    external = [c for c in items if c['type'] == 'external' and c['state'] == 'running']
    if client.state['outbox']['messages']:
        return {'state': 'sending', 'label': 'Sending', 'detail': 'Waiting for the relay', 'agent': ''}
    if running:
        card = running[-1]
        base = {'agent': card['agent'], 'task_id': card['task_id'], 'since': card['time'],
                'can_stop': card['state'] == 'running'}
        if card['state'] == 'stopping':
            return {**base, 'state': 'stopping', 'label': 'Stopping', 'detail': 'Finishing the current step'}
        phase = card['phases'][-1] if card['phases'] else None
        if phase and phase['state'] == 'running':
            key, label = KIND_STATES.get(phase['kind'], ('executing', 'Working'))
            connection = client.connection.get('state')
            if connection != 'connected':
                return {**base, 'state': 'waiting', 'label': 'Waiting for Blender', 'detail': phase['label']}
            return {**base, 'state': key, 'label': label, 'detail': phase['label']}
        if phase and phase['kind'] == 'execute' and phase['state'] == 'done':
            return {**base, 'state': 'planning', 'label': 'Planning next step',
                    'detail': card['narration'] or f"After: {phase['label']}"}
        if phase and phase['kind'] == 'capture':
            return {**base, 'state': 'reviewing', 'label': 'Reviewing the result',
                    'detail': card['narration'] or phase['label']}
        return {**base, 'state': 'thinking', 'label': 'Thinking' if not card['phases'] else 'Working',
                'detail': card['narration'] or card['title']}
    if queued:
        card = queued[-1]
        return {'state': 'queued', 'label': 'Queued', 'detail': card['title'], 'agent': card['agent'],
                'task_id': card['task_id'], 'can_stop': True}
    if external:
        card = external[-1]
        return {'state': 'executing', 'label': card['agent'], 'detail': card['phases'][-1]['label']
                if card['phases'] else 'Working', 'agent': card['agent']}
    last = next((i for i in reversed(items) if i['type'] in ('agent', 'failure', 'task')), None)
    if last and last['type'] == 'failure':
        return {'state': 'failed', 'label': 'Needs attention', 'detail': last['text'][:120],
                'agent': last.get('agent', '')}
    if last and last['type'] == 'agent' and last['text'].rstrip().endswith('?'):
        return {'state': 'needs_input', 'label': 'Waiting for you', 'detail': last['text'][-120:],
                'agent': last.get('agent', '')}
    if last and last['type'] == 'task' and last['state'] == 'stopped':
        return {'state': 'stopped', 'label': 'Stopped', 'detail': last['title'], 'agent': last['agent']}
    return {'state': 'idle', 'label': 'Ready', 'detail': '', 'agent': names.get(
        state['prefs'].get('agent_id') or state.get('default_agent'), '')}


def recovery_info(client):
    """What was in progress when the app last closed, from the ledger and real device state."""
    recovery = client.state.get('recovery')
    if not recovery or recovery.get('dismissed'):
        return None
    tasks = {t['task_id']: t for t in client.state.get('tasks', [])}
    task_id = next(iter(recovery.get('task_ids') or []), None)
    task = tasks.get(task_id, {}) if task_id else {}
    names = _agent_names(client.state)
    entries = [e for e in client.state['ledger'] if task_id and e.get('task_id') == task_id]
    last = next((e for e in reversed(entries) if e.get('label') and e.get('outcome') in (
        'requested', 'completed', 'failed', 'uncertain')), None)
    evidence = next((e for e in reversed(entries) if e.get('artifacts')), None)
    executing = recovery.get('executing')
    runtime = client.runtime_getter()
    interrupted = None
    if runtime is not None:
        for job_id, record in list(getattr(runtime, 'journal', {}).items())[-20:]:
            if record.get('state') == 'uncertain' and record.get('error') == 'app_restarted_during_command':
                interrupted = {'job_id': job_id, 'operation': record.get('operation')}
    if executing and not interrupted:
        interrupted = {'job_id': executing.get('job_id'), 'operation': executing.get('operation')}
    state_now = task.get('state')
    if state_now in ('completed', 'failed', 'stopped', 'uncertain'):
        headline = f"While GHOSTpad was closed, the task {state_now}."
    elif state_now in ACTIVE_STATES:
        headline = 'The agent is still working on the relay.'
    else:
        headline = 'GHOSTpad closed while work was in progress.'
    evidence_path = None
    if evidence:
        artifact_id = evidence['artifacts'][-1]
        evidence_path = client.evidence_path(artifact_id, evidence.get('job_id'))
    return {'title': task.get('title') or 'Previous task', 'task_id': task_id, 'agent_id': task.get('agent_id'),
            'agent': names.get(task.get('agent_id'), task.get('agent_id') or 'Agent'), 'state': state_now,
            'headline': headline,
            'last_activity': (last['label'] + (' (was in progress)' if last.get('outcome') == 'requested' else
                                               f" ({last['outcome']})" if last.get('outcome') != 'completed'
                                               else '')) if last else None,
            'last_time': last.get('time') if last else None,
            'interrupted_operation': interrupted,
            'mutation_possible': bool(interrupted and interrupted.get('operation') in ('execute_python',
                                                                                          'write_script')),
            'evidence_path': evidence_path}


def build_snapshot(client):
    state = client.state
    items = build_items(client)
    prefs = state['prefs']
    agents = []
    for agent in state.get('agents', []):
        reason = agent.get('availability', {}).get('reason')
        agents.append({'id': agent['agent_id'], 'name': agent.get('display_name', agent['agent_id']),
                       'provider': agent.get('provider', ''), 'model': agent.get('model', ''),
                       'state': agent.get('availability', {}).get('state', 'unknown'),
                       'auth': agent.get('auth', {}).get('state', 'unknown'),
                       'quota': agent.get('quota', {}).get('state', 'unknown'),
                       'reason': reason.get('message', '')[:200] if isinstance(reason, dict) else '',
                       'activity': agent.get('activity', ''), 'roles': agent.get('roles', [])})
    selected = prefs.get('agent_id') or state.get('default_agent') or (agents[0]['id'] if agents else None)
    names = _agent_names(state)
    leases = [{'holder': names.get(l['holder'].replace('-embedded', ''), l['holder']),
               'expires_at': l.get('expires_at')} for l in state.get('leases', [])]
    return {
        'v': PROTOCOL, 'rev': client.rev, 'open_request': client.open_requests,
        'connection': client.connection, 'agents': agents,
        'selected': {'agent_id': selected, 'mode': prefs.get('mode', 'do'), 'role': prefs.get('role', 'primary')},
        'modes': [{'id': k, 'label': v} for k, v in MODES], 'roles': [{'id': k, 'label': v} for k, v in ROLES],
        'activity': activity_state(client, items), 'leases': leases, 'items': items,
        'attachments': [{k: a.get(k) for k in ('id', 'kind', 'title', 'subtitle', 'state', 'path')}
                        for a in state['attachments']],
        'recovery': recovery_info(client), 'pending': len(state['outbox']['messages']),
        'last_sync': state.get('last_sync'),
    }
