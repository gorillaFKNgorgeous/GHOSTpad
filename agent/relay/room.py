# SPDX-License-Identifier: GPL-2.0-or-later
"""GHOSTroom relay service: the device-facing room exchange and the agent-neutral brief.

The native GHOSTroom never talks to an agent directly. It rides the existing
authenticated device exchange (`room` next to `chat`), and everything it shows
is read back from durable relay state: tasks, room events and the Shared
Workspace Ledger. Activity is derived from the ledger's real tool calls, not
from extra model chatter.
"""
import base64
import json
import re
import time

from store import LEGACY, NOTE_CATEGORIES, OWNER, ROOM_TEXT, TASK_MODES, TASK_ROLES, rfc3339, task_title

ROOM_PROTOCOL = 1
CONTEXT_KINDS = ('scene', 'selection', 'viewport', 'render', 'image', 'file')
MAX_CONTEXT_TEXT = 12000
MAX_UPLOAD = 2_000_000

# Short, human activity labels for GhostBlender operations. execute_python is
# labelled from the leading comment agents are asked to write, then from what
# the code visibly does.
OPERATION_LABELS = {
    'inspect_scene': 'Inspecting scene',
    'diagnostics': 'Reading diagnostics',
    'list_scripts': 'Reading saved scripts',
    'read_script': 'Reading saved scripts',
}
CODE_HINTS = (
    (r'render\.render\(|ops\.render\.', 'Rendering'),
    (r'save_mainfile|save_as_mainfile|wm\.save', 'Saving the file'),
    (r'primitive_\w+_add|meshes\.new|objects\.new', 'Building geometry'),
    (r'modifiers\.new|modifier_(add|apply)', 'Working on modifiers'),
    (r'materials\.new|node_tree|shader', 'Working on materials'),
    (r'keyframe|fcurves|animation_data', 'Animating'),
    (r'bmesh|\.vertices|\.polygons|edit_mesh|mesh\.', 'Editing mesh'),
    (r'lights?\.new|\.energy|world\.', 'Lighting'),
    (r'camera', 'Setting up the camera'),
    (r'\.location|\.rotation_euler|\.scale|transform\.', 'Transforming objects'),
    (r'cloth|rigid_body|particle|physics', 'Setting up simulation'),
    (r'armature|bones|pose\.', 'Rigging'),
    (r'objects\.remove|\.delete\(', 'Removing objects'),
)


def activity_label(operation, arguments):
    """One short label for a GhostBlender job, for grouping activity into phases."""
    if not isinstance(arguments, dict):
        arguments = {}
    if operation in OPERATION_LABELS:
        return OPERATION_LABELS[operation]
    if operation == 'capture':
        return 'Checking the render' if arguments.get('source') == 'render_result' else 'Capturing the viewport'
    if operation == 'write_script':
        return f"Saving script {str(arguments.get('name', ''))[:60]}".strip()
    if operation != 'execute_python':
        return operation or 'Working'
    code = arguments.get('code') if isinstance(arguments.get('code'), str) else ''
    for line in code.splitlines()[:6]:
        line = line.strip()
        if not line:
            continue
        if line.startswith('#') and not line.startswith('#!'):
            text = re.sub(r'\s+', ' ', line.lstrip('#').strip(' -:*'))
            if 3 <= len(text):
                text = text[0].upper() + text[1:]
                return text[:80]
        break
    for pattern, label in CODE_HINTS:
        if re.search(pattern, code):
            return label
    return 'Working in Blender'


def kind_for(operation):
    if operation == 'execute_python':
        return 'execute'
    if operation == 'capture':
        return 'capture'
    if operation == 'write_script':
        return 'persist'
    return 'inspect'


