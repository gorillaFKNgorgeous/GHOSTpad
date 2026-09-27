"""Chat cursor safety: a relay sequence restart must never make the device skip events."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'agent/relay'))
from store import Store


def load_insight():
    """Import agent/runtime/insight.py with a minimal stand-in for bpy."""
    bpy = types.ModuleType('bpy')
    bpy.types = types.SimpleNamespace(Operator=object, Panel=object,
                                      WindowManager=types.SimpleNamespace())
    bpy.props = types.SimpleNamespace(EnumProperty=lambda **_: None,
                                      StringProperty=lambda **_: None)
    bpy.context = types.SimpleNamespace(window_manager=types.SimpleNamespace(windows=[]))
    saved = sys.modules.get('bpy')
    sys.modules['bpy'] = bpy
    try:
        spec = importlib.util.spec_from_file_location('insight_under_test',
                                                      ROOT / 'agent/runtime/insight.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if saved is None:
            sys.modules.pop('bpy', None)
        else:
            sys.modules['bpy'] = saved
    return module


def assistant_texts(insight):
    return [m['text'] for m in insight.list_messages() if m['role'] == 'assistant']


class RelayCursorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.db.close()
        self.temp.cleanup()

    def store(self, name='a.sqlite'):
        store = Store(str(Path(self.temp.name) / name))
        self.stores.append(store)
        return store

    def test_stream_id_is_stable_for_a_database_and_new_for_another(self):
        first = self.store('a.sqlite')
        stream = first.chat_stream_id
        self.assertRegex(stream, r'^[a-f0-9]{32}$')
        first.db.close()
        self.stores.remove(first)
        self.assertEqual(self.store('a.sqlite').chat_stream_id, stream)
        self.assertNotEqual(self.store('b.sqlite').chat_stream_id, stream)

    def test_normal_paging_never_resets_and_stops_at_last_delivered(self):
        store = self.store()
        for i in range(60):
            store.chat_event('ipad', None, 'status', f'e{i}')
        stream = store.chat_stream_id
        page = store.chat_exchange('ipad', {'cursor': 0, 'messages': [], 'stream_id': stream})
        self.assertEqual((len(page['events']), page['cursor'], page['reset']), (50, 50, False))
        page = store.chat_exchange('ipad', {'cursor': 50, 'messages': [], 'stream_id': stream})
        self.assertEqual((len(page['events']), page['cursor'], page['reset']), (10, 60, False))
        page = store.chat_exchange('ipad', {'cursor': 60, 'messages': [], 'stream_id': stream})
        self.assertEqual((page['events'], page['cursor'], page['reset']), ([], 60, False))

    def test_cursor_ahead_of_database_resets_instead_of_claiming_delivery(self):
        store = self.store()
        store.chat_event('ipad', None, 'final', 'reply after relay reset')
        reply = store.chat_exchange('ipad', {'cursor': 60, 'messages': []})
        self.assertTrue(reply['reset'])
        self.assertEqual([e['text'] for e in reply['events']], ['reply after relay reset'])
        self.assertEqual(reply['cursor'], 1)

    def test_other_stream_id_replays_from_start(self):
        store = self.store()
        for text in ('one', 'two'):
            store.chat_event('ipad', None, 'final', text)
        reply = store.chat_exchange('ipad', {'cursor': 2, 'messages': [], 'stream_id': 'f' * 32})
        self.assertTrue(reply['reset'])
        self.assertEqual([e['seq'] for e in reply['events']], [1, 2])

    def test_malformed_stream_id_resets_rather_than_failing_the_exchange(self):
        store = self.store()
        reply = store.chat_exchange('ipad', {'cursor': 0, 'messages': [], 'stream_id': 12})
        self.assertTrue(reply['reset'])
        self.assertEqual(reply['stream_id'], store.chat_stream_id)

    def test_pruned_events_do_not_trigger_a_reset(self):
        store = self.store()
        for i in range(3):
            store.chat_event('ipad', None, 'status', f'e{i}')
        with store.db:
            store.db.execute('DELETE FROM chat_events')  # as 7-day retention does
        reply = store.chat_exchange('ipad', {'cursor': 3, 'messages': [],
                                             'stream_id': store.chat_stream_id})
        self.assertFalse(reply['reset'])
        self.assertEqual((reply['events'], reply['cursor']), ([], 3))
        store.chat_event('ipad', None, 'final', 'next')
        reply = store.chat_exchange('ipad', {'cursor': 3, 'messages': [],
                                             'stream_id': store.chat_stream_id})
        self.assertEqual([(e['seq'], e['text']) for e in reply['events']], [(4, 'next')])


class DeviceCursorTests(unittest.TestCase):
    def setUp(self):
        self.insight = load_insight()

    def test_device_sends_stream_id_once_known(self):
        self.assertNotIn('stream_id', self.insight.chat_payload())
        self.insight.apply_chat({'cursor': 0, 'ack_ids': [], 'events': [], 'stream_id': 'a' * 32})
        self.assertEqual(self.insight.chat_payload()['stream_id'], 'a' * 32)

    def test_stream_change_resets_cursor(self):
        insight = self.insight
        insight.apply_chat({'cursor': 3, 'ack_ids': [], 'stream_id': 'a' * 32, 'events': [
            {'seq': 3, 'message_id': None, 'type': 'final', 'text': 'old stream'}]})
        insight.apply_chat({'cursor': 1, 'ack_ids': [], 'stream_id': 'b' * 32, 'events': [
            {'seq': 1, 'message_id': None, 'type': 'final', 'text': 'new stream'}]})
        self.assertEqual(assistant_texts(insight), ['old stream', 'new stream'])
        self.assertEqual(insight.chat_payload()['cursor'], 1)

    def test_reset_flag_resets_cursor(self):
        insight = self.insight
        insight.apply_chat({'cursor': 9, 'ack_ids': [], 'stream_id': 'a' * 32, 'events': [
            {'seq': 9, 'message_id': None, 'type': 'final', 'text': 'first'}]})
        insight.apply_chat({'cursor': 2, 'ack_ids': [], 'stream_id': 'a' * 32, 'reset': True, 'events': [
            {'seq': 2, 'message_id': None, 'type': 'final', 'text': 'after rollback'}]})
        self.assertEqual(assistant_texts(insight), ['first', 'after rollback'])
        self.assertEqual(insight.chat_payload()['cursor'], 2)

    def test_old_relay_without_stream_id_keeps_previous_behaviour(self):
        insight = self.insight
        insight.apply_chat({'cursor': 5, 'ack_ids': [], 'events': [
            {'seq': 5, 'message_id': None, 'type': 'final', 'text': 'five'}]})
        insight.apply_chat({'cursor': 1, 'ack_ids': [], 'events': []})
        self.assertEqual(insight.chat_payload()['cursor'], 5)


class EndToEndCursorTests(unittest.TestCase):
    """The CURRENT-SHAPES §7 scenario: the relay database is replaced while the app keeps running."""

    def test_reply_after_relay_database_reset_reaches_the_device(self):
        with tempfile.TemporaryDirectory() as temp:
            insight = load_insight()
            old = Store(str(Path(temp) / 'old.sqlite'))
            for i in range(60):
                old.chat_event('ipad', None, 'status', f'e{i}')
            while True:
                reply = old.chat_exchange('ipad', insight.chat_payload())
                insight.apply_chat(reply)
                if not reply['events']:
                    break
            self.assertEqual(insight.chat_payload()['cursor'], 60)
            old.db.close()

            new = Store(str(Path(temp) / 'new.sqlite'))
            new.chat_event('ipad', None, 'final', 'The active object is GB_Rig.')
            insight.apply_chat(new.chat_exchange('ipad', insight.chat_payload()))
            new.db.close()
            self.assertEqual(assistant_texts(insight), ['The active object is GB_Rig.'])
            self.assertEqual(insight.chat_payload()['cursor'], 1)


if __name__ == '__main__':
    unittest.main()
