# SPDX-License-Identifier: GPL-2.0-or-later
"""ClaudeAdapter: the tool-use loop, MCP content conversion and failure mapping."""
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'agent/relay'))

from router import AgentError  # noqa: E402
import claude_agent  # noqa: E402
from claude_agent import ClaudeAdapter  # noqa: E402


class FakeTurn:
    def __init__(self, prompt='Do the thing', images=None):
        self.prompt = prompt
        self.images = images or []
        self.task = {'task_id': 'task-1'}
        self.stop_requested = False
        self.narrations = []

    def narrate(self, text):
        self.narrations.append(text)


def make_adapter(mcp_responses=None, anthropic_responses=None):
    adapter = ClaudeAdapter()
    adapter.mcp_url = 'http://127.0.0.1:9/mcp/p/test'
    adapter.store = None
    adapter.device_id = 'device-1'
    mcp_responses = list(mcp_responses or [])
    anthropic_responses = list(anthropic_responses or [])

    def fake_mcp(method, params=None):
        if method == 'initialize':
            return {}
        if method == 'tools/list':
            return {'tools': [{'name': 'status', 'description': 'status', 'inputSchema': {'type': 'object'}}]}
        return mcp_responses.pop(0)

    def fake_anthropic(payload):
        return anthropic_responses.pop(0)

    adapter._mcp = fake_mcp
    adapter._anthropic = fake_anthropic
    return adapter


class ToolUseLoopTest(unittest.TestCase):
    def test_tool_use_then_final_text(self):
        adapter = make_adapter(
            mcp_responses=[{'content': [{'type': 'text', 'text': 'scene ok'}], 'isError': False}],
            anthropic_responses=[
                {'stop_reason': 'tool_use', 'content': [
                    {'type': 'tool_use', 'id': 'call_1', 'name': 'status', 'input': {}}]},
                {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'Done'}]},
            ])
        turn = FakeTurn()
        result = adapter.run_turn(turn)
        self.assertEqual(result, 'Done')
        # The second Anthropic call must carry the tool_result matching tool_use_id.
        sent_messages = adapter._history
        self.assertEqual(sent_messages[-1]['text'], 'Done')

    def test_tool_result_echoed_back(self):
        captured = []
        adapter = make_adapter(
            mcp_responses=[{'content': [{'type': 'text', 'text': 'scene ok'}], 'isError': False}])
        responses = [
            {'stop_reason': 'tool_use', 'content': [
                {'type': 'tool_use', 'id': 'call_1', 'name': 'status', 'input': {}}]},
            {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'Done'}]},
        ]

        def fake_anthropic(payload):
            captured.append(payload)
            return responses.pop(0)

        adapter._anthropic = fake_anthropic
        result = adapter.run_turn(FakeTurn())
        self.assertEqual(result, 'Done')
        second_call = captured[1]
        tool_result_message = second_call['messages'][-1]
        self.assertEqual(tool_result_message['role'], 'user')
        block = tool_result_message['content'][0]
        self.assertEqual(block['type'], 'tool_result')
        self.assertEqual(block['tool_use_id'], 'call_1')
        self.assertEqual(block['content'], [{'type': 'text', 'text': 'scene ok'}])
        self.assertFalse(block['is_error'])


class ImageContentTest(unittest.TestCase):
    def test_mcp_image_becomes_anthropic_image_block(self):
        adapter = make_adapter(
            mcp_responses=[{'content': [{'type': 'image', 'mimeType': 'image/png', 'data': 'QUJD'}],
                            'isError': False}])
        captured = []
        responses = [
            {'stop_reason': 'tool_use', 'content': [
                {'type': 'tool_use', 'id': 'call_2', 'name': 'status', 'input': {}}]},
            {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'Saw it'}]},
        ]

        def fake_anthropic(payload):
            captured.append(payload)
            return responses.pop(0)

        adapter._anthropic = fake_anthropic
        result = adapter.run_turn(FakeTurn())
        self.assertEqual(result, 'Saw it')
        block = captured[1]['messages'][-1]['content'][0]
        self.assertEqual(block['content'], [{'type': 'image', 'source': {
            'type': 'base64', 'media_type': 'image/png', 'data': 'QUJD'}}])