def compact_ledger_row(row):
    """A ledger entry as GHOSTroom renders it: the entry plus the job it is about."""
    entry = json.loads(row['entry'])
    origin = entry.get('origin', {})
    value = {'seq': row['seq'], 'entry_id': entry['entry_id'], 'time': row['created'],
             'origin': {k: origin[k] for k in ('kind', 'id', 'display_name', 'provider') if k in origin},
             'category': entry['category'], 'summary': entry['summary'][:400]}
    for key in ('outcome', 'artifacts', 'rationale'):
        if key in entry:
            value[key] = entry[key]
    if 'failure' in entry:
        f = entry['failure']
        value['failure'] = {'code': f['code'], 'layer': f['layer'], 'message': f['message'][:400],
                            'mutation_possible': f['mutation_possible'], 'retry': f['retry']}
    if 'lease' in entry:
        lease = entry['lease']
        value['lease'] = {k: lease[k] for k in ('event', 'implicit', 'holder', 'scope_kind', 'expires_at')
                          if k in lease}
    if 'handoff' in entry:
        value['handoff'] = entry['handoff']
    evidence = next((e for e in entry.get('evidence', []) if e.get('type') == 'job'), None)
    if evidence:
        value['job_id'] = evidence['job_id']
        value['operation'] = evidence['operation']
        try:
            arguments = json.loads(row['arguments']) if row['arguments'] else {}
        except ValueError:
            arguments = {}
        value['label'] = activity_label(evidence['operation'], arguments)
        value['kind'] = kind_for(evidence['operation'])
        if row['task_id']:
            value['task_id'] = row['task_id']
    return value


def is_noise(entry):
    """Ledger rows that matter for audit but not for watching the work."""
    if entry['category'] == 'lease':
        lease = entry.get('lease', {})
        return lease.get('implicit', False) or lease.get('event') == 'renewed'
    return entry.get('outcome') == 'dispatched'


class Room:
    """The GHOSTroom side of the relay for one device."""

    def __init__(self, store, device_id, router=None):
        self.store, self.device_id, self.router = store, device_id, router

    # ------------------------------------------------------------------ exchange

    def exchange(self, room):
        if not isinstance(room, dict) or room.get('v') != ROOM_PROTOCOL:
            raise ValueError('invalid_room_payload')
        store, device = self.store, self.device_id
        artifact_acks, message_acks, control_acks, problems = [], [], [], []
        for upload in _list(room.get('artifacts'), 2):
            try:
                artifact_acks.append(self._accept_artifact(upload))
            except (ValueError, TypeError, KeyError) as exc:
                problems.append({'artifact_id': upload.get('artifact_id') if isinstance(upload, dict) else None,
                                 'error': str(exc)[:120]})
        for message in _list(room.get('messages'), 4):
            try:
                outcome = self._accept_message(message)
                message_acks.append({'id': message['id'], 'delivery': outcome})
            except (ValueError, TypeError, KeyError) as exc:
                problems.append({'id': message.get('id') if isinstance(message, dict) else None,
                                 'error': str(exc)[:120]})
        for control in _list(room.get('controls'), 8):
            try:
                control_acks.append({'id': control['id'], 'result': self._accept_control(control)})
            except (ValueError, TypeError, KeyError) as exc:
                control_acks.append({'id': control.get('id') if isinstance(control, dict) else None,
                                     'result': 'rejected', 'error': str(exc)[:120]})

        with store.lock:
            events_issued, ledger_issued = store.chat_events_issued(), store.ledger_issued()
        cursor, reset = _cursor(room.get('cursor'), room.get('stream_id'), store.chat_stream_id, events_issued)
        events = store.room_events(device, cursor, 100)
        ledger_cursor, ledger_reset = _cursor(room.get('ledger_cursor'), room.get('ledger_stream_id'),
                                              store.ledger_stream_id, ledger_issued)
        rows = store.room_ledger(device, ledger_cursor, 120)
        entries = []
        for row in rows:
            entry = json.loads(row['entry'])
            if not is_noise(entry):
                entries.append(compact_ledger_row(row))
        reply = {
            'v': ROOM_PROTOCOL, 'time': time.time(),
            'stream_id': store.chat_stream_id, 'reset': reset,
            'cursor': events[-1]['seq'] if events else cursor, 'events': events,
            'more': len(events) == 100 or len(rows) == 120,
            'ledger_stream_id': store.ledger_stream_id, 'ledger_reset': ledger_reset,
            'ledger_cursor': rows[-1]['seq'] if rows else ledger_cursor, 'ledger': entries,
            'ack_ids': message_acks, 'control_acks': control_acks, 'artifact_acks': artifact_acks,
            'problems': problems,
            'agents': self.router.describe() if self.router else [],
            'default_agent': self.router.default_agent if self.router else None,
            'tasks': [_task_public(task) for task in store.tasks(device, 12)],
            'leases': [lease for lease in store.status(device).get('leases', []) if not lease['implicit']],
        }
        fetch = room.get('fetch')
        if isinstance(fetch, str):
            try:
                reply['fetched'] = store.artifact(device, fetch, with_data=True)
            except ValueError as exc:
                reply['fetched'] = {'artifact_id': fetch, 'error': str(exc)}
        return reply

    def _accept_artifact(self, upload):
        if not isinstance(upload, dict):
            raise ValueError('invalid_artifact')
        artifact_id = upload['artifact_id']
        if not isinstance(artifact_id, str) or not re.fullmatch(r'art[a-f0-9]{32}', artifact_id):
            raise ValueError('invalid_artifact_id')
        kind = upload.get('kind')
        if kind not in ('screenshot', 'render', 'reference'):
            raise ValueError('invalid_artifact_kind')
        data = base64.b64decode(upload['data'], validate=True)
        if len(data) > MAX_UPLOAD:
            raise ValueError('artifact_too_large')
        self.store.store_artifact(self.device_id, OWNER, kind, upload.get('media_type'), data,
                                  upload.get('width'), upload.get('height'),
                                  title=str(upload.get('title') or kind)[:200], artifact_id=artifact_id)
        return artifact_id

    def _accept_message(self, message):
        if not isinstance(message, dict):
            raise ValueError('invalid_room_message')
        agent_id = message.get('agent_id') or (self.router.default_agent if self.router else None)
        if not agent_id:
            raise ValueError('no_agent_configured')
        if self.router and not self.router.has(agent_id):
            raise ValueError('unknown_agent')
        mode, role = message.get('mode', 'do'), message.get('role', 'primary')
        if mode not in TASK_MODES or role not in TASK_ROLES:
            raise ValueError('invalid_task_mode_or_role')
        context = []
        for item in _list(message.get('context'), 8):
            if not isinstance(item, dict) or item.get('kind') not in CONTEXT_KINDS:
                raise ValueError('invalid_context')
            clean = {'kind': item['kind'], 'title': str(item.get('title') or item['kind'])[:80]}
            if isinstance(item.get('text'), str):
                clean['text'] = item['text'][:MAX_CONTEXT_TEXT]
            if isinstance(item.get('artifact_id'), str):
                self.store.artifact(self.device_id, item['artifact_id'])  # must already be uploaded
                clean['artifact_id'] = item['artifact_id']
            context.append(clean)
        steerable = bool(self.router and self.router.can_steer(agent_id))
        return self.store.room_submit(self.device_id, message['id'], message.get('text', ''), agent_id, mode,
                                      role, context, running_steerable=steerable)

    def _accept_control(self, control):
        if not isinstance(control, dict) or not isinstance(control.get('id'), str):
            raise ValueError('invalid_control')
        if control.get('action') == 'stop':
            result = self.store.room_stop(self.device_id, control['task_id'])
            if self.router and result == 'stopping':
                self.router.interrupt(control['task_id'])
            return result
        raise ValueError('unknown_control')


