# SPDX-License-Identifier: GPL-2.0-or-later
"""Claude agent adapter for the Agent Router.

Dependency-free: talks to the Anthropic Messages API and the GhostBlender MCP
tool layer using only the Python standard library. Blender is reached only
through self.mcp_url (a local HTTP MCP Streamable endpoint), exactly like
every other adapter.
"""
import json
import os
import threading
import urllib.error
import urllib.request

from router import AgentAdapter, AgentError
from store import failure
from chat import DEVELOPER_INSTRUCTIONS

ANTHROPIC_URL = 'https://api.anthropic.com/v1/messages'
ANTHROPIC_VERSION = '2023-06-01'
DEFAULT_MODEL = 'claude-opus-5-5'
DEFAULT_MAX_TOKENS = 8000
REQUEST_TIMEOUT = 120
MAX_ITERATIONS = 60
MUTATING_TOOLS = ('execute_python', 'write_script')
HISTORY_TURNS = 12


class ClaudeAdapter(AgentAdapter):
    """Anthropic Claude, reached through the Messages API, with the GhostBlender MCP server."""

    agent_id = 'claude'
    provider = 'claude'
    display_name = 'Claude'
    participant_id = 'claude-embedded'
    auth_method = 'api_key'
    capabilities = ('discuss', 'inspect', 'plan', 'execute', 'capture', 'vision', 'review', 'teach',
                    'long_running')
    can_steer = False

    def __init__(self):
        self._lock = threading.RLock()
        self._interrupt = threading.Event()
        self._history = []  # list of {'role','text'} pairs, text-only
        self._mcp_id = 0
        self._mcp_initialized = False
        self._tools = None
        self._last_auth_error = False
        self._last_quota_error = False

    # ------------------------------------------------------------------ config

    @staticmethod
    def api_key():
        return os.environ.get('ANTHROPIC_API_KEY', '').strip()

    @staticmethod
    def model():
        return os.environ.get('CLAUDE_MODEL', '').strip() or DEFAULT_MODEL

    @staticmethod
    def max_tokens():
        try:
            return int(os.environ.get('CLAUDE_MAX_TOKENS', '').strip() or DEFAULT_MAX_TOKENS)
        except ValueError:
            return DEFAULT_MAX_TOKENS

    # ------------------------------------------------------------------ HTTP seams (tests replace these)

    def _anthropic(self, payload):
        """POST one Messages API request. Returns the parsed JSON body.

        Raises AgentError on any HTTP-level or transport failure, already
        classified into a typed failure.
        """
        api_key = self.api_key()
        body = json.dumps(payload).encode()
        request = urllib.request.Request(
            ANTHROPIC_URL, data=body, method='POST',
            headers={'x-api-key': api_key, 'anthropic-version': ANTHROPIC_VERSION,
                     'content-type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                self._last_auth_error = False
                self._last_quota_error = False
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                detail = json.loads(raw.decode())
                message = detail.get('error', {}).get('message') or str(detail)
            except Exception:
                message = raw.decode(errors='replace') or str(exc)
            raise AgentError(self._classify_http(exc.code, message)) from None
        except urllib.error.URLError as exc:
            raise AgentError(failure(
                'provider_unavailable', 'agent_provider', f'Claude is unreachable: {exc.reason}',
                False, 'switch_agent', agent_id=self.agent_id)) from None

    def _classify_http(self, code, message):
        message = str(message)[:600]
        if code in (401, 403):
            self._last_auth_error = True
            return failure('auth_expired', 'agent_provider',
                           f'Claude rejected the API key ({code}): {message}', False,
                           'after_user_action', agent_id=self.agent_id)
        if code == 429:
            self._last_quota_error = True
            return failure('quota_exhausted', 'agent_provider',
                           f'Claude has reached its usage limit: {message}', False,
                           'switch_agent', agent_id=self.agent_id)
        if code in (500, 502, 503, 529) or 'overloaded' in message.lower():
            return failure('provider_unavailable', 'agent_provider',
                           f'Claude is unavailable ({code}): {message}', False,
                           'switch_agent', agent_id=self.agent_id)
        return failure('provider_unavailable', 'agent_provider',
                       f'Claude returned an unexpected error ({code}): {message}', False,
                       'switch_agent', agent_id=self.agent_id)

    def _mcp(self, method, params=None):
        """One JSON-RPC call to the GhostBlender MCP endpoint. Returns the 'result' field."""
        self._mcp_id += 1
        payload = {'jsonrpc': '2.0', 'id': self._mcp_id, 'method': method, 'params': params or {}}
        request = urllib.request.Request(
            self.mcp_url, data=json.dumps(payload).encode(), method='POST',
            headers={'Content-Type': 'application/json',
                     'Accept': 'application/json, text/event-stream'})
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            body = json.loads(response.read().decode())
        if 'error' in body and body['error']:
            raise AgentError(failure(
                'provider_unavailable', 'agent_provider',
                f'GhostBlender MCP error: {body["error"]}', False, 'safe', agent_id=self.agent_id))
        return body.get('result')

    # ------------------------------------------------------------------ MCP tool setup

    def _ensure_tools(self):
        with self._lock:
            if self._tools is not None:
                return self._tools
            self._mcp('initialize', {
                'protocolVersion': '2025-06-18', 'capabilities': {},
                'clientInfo': {'name': 'ghostroom-claude', 'version': '1'}})
            listed = self._mcp('tools/list') or {}
            tools = []
            for tool in listed.get('tools', []):
                tools.append({'name': tool['name'], 'description': tool.get('description', ''),
                              'input_schema': tool.get('inputSchema', {'type': 'object'})})
            self._tools = tools
            self._mcp_initialized = True
            return tools

    def _call_tool(self, name, arguments):
        result = self._mcp('tools/call', {'name': name, 'arguments': arguments}) or {}
        content_out = []
        for item in result.get('content', []):
            item_type = item.get('type')
            if item_type == 'text':
                content_out.append({'type': 'text', 'text': item.get('text', '')})
            elif item_type == 'image':
                content_out.append({'type': 'image', 'source': {
                    'type': 'base64', 'media_type': item.get('mimeType', 'image/png'),
                    'data': item.get('data', '')}})
        if not content_out:
            content_out.append({'type': 'text', 'text': ''})
        return content_out, bool(result.get('isError'))

    # ------------------------------------------------------------------ probe

    def probe(self):
        if not self.api_key():
            return {'availability': 'unavailable', 'auth': 'signed_out', 'quota': 'unknown',
                    'reason': failure('auth_expired', 'agent_provider',
                                      'Claude is not configured on the relay: set ANTHROPIC_API_KEY.',
                                      False, 'after_user_action', agent_id=self.agent_id)}
        if self._last_auth_error:
            return {'availability': 'unavailable', 'auth': 'expired', 'quota': 'unknown', 'model': self.model(),
                    'reason': failure('auth_expired', 'agent_provider',
                                      'Claude rejected the API key on its last call.', False,
                                      'after_user_action', agent_id=self.agent_id)}
        if self._last_quota_error:
            return {'availability': 'unavailable', 'auth': 'signed_in', 'quota': 'exhausted', 'model': self.model(),
                    'reason': failure('quota_exhausted', 'agent_provider',
                                      'Claude reached its usage limit on its last call.', False,
                                      'switch_agent', agent_id=self.agent_id)}
        return {'availability': 'available', 'auth': 'signed_in', 'quota': 'unknown', 'model': self.model()}

    # ------------------------------------------------------------------ run_turn

    def _content_for(self, turn):
        content = [{'type': 'text', 'text': turn.prompt}]
        for image in turn.images:
            content.append({'type': 'image', 'source': {
                'type': 'base64', 'media_type': image['media_type'], 'data': image['data']}})
        return content

    def _history_messages(self):
        messages = []
        for item in self._history[-2 * HISTORY_TURNS:]:
            messages.append({'role': item['role'], 'content': [{'type': 'text', 'text': item['text']}]})
        return messages

    def _remember(self, prompt_text, final_text):
        self._history.append({'role': 'user', 'text': prompt_text})
        self._history.append({'role': 'assistant', 'text': final_text})
        del self._history[:-2 * HISTORY_TURNS]

    @staticmethod
    def _text_of(content):
        return ' '.join(block.get('text', '') for block in content if block.get('type') == 'text').strip()

    def run_turn(self, turn):
        self._interrupt.clear()
        tools = self._ensure_tools()
        messages = self._history_messages()
        messages.append({'role': 'user', 'content': self._content_for(turn)})
        mutation_possible = False
        last_text = ''

        for _ in range(MAX_ITERATIONS):
            if turn.stop_requested or self._interrupt.is_set():
                self._remember(turn.prompt, last_text or 'Stopped.')
                return ('Stopped.' + (' ' + last_text if last_text else '')).strip()

            _trim_images(messages)
            payload = {'model': self.model(), 'max_tokens': self.max_tokens(),
                       'system': DEVELOPER_INSTRUCTIONS, 'messages': messages}
            if tools:
                payload['tools'] = tools
            try:
                response = self._anthropic(payload)
            except AgentError as exc:
                exc.failure['mutation_possible'] = mutation_possible
                raise

            content = response.get('content', [])
            stop_reason = response.get('stop_reason')
            text_here = self._text_of(content)
            if text_here:
                last_text = text_here

            tool_uses = [block for block in content if block.get('type') == 'tool_use']

            if stop_reason == 'end_turn' or (stop_reason != 'tool_use' and not tool_uses):
                final = last_text or 'Done.'
                self._remember(turn.prompt, final)
                return final

            if stop_reason == 'max_tokens':
                final = last_text or 'Reached the response length limit.'
                self._remember(turn.prompt, final)
                return final

            if text_here and tool_uses:
                turn.narrate(text_here)

            messages.append({'role': 'assistant', 'content': content})

            if turn.stop_requested or self._interrupt.is_set():
                self._remember(turn.prompt, last_text or 'Stopped.')
                return ('Stopped.' + (' ' + last_text if last_text else '')).strip()

            tool_results = []
            for block in tool_uses:
                if block['name'] in MUTATING_TOOLS:
                    mutation_possible = True
                try:
                    result_content, is_error = self._call_tool(block['name'], block.get('input', {}))
                except AgentError:
                    raise
                except Exception as exc:
                    result_content = [{'type': 'text', 'text': f'{type(exc).__name__}: {exc}'}]
                    is_error = True
                tool_results.append({'type': 'tool_result', 'tool_use_id': block['id'],
                                     'content': result_content, 'is_error': is_error})
            messages.append({'role': 'user', 'content': tool_results})
        final = (last_text or 'Stopped after reaching the step limit.') + ' (stopped: step limit reached)'
        self._remember(turn.prompt, final)
        return final

    # ------------------------------------------------------------------ steer/interrupt

    def steer(self, text):
        raise RuntimeError('claude_cannot_steer')

    def interrupt(self):
        self._interrupt.set()


def _trim_images(messages, keep=3):
    """Long autonomous turns capture often. Keep only the most recent images in the request."""
    seen = 0
    for message in reversed(messages):
        content = message.get('content')
        if not isinstance(content, list):
            continue
        for block in content:
            blocks = block.get('content') if block.get('type') == 'tool_result' else [block]
            if not isinstance(blocks, list):
                continue
            for index, inner in enumerate(blocks):
                if isinstance(inner, dict) and inner.get('type') == 'image':
                    seen += 1
                    if seen > keep:
                        blocks[index] = {'type': 'text', 'text': '[earlier image omitted; read_artifact can '
                                                                  'return stored evidence again]'}
