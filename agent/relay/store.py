# SPDX-License-Identifier: GPL-2.0-or-later
"""Single-device durable relay. All state transitions serialize through SQLite."""
import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid

OPERATIONS = {'inspect_scene', 'execute_python', 'capture', 'diagnostics',
              'list_scripts', 'read_script', 'write_script'}
KEY = re.compile(r'^[a-zA-Z0-9_-]{8,80}$')
CHAT_KEY = re.compile(r'^[a-f0-9]{32}$')


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
        ''')
        self.db.commit()
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

    def _expire(self, device_id):
        now = time.time()
        self.db.execute("UPDATE jobs SET state='expired' WHERE device_id=? AND state='queued' AND expires<?", (device_id, now))
        # Delivered commands may already have changed Blender. Never requeue them.
        self.db.execute("UPDATE jobs SET state='uncertain' WHERE device_id=? AND state='issued' AND expires<?", (device_id, now))
        self.db.execute('DELETE FROM oauth WHERE expires<?', (now,))
        # Keep metadata/idempotency for seven days, images/results for one day.
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
            row = self.db.execute('SELECT * FROM device WHERE id=?', (device_id,)).fetchone()
            if not row:
                return {'online': False, 'reason': 'device_not_paired'}
            heartbeat = json.loads(row['heartbeat'])
            return {**heartbeat, 'online': time.time() - row['seen'] < 12 and heartbeat['native']['foreground'],
                    'last_seen': row['seen']}

    def submit(self, device_id, operation, arguments, request_id, scene_id):
        if operation not in OPERATIONS or not KEY.fullmatch(request_id):
            raise ValueError('invalid_operation_or_request_id')
        payload = json.dumps(arguments, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if len(payload.encode()) > 110_000:
            raise ValueError('arguments_too_large')
        digest = hashlib.sha256((operation + '\n' + scene_id + '\n' + payload).encode()).hexdigest()
        with self.lock, self.db:
            old = self.db.execute('SELECT * FROM jobs WHERE device_id=? AND request_id=?', (device_id, request_id)).fetchone()
            if old:
                if old['digest'] != digest:
                    raise ValueError('idempotency_key_reused_with_different_arguments')
                return self._public(old)
            status = self.status(device_id)
            if not status['online']:
                raise ValueError('device_offline_or_suspended')
            if status['scene_id'] != scene_id:
                raise ValueError('scene_changed; call status and inspect_scene again')
            active = self.db.execute("SELECT count(*) FROM jobs WHERE device_id=? AND state IN ('queued','issued')", (device_id,)).fetchone()[0]
            if active >= 8:
                raise ValueError('device_queue_full')
            now = time.time()
            job_id = uuid.uuid4().hex
            self.db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (job_id, device_id, request_id, digest, operation, payload, status['boot_id'], scene_id,
                 'queued', now, now + 90, None))
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
                        self.db.execute('UPDATE jobs SET state=?, result=? WHERE job_id=?',
                                        ('completed' if result['ok'] else 'failed', encoded, row['job_id']))
                    ack = row['job_id']
                elif row is None:
                    # Old results can outlive the retention period; clear the device outbox.
                    ack = completed['job_id']
            self.db.execute("UPDATE jobs SET state='expired' WHERE device_id=? AND state='queued' AND (boot_id<>? OR scene_id<>?)",
                            (device_id, heartbeat['boot_id'], heartbeat['scene_id']))
            job = None
            if heartbeat['native']['foreground'] and (not completed or ack):
                # One issued command per device; it is never issued a second time.
                active = self.db.execute("SELECT 1 FROM jobs WHERE device_id=? AND state='issued'", (device_id,)).fetchone()
                row = None if active else self.db.execute("SELECT * FROM jobs WHERE device_id=? AND state='queued' ORDER BY created LIMIT 1", (device_id,)).fetchone()
                if row:
                    self.db.execute("UPDATE jobs SET state='issued' WHERE job_id=?", (row['job_id'],))
                    job = {'job_id': row['job_id'], 'operation': row['operation'],
                           'arguments': json.loads(row['arguments']), 'boot_id': row['boot_id'],
                           'scene_id': row['scene_id'], 'expires_at': row['expires']}
            return {'protocol': 1, 'ack': ack, 'job': job}

    @staticmethod
    def _public(row):
        return {'job_id': row['job_id'], 'state': row['state'], 'operation': row['operation'],
                'scene_id': row['scene_id'], 'created_at': row['created'],
                'result': json.loads(row['result']) if row['result'] else None}

    def result(self, device_id, job_id):
        with self.lock, self.db:
            self._expire(device_id)
            row = self.db.execute('SELECT * FROM jobs WHERE device_id=? AND job_id=?', (device_id, job_id)).fetchone()
            if not row:
                raise ValueError('job_not_found')
            return self._public(row)

    def cancel(self, device_id, job_id):
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET state='cancelled' WHERE device_id=? AND job_id=? AND state='queued'", (device_id, job_id))
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