def _list(value, limit):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError('invalid_room_list')
    return value


def _cursor(cursor, stream_id, current_stream, issued):
    if type(cursor) is not int or cursor < 0:
        cursor = 0
    reset = cursor > issued or (stream_id is not None and stream_id != current_stream)
    return (0 if reset else cursor), reset


def _task_public(task):
    return {'task_id': task['task_id'], 'agent_id': task['agent_id'], 'state': task['state'],
            'title': task_title(task['text']), 'mode': task['mode'], 'role': task['role'],
            'created': task['created'], 'started': task['started'], 'finished': task['finished'],
            'read_only': task['read_only'], 'stop_requested': task['stop_requested']}


# ---------------------------------------------------------------------- brief

def workspace_brief(store, device_id, since_seq=0, exclude_participant=None, max_chars=3500, before_seq=None):
    """What happened in the shared workspace, written for an agent joining or resuming.

    Built only from the ledger and room state, so a different agent (or the same
    agent in a fresh thread) can continue without anyone's private memory.
    """
    with store.lock:
        issued = store.ledger_issued()
    # Keep the most recent history when there is a lot of it.
    since_seq = max(since_seq, (before_seq if before_seq is not None else issued + 1) - 401)
    rows = store.room_ledger(device_id, since_seq, 400, before_seq)
    lines, open_problems, notes = [], [], []
    last_phase = None
    for row in rows:
        entry = json.loads(row['entry'])
        if is_noise(entry):
            continue
        who = entry.get('origin', {}).get('display_name') or entry.get('origin', {}).get('id', '?')
        participant = entry.get('lane')
        category = entry['category']
        if exclude_participant and participant == exclude_participant and category not in (
                'instruction', 'failure', 'task'):
            continue
        when = rfc3339(row['created'])[11:16]
        if category in ('instruction', 'response', 'task'):
            lines.append(f'{when} {who} [{category}]: {entry["summary"][:300]}')
            last_phase = None
        elif category in NOTE_CATEGORIES:
            notes.append(f'{when} {who} [{category}]: {entry["summary"][:400]}'
                         + (f' — why: {entry["rationale"][:300]}' if entry.get('rationale') else ''))
        elif row['operation']:
            compact = compact_ledger_row(row)
            outcome = entry.get('outcome')
            if outcome in ('failed', 'uncertain', 'expired'):
                failure = entry.get('failure', {})
                problem = (f'{when} {who}: {compact["label"]} {outcome} ({failure.get("code")})'
                           + (' — Blender may have changed' if failure.get('mutation_possible') else ''))
                open_problems.append(problem)
                lines.append(problem)
            elif outcome == 'completed' and compact['label'] != last_phase:
                lines.append(f'{when} {who}: {compact["label"]}')
                last_phase = compact['label']
        elif category == 'failure':
            lines.append(f'{when} {who} [failure]: {entry["summary"][:200]}')
    if not lines and not notes:
        return ''
    parts = []
    if notes:
        parts.append('Notes left by participants:\n' + '\n'.join(notes[-10:]))
    if open_problems:
        parts.append('Unresolved or uncertain operations (inspect Blender before relying on them):\n'
                     + '\n'.join(open_problems[-6:]))
    parts.append('Recent activity (oldest first):\n' + '\n'.join(lines[-40:]))
    text = '\n\n'.join(parts)
    if len(text) > max_chars:
        text = '…' + text[-max_chars:]
    return text


