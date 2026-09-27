# SPDX-License-Identifier: GPL-2.0-or-later
"""Codex agent adapter for the Agent Router, and the legacy ChatWorker entry point.

Codex is one agent implementation, not the relay's brain: CodexAdapter plugs
into router.AgentRouter like any other provider.
"""

import base64
import os
from pathlib import Path
import threading

from router import AgentAdapter, AgentError, AgentRouter
from store import failure


DEVELOPER_INSTRUCTIONS = """You are the embedded GhostBlender assistant running for one trusted owner.
For Blender work, use the ghostblender MCP server and its live scene evidence rather than guessing.
Start with status and inspect_scene when the task depends on scene state. Keep edits in small steps,
poll job_result before dependent work, capture actual images when visual verification matters, and
never claim an edit succeeded without checking the result. Do not use shell commands or inspect the
relay filesystem.

You work inside GHOSTroom, the native AI workspace of GHOSTpad. The user watches your real tool calls
as live activity, so you do not need to narrate each operation. Start every execute_python code with
one short comment line naming the step (for example "# Shape the wheel arches"); GHOSTroom shows it
as the current phase. Use capture for visual checks: captures appear to the user as evidence.
Other agents and the user share this workspace: read_ledger and workspace_brief show what everyone
did; post_note records decisions (with rationale), reviews, handoffs and open questions for them.
If a tool call is rejected with stopped_by_user, stop working and reply briefly. If it is rejected
with read_only_role, do not try to change Blender. Final replies may use short paragraphs and lists;
say what changed, what you verified and what the user may want to do next.
"""


def prepare_codex_environment(mcp_url):
    """Create the dedicated Codex home without placing relay credentials in it."""
    codex_home = Path(os.environ.get("CODEX_HOME", "/data/codex"))
    os.environ["CODEX_HOME"] = str(codex_home)
    os.environ.setdefault("HOME", "/tmp/ghostbridge-home")
    Path(os.environ["HOME"]).mkdir(parents=True, exist_ok=True)
    codex_home.mkdir(parents=True, exist_ok=True)
    workdir = Path("/tmp/ghostblender-chat")
    workdir.mkdir(parents=True, exist_ok=True)

    config = f'''approval_policy = "never"
sandbox_mode = "read-only"
web_search = "disabled"

[mcp_servers.ghostblender]
url = "{mcp_url}"
required = true
default_tools_approval_mode = "approve"

[projects."/tmp/ghostblender-chat"]
trust_level = "trusted"
'''
    config_path = codex_home / "config.toml"
    if not config_path.exists() or config_path.read_text() != config:
        config_path.write_text(config)
        config_path.chmod(0o600)
    return codex_home, workdir


