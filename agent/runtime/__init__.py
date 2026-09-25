# SPDX-License-Identifier: GPL-2.0-or-later
"""Bundled startup package: one-time pairing, persistent main-thread dispatcher."""
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

import bpy
from bpy.app.handlers import persistent
from bpy.props import PointerProperty, StringProperty
from .core import Runtime, atomic_json
from . import insight

try:
    import _ghostbridge_transport as native
except ImportError:
    native = None

_runtime = None
_config = {}
_waiting = False
_next_request = 0.0
_failures = 0
_status = 'Disconnected'
_root = None


def _token_fingerprint(token):
    if not isinstance(token, str) or not token:
        return ''
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def _save_config():
    path = _root / 'config.json'
    atomic_json(path, _config)
    # Verify the exact pairing token survived the filesystem write before the UI
    # reports that the connection was saved. This makes persistence failures
    # explicit instead of looking like an authentication failure later.
    saved = json.loads(path.read_text())
    if saved.get('device_token') != _config.get('device_token'):
        raise RuntimeError('pairing_token_persistence_failed')


def _disconnect():
    global _waiting, _status
    if native:
        native.cancel()
    _waiting = False
    _config['enabled'] = False
    _save_config()
    _status = 'Disconnected'


@persistent
def _load_post(_):
    if _runtime:
        _runtime.scene_changed()


def _tick():
    global _runtime, _waiting, _next_request, _failures, _status
    try:
        if not native or not _config.get('enabled'):
            return 1.0
        if not native.status()['foreground']:
            _status = 'Paused while app is inactive'
            return 1.0
        if not _runtime:
            _runtime = Runtime(bpy, native, _root)
        if _waiting:
            response = native.poll()
            if response is None:
                return 0.15
            _waiting = False
            if response.get('status') in (401, 403):
                fingerprint = _token_fingerprint(_config.get('device_token', ''))
                _disconnect()
                _status = 'Pairing rejected; saved token sha12=' + (fingerprint or 'missing')
                return 1.0
            if response.get('status') != 200:
                raise RuntimeError(response.get('error', 'relay_http_error'))
            data = json.loads(response['body'])
            if data.get('protocol') != 1:
                raise RuntimeError('incompatible_relay')
            insight.apply_chat(data.get('chat'))
            if data.get('ack'):
                _runtime.acknowledge(data['ack'])
            _status = 'Connected'
            _failures = 0
            job = data.get('job')
            if job:
                _status = 'Executing ' + str(job.get('operation', 'command'))
                _runtime.execute(job)
                _status = 'Connected'
            _next_request = time.monotonic() + (0.05 if _runtime.outbox else 1.0)
        if not _waiting and time.monotonic() >= _next_request:
            body = {'protocol': 1, 'device_id': _config['device_id'],
                    'heartbeat': _runtime.heartbeat(), 'completed': _runtime.outbox,
                    'chat': insight.chat_payload()}
            native.request(_config['relay_url'] + '/device/exchange', _config['device_token'],
                           json.dumps(body, separators=(',', ':'), allow_nan=False))
            _waiting = True
        return 0.15
    except Exception as exc:
        # Keep the timer alive after a transient network or tool error. Never log secrets.
        _failures += 1
        _status = 'Reconnecting (' + type(exc).__name__ + ')'
        _next_request = time.monotonic() + min(30.0, 2 ** min(_failures, 5))
        if native:
            native.cancel()
        _waiting = False
        return 1.0


class GBSettings(bpy.types.PropertyGroup):
    relay_url: StringProperty(name='Relay URL', options={'SKIP_SAVE'})
    device_id: StringProperty(name='Device ID', default='ipad', options={'SKIP_SAVE'})
    device_token: StringProperty(name='Device token', subtype='PASSWORD', options={'SKIP_SAVE'})