def task_context_text(task):
    """The user's explicit context attachments, as text an agent can read."""
    blocks = []
    for item in task.get('context', []):
        title = item.get('title') or item.get('kind')
        if item.get('text'):
            blocks.append(f'[{item["kind"]}: {title}]\n{item["text"]}')
        elif item.get('artifact_id'):
            blocks.append(f'[{item["kind"]}: {title}] image artifact {item["artifact_id"]} '
                          '(attached to this message; read_artifact returns it again)')
    return '\n\n'.join(blocks)


MODE_INSTRUCTIONS = {
    'do': 'Mode: Do it for me. You may work autonomously in Blender until the request is done.',
    'with_me': 'Mode: Do it with me. Split the work with the user: do your part, then say clearly '
               'what the user should do next and wait for them.',
    'teach': 'Mode: Teach me. Do not change the scene. Explain one step at a time, tell the user '
             'exactly what to do in Blender, then inspect the live scene to check what they did.',
    'explain': 'Mode: Explain this. Do not change the scene. Inspect the actual current Blender state '
               'and explain it.',
    'review_my_work': 'Mode: Review my work. Do not change the scene. Inspect and capture what the user '
                      'made and give specific, actionable feedback.',
}
ROLE_INSTRUCTIONS = {
    'primary': '',
    'reviewer': 'Role: Reviewer. Check the other participants\' result against the goal. Inspect only; '
                'record your verdict with post_note(category="review").',
    'specialist': 'Role: Specialist. Investigate the specific area asked about and record findings '
                  'with post_note.',
    'critic': 'Role: Critic. Look deliberately for weaknesses and overlooked problems. Inspect only; '
              'record findings with post_note(category="review").',
    'verifier': 'Role: Verifier. Test whether the result meets the target, with visual evidence. '
                'Inspect only; record the verdict with post_note(category="review").',
}


def compose_prompt(task, brief='', steer=False):
    """The text an agent receives for one GHOSTroom task. Provider-neutral."""
    parts = []
    header = [MODE_INSTRUCTIONS.get(task.get('mode') or 'do', '')]
    role = ROLE_INSTRUCTIONS.get(task.get('role') or 'primary')
    if role:
        header.append(role)
    if steer:
        parts.append('[GHOSTroom] The user sent this while you are working. Take it into account now; '
                     'it may redirect the task.')
    else:
        parts.append('[GHOSTroom] ' + ' '.join(h for h in header if h))
    if brief:
        parts.append('[Shared workspace since your last turn — from the ledger]\n' + brief)
    context = task_context_text(task)
    if context:
        parts.append('[Context the user attached]\n' + context)
    parts.append('[User]\n' + task['text'])
    return '\n\n'.join(parts)[:ROOM_TEXT * 2]


def participant_label(store, participant_id):
    for item in store.participants():
        if item['participant_id'] == participant_id:
            return item['display_name']
    return LEGACY if participant_id == LEGACY else participant_id
