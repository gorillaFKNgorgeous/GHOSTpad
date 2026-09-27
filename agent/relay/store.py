# SPDX-License-Identifier: GPL-2.0-or-later
"""Single-device durable relay. All state transitions serialize through SQLite.

Participants, edit leases and the shared workspace ledger live in the same
database as jobs. Every job, lease and participant state change appends its
ledger entry inside the same transaction as the change itself, so the ledger
is authoritative and is never reconstructed from timestamps afterwards
(docs/ghostroom/LEDGER.md).
"""
import datetime
import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
import uuid

OPERATIONS = {'inspect_scene', 'execute_python', 'capture', 'diagnostics',
              'list_scripts', 'read_script', 'write_script'}
# Operations that may change the Blender scene and so need the scene edit lease.
SCENE_MUTATING = {'execute_python'}
# Operations that change the persistent script workspace. They use the separate
# workspace lock, never the scene lease (docs/ghostroom/SCRIPT-WORKSPACE.md).
WORKSPACE_MUTATING = {'write_script'}
KEY = re.compile(r'^[a-zA-Z0-9_-]{8,80}$')
CHAT_KEY = re.compile(r'^[a-f0-9]{32}$')
SHA256 = re.compile(r'^[a-f0-9]{64}$')
PARTICIPANT_ID = re.compile(r'^[a-z0-9][a-z0-9._-]{2,63}$')
PARTICIPANT_KINDS = ('user', 'agent', 'ghostblender', 'relay', 'system')

# The shared GhostBlender Simple capability (and bearer/OAuth access on the full
# relay) cannot tell its callers apart, so all of them are this one participant.
LEGACY = 'legacy-unattributed'

JOB_SECONDS = 90
# An implicit lease covers exactly one submitted job and ends with it, so it
# never outlives the job's own expiry.
IMPLICIT_LEASE_SECONDS = JOB_SECONDS
LEASE_MIN_SECONDS, LEASE_MAX_SECONDS, LEASE_DEFAULT_SECONDS = 10, 600, 120

# Whether something on the device auto-loads the script workspace at launch.
# The bridge itself never does (known from source). The device is 'unknown'
# until the read-only probe in docs/ghostroom/SCRIPT-WORKSPACE.md is run;
# set to 'none' or 'known' from its result.
SCRIPT_DEVICE_AUTO_LOAD = 'unknown'

# Device errors raised before the operation ran (agent/runtime/core.py:119-126).
# These are known not to have executed, so they cannot have changed anything.
NOT_EXECUTED = (
    ('scene_or_session_changed', 'scene_changed', 'bridge', 'after_inspect'),
    ('command_expired', 'tool_timeout', 'bridge', 'safe'),
    ('app_not_foreground', 'blender_suspended', 'bridge', 'after_user_action'),
    ('render_in_progress', 'tool_error', 'blender', 'safe'),
)


def rfc3339(epoch):
    return (datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
            .isoformat(timespec='milliseconds').replace('+00:00', 'Z'))


def sha256_text(value):
    return hashlib.sha256(value.encode()).hexdigest()


def failure(code, layer, message, mutation_possible, retry, **extra):
    """A typed failure (ghostroom/protocol/schemas/failure.schema.json)."""
    value = {'code': code, 'layer': layer, 'message': (message or code)[:16000],
             'mutation_possible': mutation_possible, 'retry': retry}
    value.update({k: v for k, v in extra.items() if v is not None})
    return value


class RelayFailure(ValueError):
    """A rejected request with a typed failure. str() is the JSON sent to MCP clients."""

    def __init__(self, value):
        self.failure = value
        super().__init__(json.dumps({'error': value['code'], 'failure': value}))