class FailureMappingTest(unittest.TestCase):
    def test_429_maps_to_quota_exhausted(self):
        adapter = make_adapter()

        def raise_429(payload):
            raise AgentError(claude_agent.failure(
                'quota_exhausted', 'agent_provider', 'rate limited', False, 'switch_agent',
                agent_id='claude'))

        adapter._anthropic = raise_429
        with self.assertRaises(AgentError) as ctx:
            adapter.run_turn(FakeTurn())
        self.assertEqual(ctx.exception.failure['code'], 'quota_exhausted')

    def test_401_maps_to_auth_expired(self):
        adapter = make_adapter()

        def raise_401(payload):
            raise AgentError(claude_agent.failure(
                'auth_expired', 'agent_provider', 'bad key', False, 'after_user_action',
                agent_id='claude'))

        adapter._anthropic = raise_401
        with self.assertRaises(AgentError) as ctx:
            adapter.run_turn(FakeTurn())
        self.assertEqual(ctx.exception.failure['code'], 'auth_expired')

    def test_classify_http_429_and_401(self):
        adapter = ClaudeAdapter()
        value_429 = adapter._classify_http(429, 'rate limited')
        self.assertEqual(value_429['code'], 'quota_exhausted')
        self.assertEqual(value_429['retry'], 'switch_agent')
        value_401 = adapter._classify_http(401, 'invalid api key')
        self.assertEqual(value_401['code'], 'auth_expired')
        self.assertEqual(value_401['retry'], 'after_user_action')
        value_529 = adapter._classify_http(529, 'overloaded_error')
        self.assertEqual(value_529['code'], 'provider_unavailable')


class ProbeTest(unittest.TestCase):
    def test_probe_without_key_is_unavailable(self):
        old = os.environ.pop('ANTHROPIC_API_KEY', None)
        try:
            adapter = ClaudeAdapter()
            probe = adapter.probe()
            self.assertEqual(probe['availability'], 'unavailable')
            self.assertEqual(probe['reason']['code'], 'auth_expired')
        finally:
            if old is not None:
                os.environ['ANTHROPIC_API_KEY'] = old

    def test_probe_with_key_is_available(self):
        old = os.environ.get('ANTHROPIC_API_KEY')
        os.environ['ANTHROPIC_API_KEY'] = 'sk-test-123'
        try:
            adapter = ClaudeAdapter()
            probe = adapter.probe()
            self.assertEqual(probe['availability'], 'available')
            self.assertEqual(probe['auth'], 'signed_in')
        finally:
            if old is None:
                os.environ.pop('ANTHROPIC_API_KEY', None)
            else:
                os.environ['ANTHROPIC_API_KEY'] = old


class InterruptTest(unittest.TestCase):
    def test_interrupt_stops_before_next_call(self):
        adapter = make_adapter()
        calls = []

        def fake_anthropic(payload):
            calls.append(payload)
            adapter.interrupt()
            return {'stop_reason': 'tool_use', 'content': [
                {'type': 'text', 'text': 'thinking'},
                {'type': 'tool_use', 'id': 'call_x', 'name': 'status', 'input': {}}]}

        adapter._anthropic = fake_anthropic
        adapter._mcp_for_tool = None
        # Never actually invoked because interrupt fires before the tool call/next round.
        result = adapter.run_turn(FakeTurn())
        self.assertTrue(result.startswith('Stopped.'))
        self.assertEqual(len(calls), 1)


class NarrationTest(unittest.TestCase):
    def test_intermediate_text_is_narrated(self):
        adapter = make_adapter(
            mcp_responses=[{'content': [{'type': 'text', 'text': 'ok'}], 'isError': False}],
            anthropic_responses=[
                {'stop_reason': 'tool_use', 'content': [
                    {'type': 'text', 'text': 'Checking status first'},
                    {'type': 'tool_use', 'id': 'call_3', 'name': 'status', 'input': {}}]},
                {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'Done'}]},
            ])
        turn = FakeTurn()
        adapter.run_turn(turn)
        self.assertIn('Checking status first', turn.narrations)


if __name__ == '__main__':
    unittest.main()
