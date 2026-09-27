# SPDX-License-Identifier: GPL-2.0-or-later
"""Agent Router: GHOSTroom tasks to whichever configured agent the user chose.

No provider is structurally essential. Every agent is an AgentAdapter; the
router owns queueing, the agent-neutral prompt, steering, stopping and the
availability/auth/quota/task state GHOSTroom shows. Each adapter reaches
Blender only through the GhostBlender MCP tool layer, under its own
participant identity, so leases, the ledger and stop/read-only enforcement in
the relay apply to every agent the same way.
"""
import json
import threading
import time

from store import failure, rfc3339
import room as room_service

FAILURE_HINTS = (
    (('usage limit', 'rate limit', 'quota', '429', 'insufficient_quota', 'credit'), 'quota_exhausted',
     'agent_provider', 'switch_agent'),
    (('not_signed_in', 'sign in', 'unauthor', '401', '403', 'expired token', 'invalid api key',
      'authentication'), 'auth_expired', 'agent_provider', 'after_user_action'),
)


class AgentError(Exception):
    """An agent turn failed with a typed failure (failure.schema.json)."""

    def __init__(self, value):
        self.failure = value
        super().__init__(value['message'])


def classify(exc, agent_id):
    """A specific failure for an exception from a provider. Never just 'AI error'."""
    if isinstance(exc, AgentError):
        return exc.failure
    text = f'{type(exc).__name__}: {exc}'.replace('\n', ' ')[:600]
    lowered = text.lower()
    for needles, code, layer, retry in FAILURE_HINTS:
        if any(needle in lowered for needle in needles):
            return failure(code, layer, text, True, retry, agent_id=agent_id)
    return failure('provider_unavailable', 'agent_provider', text, True, 'switch_agent', agent_id=agent_id)


class Turn:
    """One agent turn for one task. Adapters call narrate() for visible progress."""

    def __init__(self, router, adapter, task, prompt, images):
        self.router, self.adapter, self.task = router, adapter, task
        self.prompt, self.images = prompt, images
        self._last_narration = 0.0

    @property
    def stop_requested(self):
        current = self.router.store.task(self.router.device_id, self.task['task_id'])
        return bool(current and current['stop_requested'])

    def narrate(self, text):
        text = ' '.join(str(text).split())
        if not text or time.monotonic() - self._last_narration < 1.0:
            return
        self._last_narration = time.monotonic()
        self.router.store.chat_event(self.router.device_id, self.task['task_id'], 'status', text[:600])


class AgentAdapter:
    """Common interface for every agent GHOSTroom can talk to.

    Subclasses set the identity attributes and implement probe() and run_turn().
    """

    agent_id = 'agent'
    provider = 'none'
    display_name = 'Agent'
    participant_id = 'agent-embedded'
    auth_method = 'other'
    capabilities = ('discuss', 'inspect', 'plan', 'execute', 'capture', 'review', 'long_running')
    roles = ('primary', 'reviewer', 'specialist', 'critic', 'verifier')
    can_steer = False
    # Setup operations GHOSTroom offers for this agent: 'sign_in' (interactive
    # provider sign-in), 'api_key' (paste a key), 'sign_out'.
    setup_methods = ()
    secrets = None

    def setup(self, op, value=None):
        """Run a setup operation requested from GHOSTroom. Returns a short status text."""
        raise ValueError('setup_not_supported')

    def setup_state(self):
        """{'state': idle|pending|done|error, 'message', optional 'url', 'code'} for GHOSTroom."""
        return {'state': 'idle'}

    def configure(self, store, device_id, mcp_url):
        self.store, self.device_id, self.mcp_url = store, device_id, mcp_url

    def probe(self):
        """Return {'availability','auth','quota','model','reason'} without starting a turn."""
        return {'availability': 'unknown', 'auth': 'unknown', 'quota': 'unknown'}

    def run_turn(self, turn):
        """Run one turn to completion and return the final reply text."""
        raise NotImplementedError

    def steer(self, text):
        """Deliver extra user input into the running turn. Only if can_steer."""
        raise NotImplementedError

    def interrupt(self):
        """Best-effort request to end the running turn early."""

    def close(self):
        pass