def category_for(operation):
    if operation in SCENE_MUTATING:
        return 'mutation'
    if operation in WORKSPACE_MUTATING:
        return 'persistent_code'
    return 'evidence' if operation == 'capture' else 'inspection'


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS device (id TEXT PRIMARY KEY, heartbeat TEXT NOT NULL, seen REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, request_id TEXT NOT NULL,
                digest TEXT NOT NULL, operation TEXT NOT NULL, arguments TEXT NOT NULL,
                boot_id TEXT NOT NULL, scene_id TEXT NOT NULL, state TEXT NOT NULL,
                created REAL NOT NULL, expires REAL NOT NULL, result TEXT,
                UNIQUE(device_id, request_id));
            CREATE INDEX IF NOT EXISTS jobs_device_state ON jobs(device_id, state, created);
            CREATE TABLE IF NOT EXISTS oauth (key TEXT PRIMARY KEY, kind TEXT NOT NULL, value TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS chat_messages (
                device_id TEXT NOT NULL, message_id TEXT NOT NULL, text TEXT NOT NULL,
                state TEXT NOT NULL, created REAL NOT NULL, started REAL, finished REAL,
                PRIMARY KEY(device_id, message_id));
            CREATE INDEX IF NOT EXISTS chat_messages_queue
                ON chat_messages(device_id, state, created);
            CREATE TABLE IF NOT EXISTS chat_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL,
                message_id TEXT, type TEXT NOT NULL, text TEXT NOT NULL, created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS chat_events_device_seq
                ON chat_events(device_id, seq);
            CREATE TABLE IF NOT EXISTS chat_state (
                device_id TEXT PRIMARY KEY, thread_id TEXT, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS relay_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS participants (
                participant_id TEXT PRIMARY KEY, kind TEXT NOT NULL, provider TEXT,
                display_name TEXT NOT NULL, capability_sha256 TEXT UNIQUE,
                created REAL NOT NULL, revoked REAL,
                client_label TEXT, client_label_seen REAL);
            CREATE TABLE IF NOT EXISTS leases (
                lease_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, participant_id TEXT NOT NULL,
                scope_kind TEXT NOT NULL, scene_id TEXT, implicit INTEGER NOT NULL, job_id TEXT,
                acquired REAL NOT NULL, expires REAL NOT NULL, state TEXT NOT NULL,
                ended REAL, reason TEXT);
            CREATE INDEX IF NOT EXISTS leases_active ON leases(device_id, scope_kind, state);
            CREATE TABLE IF NOT EXISTS ledger (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, entry_id TEXT NOT NULL UNIQUE,
                device_id TEXT NOT NULL, created REAL NOT NULL, participant_id TEXT NOT NULL,
                category TEXT NOT NULL, outcome TEXT, job_id TEXT, lease_id TEXT,
                entry TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS ledger_device_seq ON ledger(device_id, seq);
            CREATE INDEX IF NOT EXISTS ledger_job ON ledger(job_id);
        ''')
        # Relay databases created before participants/leases existed: add the
        # columns in place. Old rows are attributed to the legacy participant.
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(jobs)')}
        for column in ('participant_id', 'lease_id'):
            if column not in columns:
                self.db.execute(f'ALTER TABLE jobs ADD COLUMN {column} TEXT')
        self.db.execute('INSERT OR IGNORE INTO relay_meta VALUES (?,?)',
                        ('ledger_stream_id', uuid.uuid4().hex))
        self.db.execute(
            'INSERT OR IGNORE INTO participants(participant_id,kind,provider,display_name,'
            'capability_sha256,created) VALUES (?,?,?,?,NULL,?)',
            (LEGACY, 'agent', None, 'Unattributed (shared legacy capability)', time.time()))
        self.db.commit()
        self.ledger_stream_id = self.db.execute(
            "SELECT value FROM relay_meta WHERE key='ledger_stream_id'").fetchone()[0]
        self._recover_chat()

    def _chat_event_locked(self, device_id, message_id, event_type, text):
        if event_type not in ('status', 'final', 'error') or not isinstance(text, str):
            raise ValueError('invalid_chat_event')
        text = text.strip()
        if not text or len(text) > 2000:
            raise ValueError('invalid_chat_event_text')
        self.db.execute(
            'INSERT INTO chat_events(device_id,message_id,type,text,created) VALUES (?,?,?,?,?)',
            (device_id, message_id, event_type, text, time.time()),
        )

    def _recover_chat(self):
        # A running model turn may already have changed Blender. Never replay it
        # automatically after a relay restart.
        with self.lock, self.db:
            rows = self.db.execute(
                "SELECT device_id,message_id FROM chat_messages WHERE state='running'"
            ).fetchall()
            for row in rows:
                self.db.execute(
                    "UPDATE chat_messages SET state='uncertain', finished=? "
                    "WHERE device_id=? AND message_id=?",
                    (time.time(), row['device_id'], row['message_id']),
                )
                self._chat_event_locked(
                    row['device_id'],
                    row['message_id'],
                    'error',
                    'Previous AI turn was interrupted and was not replayed automatically.',
                )
                self.db.execute(
                    'DELETE FROM chat_state WHERE device_id=?',
                    (row['device_id'],),
                )

    # ------------------------------------------------------------------ participants

    def register_participant(self, participant_id, kind, display_name, provider=None):
        """Create a participant and return its new capability. Only a digest is stored."""
        self._check_participant_fields(participant_id, kind, display_name, provider)
        capability = secrets.token_urlsafe(32)
        with self.lock, self.db:
            row = self.db.execute('SELECT revoked FROM participants WHERE participant_id=?',
                                  (participant_id,)).fetchone()
            if row is not None and row['revoked'] is None:
                raise ValueError('participant_exists; rotate its capability instead')
            self.db.execute(
                'INSERT INTO participants(participant_id,kind,provider,display_name,capability_sha256,created) '
                'VALUES (?,?,?,?,?,?) ON CONFLICT(participant_id) DO UPDATE SET kind=excluded.kind, '
                'provider=excluded.provider, display_name=excluded.display_name, '
                'capability_sha256=excluded.capability_sha256, revoked=NULL',
                (participant_id, kind, provider, display_name, sha256_text(capability), time.time()))
        return capability

    def ensure_participant_capability(self, participant_id, kind, display_name, provider=None):
        """Create or update a service participant and give it a fresh capability.

        Used for the relay's own embedded worker: its capability is regenerated at
        every relay start and only a digest is persisted.
        """
        self._check_participant_fields(participant_id, kind, display_name, provider)
        capability = secrets.token_urlsafe(32)
        with self.lock, self.db:
            self.db.execute(
                'INSERT INTO participants(participant_id,kind,provider,display_name,capability_sha256,created) '
                'VALUES (?,?,?,?,?,?) ON CONFLICT(participant_id) DO UPDATE SET kind=excluded.kind, '
                'provider=excluded.provider, display_name=excluded.display_name, '
                'capability_sha256=excluded.capability_sha256, revoked=NULL',
                (participant_id, kind, provider, display_name, sha256_text(capability), time.time()))
        return capability

    def rotate_capability(self, participant_id):
        capability = secrets.token_urlsafe(32)
        with self.lock, self.db:
            changed = self.db.execute(
                'UPDATE participants SET capability_sha256=? WHERE participant_id=? AND revoked IS NULL '
                'AND participant_id<>?', (sha256_text(capability), participant_id, LEGACY))
            if changed.rowcount != 1:
                raise ValueError('participant_not_found')
        return capability

    def revoke_participant(self, participant_id):
        with self.lock, self.db:
            changed = self.db.execute(
                'UPDATE participants SET revoked=?, capability_sha256=NULL WHERE participant_id=? '
                'AND revoked IS NULL AND participant_id<>?', (time.time(), participant_id, LEGACY))
            if changed.rowcount != 1:
                raise ValueError('participant_not_found')

    def participant_for_capability(self, capability):
        """Resolve a presented capability to its participant_id, or None."""
        if not isinstance(capability, str) or not 16 <= len(capability) <= 256:
            return None
        with self.lock:
            row = self.db.execute(
                'SELECT participant_id FROM participants WHERE capability_sha256=? AND revoked IS NULL',
                (sha256_text(capability),)).fetchone()
        return row['participant_id'] if row else None

    def note_client_label(self, participant_id, label):
        """Keep MCP clientInfo as an unverified display label. It never grants identity."""
        if not isinstance(label, str):
            return
        label = re.sub(r'[\x00-\x1f\x7f]', ' ', label).strip()[:80]
        if not label:
            return
        with self.lock, self.db:
            self.db.execute('UPDATE participants SET client_label=?, client_label_seen=? WHERE participant_id=?',
                            (label, time.time(), participant_id))

    def participants(self):
        with self.lock:
            rows = self.db.execute(
                'SELECT participant_id,kind,provider,display_name,created,revoked,client_label,'
                'client_label_seen FROM participants ORDER BY participant_id').fetchall()
        return [{**dict(row), 'client_label_unverified': row['client_label']} for row in rows]

    @staticmethod
    def _check_participant_fields(participant_id, kind, display_name, provider):
        if not isinstance(participant_id, str) or not PARTICIPANT_ID.fullmatch(participant_id):
            raise ValueError('invalid_participant_id')
        if participant_id == LEGACY:
            raise ValueError('reserved_participant_id')
        if kind not in PARTICIPANT_KINDS:
            raise ValueError('invalid_participant_kind')
        if not isinstance(display_name, str) or not 1 <= len(display_name.strip()) <= 200:
            raise ValueError('invalid_display_name')
        if provider is not None and not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,63}', provider):
            raise ValueError('invalid_provider')

    def _origin(self, participant_id):
        row = self.db.execute('SELECT kind,provider,display_name FROM participants WHERE participant_id=?',
                              (participant_id,)).fetchone()
        if row is None:
            return {'kind': 'agent', 'id': participant_id}
        origin = {'kind': row['kind'], 'id': participant_id, 'display_name': row['display_name'][:200]}
        if row['provider']:
            origin['provider'] = row['provider']
        return origin

    # ------------------------------------------------------------------ ledger

    def _ledger(self, device_id, participant_id, category, summary, *, outcome=None, job=None,
                job_state=None, result=None, lease_id=None, scene_id=None, failure_value=None,
                supersedes=None, persistent_code=None, lease=None, rationale=None):
        """Append one ledger entry. Callers are always inside the state change's transaction."""
        now = time.time()
        entry = {'entry_id': uuid.uuid4().hex, 'timestamp': rfc3339(now),
                 'origin': self._origin(participant_id), 'lane': participant_id,
                 'category': category, 'summary': summary[:2000]}
        if outcome:
            entry['outcome'] = outcome
        if rationale:
            entry['rationale'] = rationale[:4000]
        if job is not None:
            entry['evidence'] = [self._job_evidence(job, job_state, result)]
        if scene_id:
            entry['scene_id'] = scene_id
        if lease_id:
            entry['lease_id'] = lease_id
        if failure_value:
            entry['failure'] = failure_value
        if supersedes:
            entry['supersedes'] = supersedes
        if persistent_code:
            entry['persistent_code'] = persistent_code
        if lease:
            entry['lease'] = lease
        self.db.execute(
            'INSERT INTO ledger(entry_id,device_id,created,participant_id,category,outcome,job_id,lease_id,entry) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            (entry['entry_id'], device_id, now, participant_id, category, outcome,
             job['job_id'] if job is not None else None,
             lease_id or (lease or {}).get('lease_id'), json.dumps(entry, allow_nan=False)))
        return entry['entry_id']

    @staticmethod
    def _job_evidence(row, state=None, result=None):
        """Reference a job by id and digests. Never copies its arguments or result."""
        evidence = {'type': 'job', 'job_id': row['job_id'], 'operation': row['operation'],
                    'state': state or row['state'], 'relay_digest': row['digest'],
                    'request_id': row['request_id'], 'scene_id': row['scene_id'],
                    'boot_id': row['boot_id']}
        if result is not None:
            evidence['result_sha256'] = sha256_text(result)
        return evidence

    @staticmethod
    def _persistent_code(row, result_value=None):
        arguments = json.loads(row['arguments'])
        code, previous = arguments.get('code'), arguments.get('expected_sha256')
        value = {'risk': True, 'script': arguments.get('name'),
                 'replaces_existing': previous is not None, 'previous_sha256': previous,
                 'resulting_sha256': sha256_text(code) if isinstance(code, str) else None,
                 'confirmed_by_device': False,
                 'loaded_by_bridge_at_startup': False,
                 'device_auto_load': SCRIPT_DEVICE_AUTO_LOAD,
                 'request_id': row['request_id'], 'job_id': row['job_id']}
        if isinstance(result_value, dict) and SHA256.fullmatch(str(result_value.get('sha256', ''))):
            value['resulting_sha256'] = result_value['sha256']
            value['confirmed_by_device'] = True
        return value

    def _job_entry(self, device_id, row, outcome, summary, *, state=None, result=None,
                   failure_value=None, supersedes=None, result_value=None):
        participant_id = row['participant_id'] or LEGACY
        return self._ledger(
            device_id, participant_id, category_for(row['operation']), summary, outcome=outcome,
            job=row, job_state=state, result=result, lease_id=row['lease_id'],
            scene_id=row['scene_id'], failure_value=failure_value, supersedes=supersedes,
            persistent_code=(self._persistent_code(row, result_value)
                             if row['operation'] in WORKSPACE_MUTATING else None))

    def read_ledger(self, device_id, after_seq=0, limit=50, stream_id=None):
        if type(after_seq) is not int or after_seq < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('invalid_ledger_cursor')
        with self.lock:
            row = self.db.execute("SELECT seq FROM sqlite_sequence WHERE name='ledger'").fetchone()
            issued = int(row[0]) if row else 0
            reset = after_seq > issued or (stream_id is not None and stream_id != self.ledger_stream_id)
            if reset:
                after_seq = 0
            rows = self.db.execute(
                'SELECT seq, entry FROM ledger WHERE device_id=? AND seq>? ORDER BY seq LIMIT ?',
                (device_id, after_seq, limit)).fetchall()
        entries = []
        for row in rows:
            entry = json.loads(row['entry'])
            entry['seq'] = {'stream_id': self.ledger_stream_id, 'seq': row['seq']}
            entries.append(entry)
        return {'stream_id': self.ledger_stream_id, 'reset': reset, 'entries': entries,
                'cursor': rows[-1]['seq'] if rows else after_seq}

    # ------------------------------------------------------------------ leases

    def _active_leases(self, device_id, scope_kind, now):
        return self.db.execute(
            "SELECT * FROM leases WHERE device_id=? AND scope_kind=? AND state='active' AND expires>? "
            'ORDER BY acquired', (device_id, scope_kind, now)).fetchall()

    @staticmethod
    def _lease_view(row, event, **extra):
        value = {'lease_id': row['lease_id'], 'event': event, 'implicit': bool(row['implicit']),
                 'scope_kind': row['scope_kind'], 'holder': row['participant_id'],
                 'acquired_at': rfc3339(row['acquired']), 'expires_at': rfc3339(row['expires'])}
        if row['scene_id']:
            value['scene_id'] = row['scene_id']
        if row['job_id']:
            value['job_id'] = row['job_id']
        value.update({k: v for k, v in extra.items() if v is not None})
        return value

    def _start_lease(self, device_id, participant_id, scope_kind, scene_id, implicit, seconds,
                     job_id=None, lease_id=None):
        now = time.time()
        lease_id = lease_id or uuid.uuid4().hex
        self.db.execute(
            'INSERT INTO leases(lease_id,device_id,participant_id,scope_kind,scene_id,implicit,job_id,'
            "acquired,expires,state) VALUES (?,?,?,?,?,?,?,?,?,'active')",
            (lease_id, device_id, participant_id, scope_kind, scene_id, int(implicit), job_id, now, now + seconds))
        row = self.db.execute('SELECT * FROM leases WHERE lease_id=?', (lease_id,)).fetchone()
        kind = 'implicit ' if implicit else ''
        self._ledger(device_id, participant_id, 'lease',
                     f'{participant_id} acquired {kind}{scope_kind} lease {lease_id}'
                     + (f' for job {job_id}' if job_id else ''),
                     lease_id=lease_id, scene_id=scene_id, lease=self._lease_view(row, 'acquired'))
        return row

    def _end_lease(self, row, state, reason, actor=None):
        """End an active lease and ledger it. No-op if it already ended."""
        changed = self.db.execute(
            "UPDATE leases SET state=?, ended=?, reason=? WHERE lease_id=? AND state='active'",
            (state, time.time(), reason, row['lease_id']))
        if changed.rowcount != 1:
            return
        event = {'released': 'released', 'expired': 'expired', 'revoked': 'revoked'}[state]
        self._ledger(row['device_id'], actor or row['participant_id'], 'lease',
                     f"{row['scope_kind']} lease {row['lease_id']} held by {row['participant_id']} {event}: {reason}",
                     lease_id=row['lease_id'], scene_id=row['scene_id'],
                     lease=self._lease_view(row, event, reason=reason))

    def _end_job_lease(self, job, reason):
        """End the implicit lease that covered only this job."""
        if not job['lease_id']:
            return
        lease = self.db.execute('SELECT * FROM leases WHERE lease_id=? AND implicit=1 AND job_id=?',
                                (job['lease_id'], job['job_id'])).fetchone()
        if lease is not None:
            self._end_lease(lease, 'released', reason)

    def _reject(self, device_id, participant_id, code, message, *, conflicting=None,
                presented_lease=None, request_id=None, operation=None):
        """Ledger a rejected request and return its typed failure. No state changes."""
        value = failure(code, 'relay', message, False, 'safe')
        lease = None
        if conflicting is not None:
            lease = self._lease_view(conflicting, 'rejected', requested_by=participant_id,
                                     request_id=request_id, operation=operation)
        elif isinstance(presented_lease, str) and KEY.fullmatch(presented_lease):
            lease = {'lease_id': presented_lease, 'event': 'rejected', 'requested_by': participant_id}
            if request_id:
                lease['request_id'] = request_id
            if operation:
                lease['operation'] = operation
        self._ledger(device_id, participant_id, 'lease' if lease else 'failure',
                     f'{participant_id}: {operation or "request"} rejected ({code})',
                     failure_value=value, lease=lease,
                     lease_id=lease['lease_id'] if lease else None)
        return value

    def _scene_lease_for_job(self, device_id, participant_id, scene_id, lease_id, request_id, operation):
        """Decide which scene lease a mutating job runs under.

        Returns (lease_row, needs_implicit, rejection).
        """
        now = time.time()
        active = self._active_leases(device_id, 'scene', now)
        others = [lease for lease in active if lease['participant_id'] != participant_id]
        if lease_id is not None:
            lease = self.db.execute('SELECT * FROM leases WHERE lease_id=? AND device_id=?',
                                    (lease_id, device_id)).fetchone() if isinstance(lease_id, str) else None
            problem = None
            if lease is None:
                problem = 'unknown lease_id'
            elif lease['participant_id'] != participant_id:
                problem = 'lease is held by another participant'
            elif lease['implicit']:
                problem = 'implicit leases cannot be presented'
            elif lease['state'] != 'active' or lease['expires'] <= now:
                problem = 'lease is no longer active'
            elif lease['scene_id'] != scene_id:
                problem = 'lease is for a different scene'
            if problem:
                return None, False, self._reject(
                    device_id, participant_id, 'lease_invalid', problem, presented_lease=lease_id,
                    request_id=request_id, operation=operation)
            return lease, False, None
        if others:
            holder = others[0]
            return None, False, self._reject(
                device_id, participant_id, 'lease_conflict',
                f"{holder['participant_id']} holds the scene edit lease {holder['lease_id']} "
                f"until {rfc3339(holder['expires'])}",
                conflicting=holder, request_id=request_id, operation=operation)
        own = [lease for lease in active if lease['participant_id'] == participant_id
               and not lease['implicit'] and lease['scene_id'] == scene_id]
        if own:
            return own[0], False, None
        return None, True, None

    def acquire_lease(self, device_id, participant_id, scene_id, seconds=LEASE_DEFAULT_SECONDS):
        """Acquire or renew an explicit scene edit lease for this participant."""
        if type(seconds) not in (int, float) or not LEASE_MIN_SECONDS <= seconds <= LEASE_MAX_SECONDS:
            raise ValueError('invalid_lease_duration')
        rejection = None
        with self.lock, self.db:
            status = self.status(device_id)
            if status.get('scene_id') != scene_id:
                raise ValueError('scene_changed; call status and inspect_scene again')
            now = time.time()
            active = self._active_leases(device_id, 'scene', now)
            others = [lease for lease in active if lease['participant_id'] != participant_id]
            own = [lease for lease in active if lease['participant_id'] == participant_id
                   and not lease['implicit'] and lease['scene_id'] == scene_id]
            if others:
                holder = others[0]
                rejection = self._reject(
                    device_id, participant_id, 'lease_conflict',
                    f"{holder['participant_id']} holds the scene edit lease {holder['lease_id']} "
                    f"until {rfc3339(holder['expires'])}", conflicting=holder, operation='acquire_lease')
            elif own:
                lease_id = own[0]['lease_id']
                self.db.execute('UPDATE leases SET expires=? WHERE lease_id=?', (now + seconds, lease_id))
                row = self.db.execute('SELECT * FROM leases WHERE lease_id=?', (lease_id,)).fetchone()
                self._ledger(device_id, participant_id, 'lease',
                             f'{participant_id} renewed scene lease {lease_id}', lease_id=lease_id,
                             scene_id=scene_id, lease=self._lease_view(row, 'renewed'))
            else:
                lease_id = self._start_lease(device_id, participant_id, 'scene', scene_id, False, seconds)['lease_id']
        if rejection:
            raise RelayFailure(rejection)
        return self.lease(device_id, lease_id)

    def release_lease(self, device_id, participant_id, lease_id):
        rejection = None
        with self.lock, self.db:
            self._expire(device_id)
            lease = self.db.execute('SELECT * FROM leases WHERE lease_id=? AND device_id=?',
                                    (lease_id, device_id)).fetchone() if isinstance(lease_id, str) else None
            if (lease is None or lease['participant_id'] != participant_id or lease['implicit']
                    or lease['state'] != 'active'):
                rejection = self._reject(device_id, participant_id, 'lease_invalid',
                                         'only the holder can release its active explicit lease',
                                         presented_lease=lease_id, operation='release_lease')
            else:
                self._end_lease(lease, 'released', 'released by holder')
        if rejection:
            raise RelayFailure(rejection)
        return self.lease(device_id, lease_id)

    def lease(self, device_id, lease_id):
        with self.lock:
            row = self.db.execute('SELECT * FROM leases WHERE lease_id=? AND device_id=?',
                                  (lease_id, device_id)).fetchone()
        if row is None:
            raise ValueError('lease_not_found')
        return self._lease_public(row)

    @staticmethod
    def _lease_public(row):
        value = {'lease_id': row['lease_id'], 'holder': row['participant_id'],
                 'scope_kind': row['scope_kind'], 'scene_id': row['scene_id'],
                 'implicit': bool(row['implicit']), 'job_id': row['job_id'],
                 'acquired_at': rfc3339(row['acquired']), 'expires_at': rfc3339(row['expires']),
                 'state': row['state']}
        if row['reason']:
            value['reason'] = row['reason']
        return value

    # ------------------------------------------------------------------ jobs

    def _result_failure(self, operation, result):
        """Outcome and typed failure for a device result with ok=false."""
        error = str(result.get('error', '')) or 'device reported failure'
        mutating = operation in SCENE_MUTATING or operation in WORKSPACE_MUTATING
        for prefix, code, layer, retry in NOT_EXECUTED:
            if error.startswith(prefix):
                return 'failed', failure(code, layer, error, False, retry, legacy_error=error[:2000])
        if error.startswith('duplicate_command_not_reexecuted'):
            # The device had already run this job once; that first outcome never
            # reached the relay, so success or failure is unknown.
            if mutating:
                return 'uncertain', failure(
                    'interrupted_after_possible_mutation', 'bridge',
                    'The device had already run this job; its original outcome was never delivered.',
                    True, 'after_inspect', legacy_error=error[:2000])
            return 'failed', failure('tool_error', 'bridge', error, False, 'safe', legacy_error=error[:2000])
        if operation in SCENE_MUTATING:
            code = 'tool_timeout' if 'time limit reached' in error else 'tool_error'
            # Python errors and timeouts can leave partial edits (core.py:175-194).
            return 'failed', failure(code, 'tool', error, True, 'after_inspect', legacy_error=error[:2000])
        # write_script replaces the file atomically as its last step, so a
        # reported error means the file was not changed.
        return 'failed', failure('tool_error', 'tool', error, False, 'safe', legacy_error=error[:2000])

    def _expire(self, device_id):
        now = time.time()
        for row in self.db.execute(
                "SELECT * FROM jobs WHERE device_id=? AND state='queued' AND expires<?",
                (device_id, now)).fetchall():
            self.db.execute("UPDATE jobs SET state='expired' WHERE job_id=? AND state='queued'", (row['job_id'],))
            self._job_entry(device_id, row, 'expired',
                            f"{row['operation']} {row['job_id']} expired before delivery; it never ran",
                            state='expired',
                            failure_value=failure('tool_timeout', 'relay',
                                                  'Job expired in the relay queue before the device took it.',
                                                  False, 'safe', legacy_error='command_expired'))
            self._end_job_lease(row, 'job expired before delivery')
        # Delivered commands may already have changed Blender. Never requeue them.
        for row in self.db.execute(
                "SELECT * FROM jobs WHERE device_id=? AND state='issued' AND expires<?",
                (device_id, now)).fetchall():
            self.db.execute("UPDATE jobs SET state='uncertain' WHERE job_id=? AND state='issued'", (row['job_id'],))
            if row['operation'] in SCENE_MUTATING or row['operation'] in WORKSPACE_MUTATING:
                value = failure('interrupted_after_possible_mutation', 'relay',
                                'Job was delivered but no result arrived before it expired; it may have run.',
                                True, 'after_inspect')
            else:
                value = failure('tool_timeout', 'relay',
                                'Job was delivered but no result arrived before it expired.', False, 'safe')
            self._job_entry(device_id, row, 'uncertain',
                            f"{row['operation']} {row['job_id']} outcome unknown: delivered, no result before expiry",
                            state='uncertain', failure_value=value)
            self._end_job_lease(row, 'job outcome unknown at expiry')
        for lease in self.db.execute(
                "SELECT * FROM leases WHERE device_id=? AND state='active' AND expires<=?",
                (device_id, now)).fetchall():
            self._end_lease(lease, 'expired', 'lease expiry reached')
        self.db.execute('DELETE FROM oauth WHERE expires<?', (now,))
        # Keep metadata/idempotency for seven days, images/results for one day.
        # The ledger is never pruned: it references jobs by id and digest only.
        self.db.execute('UPDATE jobs SET result=NULL WHERE created<? AND result IS NOT NULL', (now-86400,))
        self.db.execute("DELETE FROM jobs WHERE created<? AND state NOT IN ('queued','issued')", (now-7*86400,))
        self.db.execute('DELETE FROM chat_events WHERE created<?', (now-7*86400,))
        self.db.execute(
            "DELETE FROM chat_messages WHERE created<? AND state NOT IN ('queued','running')",
            (now-7*86400,),
        )

    def status(self, device_id):
        with self.lock, self.db:
            self._expire(device_id)
            leases = [self._lease_public(row) for row in self.db.execute(
                "SELECT * FROM leases WHERE device_id=? AND state='active' ORDER BY acquired",
                (device_id,)).fetchall()]
            row = self.db.execute('SELECT * FROM device WHERE id=?', (device_id,)).fetchone()
            if not row:
                return {'online': False, 'reason': 'device_not_paired', 'leases': leases}
            heartbeat = json.loads(row['heartbeat'])
            return {**heartbeat, 'online': time.time() - row['seen'] < 12 and heartbeat['native']['foreground'],
                    'last_seen': row['seen'], 'leases': leases}

    def submit(self, device_id, operation, arguments, request_id, scene_id,
               participant_id=LEGACY, lease_id=None):
        if operation not in OPERATIONS or not KEY.fullmatch(request_id):
            raise ValueError('invalid_operation_or_request_id')
        if lease_id is not None and operation not in SCENE_MUTATING:
            raise ValueError('lease_id_only_applies_to_execute_python')
        if operation in WORKSPACE_MUTATING:
            expected = arguments.get('expected_sha256')
            if expected is not None and (not isinstance(expected, str) or not SHA256.fullmatch(expected)):
                raise ValueError('invalid_expected_sha256')
        payload = json.dumps(arguments, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if len(payload.encode()) > 110_000:
            raise ValueError('arguments_too_large')
        digest = hashlib.sha256((operation + '\n' + scene_id + '\n' + payload).encode()).hexdigest()
        rejection = None
        job_id = None
        with self.lock, self.db:
            old = self.db.execute('SELECT * FROM jobs WHERE device_id=? AND request_id=?', (device_id, request_id)).fetchone()
            if old:
                if old['digest'] != digest:
                    raise ValueError('idempotency_key_reused_with_different_arguments')
                if (old['participant_id'] or LEGACY) != participant_id:
                    raise ValueError('idempotency_key_owned_by_another_participant')
                return self._public(old)
            status = self.status(device_id)
            if not status['online']:
                raise ValueError('device_offline_or_suspended')
            if status['scene_id'] != scene_id:
                raise ValueError('scene_changed; call status and inspect_scene again')
            active = self.db.execute("SELECT count(*) FROM jobs WHERE device_id=? AND state IN ('queued','issued')", (device_id,)).fetchone()[0]
            if active >= 8:
                raise ValueError('device_queue_full')
            lease, implicit_scope = None, None
            if operation in SCENE_MUTATING:
                lease, needs_implicit, rejection = self._scene_lease_for_job(
                    device_id, participant_id, scene_id, lease_id, request_id, operation)
                implicit_scope = 'scene' if needs_implicit else None
            elif operation in WORKSPACE_MUTATING:
                busy = self._active_leases(device_id, 'script_workspace', time.time())
                if busy:
                    rejection = self._reject(
                        device_id, participant_id, 'script_workspace_busy',
                        f"script workspace write {busy[0]['job_id']} by {busy[0]['participant_id']} is still pending",
                        conflicting=busy[0], request_id=request_id, operation=operation)
                else:
                    implicit_scope = 'script_workspace'
            if rejection is None:
                now = time.time()
                job_id = uuid.uuid4().hex
                self.db.execute(
                    'INSERT INTO jobs(job_id,device_id,request_id,digest,operation,arguments,boot_id,scene_id,'
                    'state,created,expires,result,participant_id,lease_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (job_id, device_id, request_id, digest, operation, payload, status['boot_id'], scene_id,
                     'queued', now, now + JOB_SECONDS, None, participant_id,
                     lease['lease_id'] if lease is not None else None))
                if implicit_scope:
                    implicit = self._start_lease(
                        device_id, participant_id, implicit_scope,
                        scene_id if implicit_scope == 'scene' else None, True, IMPLICIT_LEASE_SECONDS,
                        job_id=job_id, lease_id='implicit-' + job_id)
                    self.db.execute('UPDATE jobs SET lease_id=? WHERE job_id=?', (implicit['lease_id'], job_id))
                row = self.db.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
                self._job_entry(device_id, row, 'requested',
                                f"{participant_id} requested {operation} {job_id}"
                                + (f" under lease {row['lease_id']}" if row['lease_id'] else ''))
        if rejection is not None:
            raise RelayFailure(rejection)
        return self.result(device_id, job_id)

    def exchange(self, device_id, heartbeat, completed=None):
        if not isinstance(heartbeat, dict) or any(not isinstance(heartbeat.get(k), str) for k in ('boot_id','scene_id','blender_version')):
            raise ValueError('invalid_heartbeat')
        if not isinstance(heartbeat.get('native', {}).get('foreground'), bool):
            raise ValueError('invalid_foreground_state')
        heartbeat_json = json.dumps(heartbeat, allow_nan=False)
        if len(heartbeat_json) > 16000:
            raise ValueError('heartbeat_too_large')
        with self.lock, self.db:
            self._expire(device_id)
            self.db.execute('INSERT INTO device VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET heartbeat=excluded.heartbeat, seen=excluded.seen',
                            (device_id, heartbeat_json, time.time()))
            ack = None
            if completed:
                row = self.db.execute('SELECT * FROM jobs WHERE device_id=? AND job_id=?', (device_id, completed['job_id'])).fetchone()
                if row and row['boot_id'] == completed.get('boot_id') and row['state'] in ('issued','uncertain','completed','failed'):
                    result = completed['result']
                    if not isinstance(result.get('ok'), bool):
                        raise ValueError('invalid_result')
                    if row['state'] in ('issued', 'uncertain'):
                        encoded = json.dumps(result, allow_nan=False)
                        if len(encoded.encode()) > 2_500_000:
                            raise ValueError('result_too_large')
                        state = 'completed' if result['ok'] else 'failed'
                        self.db.execute('UPDATE jobs SET state=?, result=? WHERE job_id=?',
                                        (state, encoded, row['job_id']))
                        self._record_result(device_id, row, result, encoded, state)
                    ack = row['job_id']
                elif row is None:
                    # Old results can outlive the retention period; clear the device outbox.
                    ack = completed['job_id']
            for row in self.db.execute(
                    "SELECT * FROM jobs WHERE device_id=? AND state='queued' AND (boot_id<>? OR scene_id<>?)",
                    (device_id, heartbeat['boot_id'], heartbeat['scene_id'])).fetchall():
                self.db.execute("UPDATE jobs SET state='expired' WHERE job_id=? AND state='queued'", (row['job_id'],))
                self._job_entry(device_id, row, 'expired',
                                f"{row['operation']} {row['job_id']} dropped before delivery: the scene or app session changed",
                                state='expired',
                                failure_value=failure('scene_changed', 'relay',
                                                      'The scene or app session changed before the job was delivered.',
                                                      False, 'after_inspect'))
                self._end_job_lease(row, 'job dropped: scene changed')
            # Explicit scene leases only make sense for the scene they were taken on.
            for lease in self.db.execute(
                    "SELECT * FROM leases WHERE device_id=? AND state='active' AND scope_kind='scene' "
                    'AND implicit=0 AND scene_id<>?', (device_id, heartbeat['scene_id'])).fetchall():
                self._end_lease(lease, 'expired', 'scene changed')
            job = None
            if heartbeat['native']['foreground'] and (not completed or ack):
                # One issued command per device; it is never issued a second time.
                active = self.db.execute("SELECT 1 FROM jobs WHERE device_id=? AND state='issued'", (device_id,)).fetchone()
                row = None if active else self.db.execute("SELECT * FROM jobs WHERE device_id=? AND state='queued' ORDER BY created LIMIT 1", (device_id,)).fetchone()
                if row:
                    self.db.execute("UPDATE jobs SET state='issued' WHERE job_id=?", (row['job_id'],))
                    if row['operation'] in SCENE_MUTATING or row['operation'] in WORKSPACE_MUTATING:
                        # Dispatch is not success; the outcome is recorded when a result arrives.
                        self._job_entry(device_id, row, 'dispatched',
                                        f"{row['operation']} {row['job_id']} delivered to the device",
                                        state='issued')
                    job = {'job_id': row['job_id'], 'operation': row['operation'],
                           'arguments': json.loads(row['arguments']), 'boot_id': row['boot_id'],
                           'scene_id': row['scene_id'], 'expires_at': row['expires']}
            return {'protocol': 1, 'ack': ack, 'job': job}

    def _record_result(self, device_id, row, result, encoded, state):
        """Ledger the device-reported outcome of a job, in the result's transaction."""
        supersedes = None
        if row['state'] == 'uncertain':
            earlier = self.db.execute(
                "SELECT entry_id FROM ledger WHERE job_id=? AND outcome='uncertain' ORDER BY seq DESC LIMIT 1",
                (row['job_id'],)).fetchone()
            supersedes = earlier['entry_id'] if earlier else None
        late = ' (late result; supersedes the uncertain entry)' if supersedes else ''
        if result['ok']:
            # The device ran the job to completion and reported success for this
            # exact job_id and boot_id. That report is the only evidence of success.
            self._job_entry(device_id, row, 'completed',
                            f"{row['operation']} {row['job_id']} completed on the device{late}",
                            state=state, result=encoded, supersedes=supersedes,
                            result_value=result.get('value'))
        else:
            outcome, value = self._result_failure(row['operation'], result)
            self._job_entry(device_id, row, outcome,
                            f"{row['operation']} {row['job_id']} {outcome}: {value['message'][:200]}{late}",
                            state=state, result=encoded, failure_value=value, supersedes=supersedes)
        self._end_job_lease(row, 'job finished')

    @staticmethod
    def _public(row):
        return {'job_id': row['job_id'], 'state': row['state'], 'operation': row['operation'],
                'scene_id': row['scene_id'], 'created_at': row['created'],
                'participant_id': row['participant_id'] or LEGACY, 'lease_id': row['lease_id'],
                'result': json.loads(row['result']) if row['result'] else None}

    def result(self, device_id, job_id):
        with self.lock, self.db:
            self._expire(device_id)
            row = self.db.execute('SELECT * FROM jobs WHERE device_id=? AND job_id=?', (device_id, job_id)).fetchone()
            if not row:
                raise ValueError('job_not_found')
            return self._public(row)

    def cancel(self, device_id, job_id, participant_id=LEGACY):
        with self.lock, self.db:
            row = self.db.execute('SELECT * FROM jobs WHERE device_id=? AND job_id=?', (device_id, job_id)).fetchone()
            if row is not None and (row['participant_id'] or LEGACY) != participant_id:
                raise ValueError('job_owned_by_another_participant')
            changed = self.db.execute(
                "UPDATE jobs SET state='cancelled' WHERE device_id=? AND job_id=? AND state='queued'", (device_id, job_id))
            if changed.rowcount == 1:
                self._job_entry(device_id, row, 'cancelled',
                                f"{row['operation']} {job_id} cancelled before delivery; it never ran",
                                state='cancelled')
                self._end_job_lease(row, 'job cancelled')
            return self.result(device_id, job_id)

    def chat_exchange(self, device_id, chat):
        if not isinstance(chat, dict):
            raise ValueError('invalid_chat_payload')
        cursor = chat.get('cursor', 0)
        messages = chat.get('messages', [])
        if type(cursor) is not int or cursor < 0 or not isinstance(messages, list) or len(messages) > 4:
            raise ValueError('invalid_chat_payload')

        ack_ids = []
        with self.lock, self.db:
            max_seq = self.db.execute(
                'SELECT COALESCE(MAX(seq),0) FROM chat_events WHERE device_id=?',
                (device_id,),
            ).fetchone()[0]
            cursor = min(cursor, int(max_seq))
            for item in messages:
                if not isinstance(item, dict) or set(item) != {'id', 'text'}:
                    raise ValueError('invalid_chat_message')
                message_id, text = item['id'], item['text']
                if not isinstance(message_id, str) or not CHAT_KEY.fullmatch(message_id):
                    raise ValueError('invalid_chat_message_id')
                if not isinstance(text, str):
                    raise ValueError('invalid_chat_message_text')
                text = text.strip()
                if not text or len(text) > 2000:
                    raise ValueError('invalid_chat_message_text')
                old = self.db.execute(
                    'SELECT text FROM chat_messages WHERE device_id=? AND message_id=?',
                    (device_id, message_id),
                ).fetchone()
                if old:
                    if old['text'] != text:
                        raise ValueError('chat_message_id_reused_with_different_text')
                else:
                    self.db.execute(
                        'INSERT INTO chat_messages(device_id,message_id,text,state,created) '
                        "VALUES (?,?,?,'queued',?)",
                        (device_id, message_id, text, time.time()),
                    )
                ack_ids.append(message_id)

            rows = self.db.execute(
                'SELECT seq,message_id,type,text FROM chat_events '
                'WHERE device_id=? AND seq>? ORDER BY seq LIMIT 50',
                (device_id, cursor),
            ).fetchall()
            events = [
                {
                    'seq': row['seq'],
                    'message_id': row['message_id'],
                    'type': row['type'],
                    'text': row['text'],
                }
                for row in rows
            ]
            next_cursor = rows[-1]['seq'] if rows else cursor
            return {'cursor': next_cursor, 'ack_ids': ack_ids, 'events': events}

    def chat_claim(self, device_id):
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT message_id,text FROM chat_messages "
                "WHERE device_id=? AND state='queued' ORDER BY created LIMIT 1",
                (device_id,),
            ).fetchone()
            if not row:
                return None
            changed = self.db.execute(
                "UPDATE chat_messages SET state='running', started=? "
                "WHERE device_id=? AND message_id=? AND state='queued'",
                (time.time(), device_id, row['message_id']),
            )
            if changed.rowcount != 1:
                return None
            return {'id': row['message_id'], 'text': row['text']}

    def chat_event(self, device_id, message_id, event_type, text):
        with self.lock, self.db:
            self._chat_event_locked(device_id, message_id, event_type, text)

    def chat_complete(self, device_id, message_id, text):
        if not isinstance(text, str):
            raise ValueError('invalid_chat_final')
        text = text.strip()
        if not text or len(text) > 2000:
            raise ValueError('invalid_chat_final')
        with self.lock, self.db:
            changed = self.db.execute(
                "UPDATE chat_messages SET state='completed', finished=? "
                "WHERE device_id=? AND message_id=? AND state='running'",
                (time.time(), device_id, message_id),
            )
            if changed.rowcount != 1:
                raise ValueError('chat_message_not_running')
            self._chat_event_locked(device_id, message_id, 'final', text)

    def chat_fail(self, device_id, message_id, text):
        if not isinstance(text, str):
            raise ValueError('invalid_chat_error')
        text = text.strip()
        if not text or len(text) > 2000:
            raise ValueError('invalid_chat_error')
        with self.lock, self.db:
            changed = self.db.execute(
                "UPDATE chat_messages SET state='failed', finished=? "
                "WHERE device_id=? AND message_id=? AND state IN ('queued','running')",
                (time.time(), device_id, message_id),
            )
            if changed.rowcount == 1:
                self._chat_event_locked(device_id, message_id, 'error', text)

    def chat_thread_id(self, device_id):
        with self.lock:
            row = self.db.execute(
                'SELECT thread_id FROM chat_state WHERE device_id=?',
                (device_id,),
            ).fetchone()
            return row['thread_id'] if row else None

    def chat_set_thread_id(self, device_id, thread_id):
        if thread_id is not None and (not isinstance(thread_id, str) or len(thread_id) > 256):
            raise ValueError('invalid_chat_thread_id')
        with self.lock, self.db:
            if thread_id is None:
                self.db.execute('DELETE FROM chat_state WHERE device_id=?', (device_id,))
            else:
                self.db.execute(
                    'INSERT INTO chat_state(device_id,thread_id,updated) VALUES (?,?,?) '
                    'ON CONFLICT(device_id) DO UPDATE SET thread_id=excluded.thread_id, updated=excluded.updated',
                    (device_id, thread_id, time.time()),
                )

    def put_oauth(self, key, kind, value, expires):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO oauth VALUES (?,?,?,?)', (key, kind, json.dumps(value), expires))

    def get_oauth(self, key, kind, consume=False):
        with self.lock, self.db:
            row = self.db.execute('SELECT * FROM oauth WHERE key=? AND kind=? AND expires>?', (key, kind, time.time())).fetchone()
            if row and consume:
                self.db.execute('DELETE FROM oauth WHERE key=?', (key,))
            return json.loads(row['value']) if row else None