class GBConnect(bpy.types.Operator):
    bl_idname = 'ghostbridge.connect'
    bl_label = 'Connect Agent'
    bl_description = 'Authorize the paired agent to inspect, capture and run Python in this app'

    def execute(self, context):
        global _config, _next_request, _runtime, _waiting, _status
        settings = context.window_manager.ghostbridge
        url = settings.relay_url.strip().rstrip('/')
        token = settings.device_token.strip() or _config.get('device_token', '')
        parts = urlsplit(url)
        if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
                or parts.query or parts.fragment or parts.path not in ('', '/')):
            self.report({'ERROR'}, 'Enter the HTTPS relay origin, without a path')
            return {'CANCELLED'}
        device_id = settings.device_id.strip()
        if not device_id or len(device_id) > 80 or len(token) < 32 or '\n' in token or '\r' in token:
            self.report({'ERROR'}, 'A device ID and pairing token of at least 32 characters are required')
            return {'CANCELLED'}
        native.cancel()
        _waiting = False
        _config = {'relay_url': url, 'device_id': device_id, 'device_token': token, 'enabled': True}
        try:
            _save_config()
        except Exception:
            _config['enabled'] = False
            self.report({'ERROR'}, 'Could not persist pairing token in the app sandbox')
            _status = 'Pairing token persistence failed'
            return {'CANCELLED'}
        # Keep the password field populated in memory so the UI visibly reflects
        # that a token exists. SKIP_SAVE prevents it being written into .blend files.
        settings.device_token = token
        _next_request = 0.0
        _status = 'Connecting; token saved sha12=' + _token_fingerprint(token)
        return {'FINISHED'}


class GBDisconnect(bpy.types.Operator):
    bl_idname = 'ghostbridge.disconnect'
    bl_label = 'Disconnect Agent'
    def execute(self, context):
        _disconnect()
        return {'FINISHED'}


class GBPanel(bpy.types.Panel):
    bl_label = 'Agent Connection'
    bl_idname = 'GHOSTBRIDGE_PT_connection'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'GhostBlender'

    def draw(self, context):
        layout = self.layout
        settings = context.window_manager.ghostbridge
        layout.label(text=_status)
        saved = _config.get('device_token', '')
        if saved:
            layout.label(text='Saved device token: sha12=' + _token_fingerprint(saved))
        else:
            layout.label(text='Saved device token: none')
        if _config.get('enabled'):
            layout.operator('ghostbridge.disconnect')
        else:
            layout.prop(settings, 'relay_url')
            layout.prop(settings, 'device_id')
            layout.prop(settings, 'device_token')
            layout.label(text='Agent access includes Python and screen capture.')
            layout.operator('ghostbridge.connect')


_CLASSES = (GBSettings, GBConnect, GBDisconnect, GBPanel)


def register():
    global _config, _root, _status
    if not native:
        return
    _root = Path(bpy.utils.user_resource('CONFIG', path='ghostbridge', create=True))
    try:
        path = _root / 'config.json'
        _config = json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        _config = {}
        _status = 'Pairing configuration could not be read'
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.ghostbridge = PointerProperty(type=GBSettings)
    insight.register()
    # WindowManager may not exist during early startup; fill the fields on the first tick.
    def fill_settings():
        wm = bpy.context.window_manager
        if not wm:
            return 0.5
        wm.ghostbridge.relay_url = _config.get('relay_url', '')
        wm.ghostbridge.device_id = _config.get('device_id', 'ipad')
        wm.ghostbridge.device_token = _config.get('device_token', '')
        return None
    bpy.app.timers.register(fill_settings, first_interval=1.0)
    bpy.app.handlers.load_post.append(_load_post)
    bpy.app.timers.register(_tick, first_interval=1.5, persistent=True)


def unregister():
    global _waiting, _runtime
    if not native:
        return
    native.cancel()
    _waiting = False
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    if _load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_load_post)
    insight.unregister()
    del bpy.types.WindowManager.ghostbridge
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
    _runtime = None