class CodexAdapter(AgentAdapter):
    """OpenAI Codex through the openai-codex SDK, with the GhostBlender MCP server."""

    agent_id = 'codex'
    provider = 'codex'
    display_name = 'Codex'
    participant_id = 'codex-embedded'
    auth_method = 'device_code'
    capabilities = ('discuss', 'inspect', 'plan', 'execute', 'capture', 'vision', 'review', 'teach',
                    'long_running')
    can_steer = True
    setup_methods = ('sign_in', 'sign_out')

    def __init__(self):
        self._login = {'state': 'idle'}
        self._codex = None
        self._thread = None
        self._sdk = None
        self._workdir = None
        self._handle = None
        self._lock = threading.RLock()

    def configure(self, store, device_id, mcp_url):
        super().configure(store, device_id, mcp_url)
        _, self._workdir = prepare_codex_environment(mcp_url)

    # -- SDK lifecycle, unchanged from the proven embedded worker ------------

    def close(self):
        with self._lock:
            if self._codex is not None:
                try:
                    self._codex.close()
                except Exception:
                    pass
            self._codex = None
            self._thread = None
            self._sdk = None
            self._handle = None

    def _open_codex(self):
        if self._codex is not None:
            return self._codex
        from openai_codex import ApprovalMode, Codex, Sandbox

        codex = Codex()
        account = codex.account()
        if getattr(account, "account", None) is None:
            codex.close()
            raise AgentError(failure('auth_expired', 'agent_provider',
                                     'Codex is not signed in on the relay (codex_not_signed_in).',
                                     False, 'after_user_action', agent_id=self.agent_id))
        self._codex = codex
        self._sdk = (ApprovalMode, Sandbox)
        return codex

    def _thread_options(self):
        approval_mode, sandbox = self._sdk
        options = {
            "approval_mode": approval_mode.deny_all,
            "sandbox": sandbox.read_only,
            "cwd": str(self._workdir),
            "developer_instructions": DEVELOPER_INSTRUCTIONS,
        }
        effort = os.environ.get("CODEX_REASONING_EFFORT", "medium").strip()
        if effort:
            options["config"] = {"model_reasoning_effort": effort}
        model = self.model()
        if model:
            options["model"] = model
        return options

    @staticmethod
    def model():
        return os.environ.get("CODEX_MODEL", "").strip()

    def _ensure_thread(self):
        if self._thread is not None:
            return self._thread
        codex = self._open_codex()
        options = self._thread_options()
        thread_id = self.store.chat_thread_id(self.device_id)
        if thread_id:
            try:
                self._thread = codex.thread_resume(thread_id, **options)
                return self._thread
            except Exception:
                # Resuming has not started the user's new turn, so falling back to
                # a new conversation cannot duplicate Blender work. The shared
                # ledger brief carries the project context into the new thread.
                self.store.chat_set_thread_id(self.device_id, None)
                self.close()
                codex = self._open_codex()
                options = self._thread_options()
        self._thread = codex.thread_start(**options)
        self.store.chat_set_thread_id(self.device_id, self._thread.id)
        return self._thread

    # -- setup from GHOSTroom ------------------------------------------------

    def setup(self, op, value=None):
        from openai_codex import Codex

        if op == 'sign_out':
            with self._lock:
                codex = Codex()
                try:
                    codex.logout()
                finally:
                    codex.close()
                self.close()
            self._login = {'state': 'idle', 'message': 'Signed out of Codex'}
            return 'Signed out of Codex'
        if op != 'sign_in':
            raise ValueError('setup_not_supported')
        if self._login.get('state') == 'pending':
            return 'Sign-in already in progress'
        codex = Codex()
        try:
            account = codex.account()
            if getattr(account, 'account', None) is not None:
                codex.close()
                self._login = {'state': 'done', 'message': 'Codex is already signed in'}
                return self._login['message']
            login = codex.login_chatgpt_device_code()
        except Exception:
            codex.close()
            raise
        self._login = {'state': 'pending', 'url': str(login.verification_url), 'code': str(login.user_code),
                       'message': 'Open the link, sign in to ChatGPT and enter the code'}

        def wait():
            try:
                completed = login.wait()
                ok = bool(getattr(completed, 'success', False))
                self._login = {'state': 'done' if ok else 'error',
                               'message': 'Codex signed in' if ok else 'Codex sign-in did not complete'}
            except Exception as exc:
                self._login = {'state': 'error', 'message': f'Codex sign-in failed: {type(exc).__name__}'}
            finally:
                codex.close()
                with self._lock:
                    if self._handle is None:
                        self.close()  # the next turn re-opens Codex with the new session

        threading.Thread(target=wait, daemon=True, name='codex-sign-in').start()
        return self._login['message']

    def setup_state(self):
        return dict(self._login)

    # -- AgentAdapter ---------------------------------------------------------

    def probe(self):
        try:
            import openai_codex  # noqa: F401
        except ImportError:
            return {'availability': 'unavailable', 'auth': 'unknown', 'quota': 'unknown',
                    'reason': failure('provider_unavailable', 'agent_provider',
                                      'The openai-codex SDK is not installed on the relay.', False,
                                      'switch_agent', agent_id=self.agent_id)}
        with self._lock:
            if self._handle is not None:
                return {'availability': 'available', 'auth': 'signed_in', 'quota': 'unknown',
                        'model': self.model() or None}
            try:
                self._open_codex()
            except AgentError as exc:
                return {'availability': 'unavailable', 'auth': 'signed_out', 'quota': 'unknown',
                        'reason': exc.failure}
        return {'availability': 'available', 'auth': 'signed_in', 'quota': 'unknown',
                'model': self.model() or None}

    def _input(self, turn):
        if not turn.images:
            return turn.prompt
        try:
            from openai_codex import ImageInput, TextInput
        except ImportError:
            return turn.prompt
        items = [TextInput(turn.prompt)]
        for image in turn.images:
            items.append(ImageInput(f"data:{image['media_type']};base64,{image['data']}"))
        return items

    def run_turn(self, turn):
        with self._lock:
            thread = self._ensure_thread()
            approval_mode, sandbox = self._sdk
            options = dict(approval_mode=approval_mode.deny_all, sandbox=sandbox.read_only,
                           cwd=str(self._workdir))
            starter = getattr(thread, 'turn', None)
            if starter is None:
                handle = None
            else:
                handle = starter(self._input(turn), **options)
                self._handle = handle
        try:
            if handle is None:
                result = thread.run(turn.prompt, **options)
                final = getattr(result, "final_response", None)
            else:
                final = self._consume(handle, turn)
        except AgentError:
            self.close()
            raise
        except Exception:
            self.close()
            raise
        finally:
            with self._lock:
                self._handle = None
        if turn.stop_requested:
            return final.strip()[:16000] if isinstance(final, str) and final.strip() else 'Stopped.'
        if not isinstance(final, str) or not final.strip():
            raise AgentError(failure('provider_unavailable', 'agent_provider',
                                     'Codex returned no final response (codex_returned_no_final_response).',
                                     True, 'after_inspect', agent_id=self.agent_id))
        return final.strip()[:16000]

    def _consume(self, handle, turn):
        """Stream the turn: commentary becomes visible narration, the final answer is returned."""
        final, fallback, status, error = None, None, None, None
        for event in handle.stream():
            payload = getattr(event, 'payload', None)
            item = getattr(payload, 'item', None)
            item = getattr(item, 'root', item)
            if getattr(event, 'method', '') == 'item/completed' and getattr(item, 'type', None) == 'agentMessage':
                phase = getattr(getattr(item, 'phase', None), 'value', getattr(item, 'phase', None))
                text = getattr(item, 'text', '') or ''
                if phase == 'final_answer':
                    final = text
                elif phase == 'commentary':
                    turn.narrate(text)
                    fallback = text
                else:
                    fallback = text
            completed = getattr(payload, 'turn', None)
            if getattr(event, 'method', '') == 'turn/completed' and completed is not None:
                status = getattr(getattr(completed, 'status', None), 'value', getattr(completed, 'status', None))
                error = getattr(getattr(completed, 'error', None), 'message', None)
        if status == 'failed':
            raise RuntimeError(error or 'turn failed')
        return final or fallback

    def steer(self, text):
        with self._lock:
            handle = self._handle
        if handle is None:
            raise RuntimeError('no_running_turn')
        handle.steer(text)

    def interrupt(self):
        with self._lock:
            handle = self._handle
        if handle is not None:
            handle.interrupt()


class ChatWorker:
    """Legacy entry point: an Agent Router with only the Codex adapter.

    Kept so existing deployments and tests start the embedded agent the same
    way. New deployments build an AgentRouter with every configured adapter.
    """

    def __init__(self, store, device_id, mcp_url, poll_interval=0.25):
        self.router = AgentRouter(store, device_id, 'http://127.0.0.1', poll_interval=poll_interval)
        self.adapter = CodexAdapter()
        self.mcp_url = mcp_url
        self._registered = False

    def start(self):
        if not self._registered:
            self.router.register(self.adapter, mcp_url=self.mcp_url)
            self._registered = True
        self.router.start()

    def close(self):
        self.router.close()