class AgentRouter:
    def __init__(self, store, device_id, mcp_base, default_agent=None, poll_interval=0.25, secrets=None):
        self.secrets = secrets
        self.store, self.device_id, self.mcp_base = store, device_id, mcp_base.rstrip('/')
        self.poll_interval = poll_interval
        self.adapters, self._threads, self._running = {}, {}, {}
        self._probes, self._stop = {}, threading.Event()
        self._refreshing = set()
        self._lock = threading.RLock()
        self._default = default_agent

    # ------------------------------------------------------------------ registry

    def register(self, adapter, mcp_url=None):
        capability = self.store.ensure_participant_capability(
            adapter.participant_id, 'agent', adapter.display_name, provider=adapter.provider)
        adapter.configure(self.store, self.device_id, mcp_url or f'{self.mcp_base}/mcp/p/{capability}')
        if adapter.secrets is None:
            adapter.secrets = self.secrets
        self.adapters[adapter.agent_id] = adapter
        self.store.embedded = frozenset(self.store.embedded | {adapter.participant_id})
        if self._default is None:
            self._default = adapter.agent_id
        self.store.default_agent = self.default_agent
        return adapter

    @property
    def default_agent(self):
        return self._default

    def has(self, agent_id):
        return agent_id in self.adapters

    # ------------------------------------------------------------------ setup from GHOSTroom

    def setup(self, agent_id, op, secret=None):
        adapter = self.adapters.get(agent_id)
        if adapter is None:
            raise ValueError('unknown_agent')
        if op not in adapter.setup_methods:
            raise ValueError('setup_not_supported')
        message = adapter.setup(op, secret)
        self._probes.pop(agent_id, None)  # show the new state at the next exchange
        with self.store.lock, self.store.db:
            self.store._ledger(self.device_id, 'owner', 'summary',
                               f'{adapter.display_name}: {op.replace("_", " ")} requested from GHOSTroom')
        return message

    def setup_states(self):
        states = {}
        for agent_id, adapter in self.adapters.items():
            try:
                state = dict(adapter.setup_state() or {})
            except Exception as exc:
                state = {'state': 'error', 'message': type(exc).__name__}
            state['methods'] = list(adapter.setup_methods)
            states[agent_id] = state
        return states

    def can_steer(self, agent_id):
        adapter = self.adapters.get(agent_id)
        return bool(adapter and adapter.can_steer)

    # ------------------------------------------------------------------ state

    def _probe(self, adapter, max_age=60.0):
        """Cached provider state. Stale entries refresh in the background, never inline.

        describe() runs inside the device exchange, and a probe can start a provider
        runtime; the device must never wait for that.
        """
        cached = self._probes.get(adapter.agent_id)
        if cached and time.monotonic() - cached[0] < max_age:
            return cached[1]
        with self._lock:
            refreshing = adapter.agent_id in self._refreshing
            if not refreshing:
                self._refreshing.add(adapter.agent_id)
        if not refreshing:
            threading.Thread(target=self._refresh, args=(adapter,), daemon=True,
                             name=f'ghostroom-probe-{adapter.agent_id}').start()
        return cached[1] if cached else {'availability': 'unknown', 'auth': 'unknown', 'quota': 'unknown'}

    def _refresh(self, adapter):
        try:
            value = adapter.probe() or {}
        except Exception as exc:  # A probe failure is a state to show, never a crash.
            value = {'availability': 'unavailable', 'auth': 'unknown', 'quota': 'unknown',
                     'reason': classify(exc, adapter.agent_id)}
        finally:
            with self._lock:
                self._refreshing.discard(adapter.agent_id)
        self._probes[adapter.agent_id] = (time.monotonic(), value)

    def probe_now(self):
        """Refresh every adapter synchronously (startup and tests)."""
        for adapter in self.adapters.values():
            self._refresh(adapter)

    def note_failure(self, agent_id, value):
        """Remember a provider failure so availability reflects it before the next turn."""
        adapter = self.adapters.get(agent_id)
        if not adapter:
            return
        state = {'availability': 'unavailable', 'auth': 'unknown', 'quota': 'unknown', 'reason': value}
        if value['code'] == 'quota_exhausted':
            state['quota'] = 'exhausted'
        elif value['code'] == 'auth_expired':
            state['auth'] = 'expired'
        else:
            state['availability'] = 'unknown'
        # Provider trouble is re-probed after a short while rather than cached for long.
        self._probes[agent_id] = (time.monotonic() - 30.0, state)

    def describe(self):
        """Agent descriptors (agent.schema.json) for GHOSTroom and tools, embedded then external."""
        return self._describe_embedded() + room_service.external_descriptors(self.store)

    def _describe_embedded(self):
        now = rfc3339(time.time())
        result = []
        for adapter in self.adapters.values():
            probe = self._probe(adapter)
            availability = {'state': probe.get('availability', 'unknown'), 'checked_at': now}
            running = self._running.get(adapter.agent_id)
            if availability['state'] == 'available' and running:
                availability['state'] = 'busy'
            if probe.get('reason') and availability['state'] in ('unavailable', 'unknown'):
                availability['reason'] = probe['reason']
            if availability['state'] == 'unavailable' and 'reason' not in availability:
                availability['reason'] = failure('provider_unavailable', 'router', 'Agent unavailable', False,
                                                 'switch_agent', agent_id=adapter.agent_id)
            descriptor = {
                'agent_id': adapter.agent_id, 'display_name': adapter.display_name,
                'provider': adapter.provider, 'lane': adapter.participant_id,
                'availability': availability,
                'auth': {'state': probe.get('auth', 'unknown'), 'method': adapter.auth_method, 'checked_at': now},
                'capabilities': list(adapter.capabilities), 'roles': list(adapter.roles),
                'quota': {'state': probe.get('quota', 'unknown')}, 'updated_at': now,
            }
            if probe.get('model'):
                descriptor['model'] = str(probe['model'])[:128]
            if probe.get('quota_detail'):
                descriptor['quota']['detail'] = str(probe['quota_detail'])[:200]
            if running:
                descriptor['active_task'] = running['task_id']
                activity = self._activity(adapter, running)
                if activity:
                    descriptor['activity'] = activity
            result.append(descriptor)
        return result

    def _activity(self, adapter, task):
        with self.store.lock:
            row = self.store.db.execute(
                "SELECT operation, arguments, state FROM jobs WHERE device_id=? AND task_id=? "
                'ORDER BY created DESC LIMIT 1', (self.device_id, task['task_id'])).fetchone()
        if row is None:
            return 'Thinking'
        label = room_service.activity_label(row['operation'], json.loads(row['arguments']))
        if row['state'] in ('queued', 'issued'):
            return label[:200]
        return ('Thinking after: ' + label)[:200]

    # ------------------------------------------------------------------ control

    def interrupt(self, task_id):
        for agent_id, task in list(self._running.items()):
            if task and task['task_id'] == task_id:
                try:
                    self.adapters[agent_id].interrupt()
                except Exception as exc:
                    print('ghostroom_interrupt_error=' + type(exc).__name__, flush=True)

    def start(self):
        self._stop.clear()
        for adapter in self.adapters.values():
            self._probe(adapter)
        for agent_id in self.adapters:
            thread = self._threads.get(agent_id)
            if thread and thread.is_alive():
                continue
            thread = threading.Thread(target=self._worker, args=(agent_id,), name=f'ghostroom-{agent_id}',
                                      daemon=True)
            self._threads[agent_id] = thread
            thread.start()

    def close(self):
        self._stop.set()
        for adapter in self.adapters.values():
            try:
                adapter.interrupt()
            except Exception:
                pass
        for thread in self._threads.values():
            thread.join(timeout=2.0)
        for adapter in self.adapters.values():
            try:
                adapter.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ worker

    def _lane_key(self, agent_id):
        return f'lane_seen:{self.device_id}:{agent_id}'

    def _lane_seen(self, agent_id):
        with self.store.lock:
            row = self.store.db.execute('SELECT value FROM relay_meta WHERE key=?',
                                        (self._lane_key(agent_id),)).fetchone()
        if not row:
            return 0
        stream, _, seq = row['value'].partition(':')
        return int(seq) if stream == self.store.ledger_stream_id and seq.isdigit() else 0

    def _mark_lane(self, agent_id):
        with self.store.lock, self.store.db:
            seq = self.store.ledger_issued()
            self.store.db.execute('INSERT OR REPLACE INTO relay_meta VALUES (?,?)',
                                  (self._lane_key(agent_id), f'{self.store.ledger_stream_id}:{seq}'))

    def _images(self, task):
        images = []
        for item in task.get('context', []):
            artifact_id = item.get('artifact_id')
            if not artifact_id:
                continue
            try:
                value = self.store.artifact(self.device_id, artifact_id, with_data=True)
            except ValueError:
                continue
            if value.get('data'):
                images.append({'artifact_id': artifact_id, 'media_type': value['media']['media_type'],
                               'data': value['data']})
        return images[:4]

    def _worker(self, agent_id):
        adapter = self.adapters[agent_id]
        while not self._stop.is_set():
            task = self.store.chat_claim(self.device_id, agent_id, adapter.participant_id)
            if task is None:
                self._stop.wait(self.poll_interval)
                continue
            self._running[agent_id] = task
            try:
                self._run_task(adapter, task)
            finally:
                self._running.pop(agent_id, None)
                self._mark_lane(agent_id)
                self.store.requeue_steers(self.device_id, task['task_id'])

    def _run_task(self, adapter, task):
        seen = self._lane_seen(adapter.agent_id)
        brief = room_service.workspace_brief(self.store, self.device_id, seen,
                                             exclude_participant=adapter.participant_id,
                                             before_seq=task.get('instruction_seq'))
        prompt = room_service.compose_prompt(task, brief)
        turn = Turn(self, adapter, task, prompt, self._images(task))
        done = threading.Event()
        watcher = threading.Thread(target=self._watch, args=(adapter, turn, done), daemon=True,
                                   name=f'ghostroom-watch-{adapter.agent_id}')
        watcher.start()
        try:
            final = adapter.run_turn(turn)
        except Exception as exc:
            value = classify(exc, adapter.agent_id)
            print(f'ghostroom_agent_error agent={adapter.agent_id} code={value["code"]}', flush=True)
            if value['code'] in ('quota_exhausted', 'auth_expired', 'provider_unavailable'):
                self.note_failure(adapter.agent_id, value)
            stopped = turn.stop_requested
            self.store.chat_fail(self.device_id, task['task_id'], _failure_text(adapter, value, stopped), value)
        else:
            if not isinstance(final, str) or not final.strip():
                final = 'Stopped.' if turn.stop_requested else 'The agent finished without a reply.'
            if self._probes.get(adapter.agent_id, (0, {}))[1].get('availability') != 'available':
                self._probes.pop(adapter.agent_id, None)  # it just worked; re-probe instead of showing stale trouble
            self.store.chat_complete(self.device_id, task['task_id'], final.strip()[:16000])
        finally:
            done.set()
            watcher.join(timeout=1.0)

    def _watch(self, adapter, turn, done):
        """Deliver redirects into the running turn and enforce stop, while it runs."""
        interrupted = False
        while not done.wait(0.4):
            if not interrupted and turn.stop_requested:
                interrupted = True
                try:
                    adapter.interrupt()
                except Exception as exc:
                    print('ghostroom_interrupt_error=' + type(exc).__name__, flush=True)
            if interrupted or not adapter.can_steer:
                continue
            for steer in self.store.take_steers(self.device_id, turn.task['task_id']):
                try:
                    adapter.steer(room_service.compose_prompt(steer, steer=True))
                    self.store.chat_event(self.device_id, turn.task['task_id'], 'status',
                                          'Redirect delivered to the running task')
                except Exception as exc:
                    # The redirect could not reach the turn: run it as the next task instead.
                    print('ghostroom_steer_error=' + type(exc).__name__, flush=True)
                    self.store.unsteer(self.device_id, steer['task_id'])


def _failure_text(adapter, value, stopped):
    if stopped:
        return f'{adapter.display_name} stopped.' + (
            ' Blender may have been changed before the stop; inspect before continuing.'
            if value.get('mutation_possible') else '')
    code = value['code']
    if code == 'quota_exhausted':
        return f'{adapter.display_name} has reached its usage limit. Another agent can continue from the ledger.'
    if code == 'auth_expired':
        return f'{adapter.display_name} needs to be signed in again on the relay.'
    if code == 'provider_unavailable':
        return (f'{adapter.display_name} is unavailable ({value["message"][:160]}). '
                'The bridge is fine; inspect the scene before retrying or switch agent.')
    return f'{adapter.display_name} failed: {value["message"][:300]}'
