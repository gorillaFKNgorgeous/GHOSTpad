# SPDX-License-Identifier: GPL-2.0-or-later
"""Main-thread-only runtime tools and a durable, at-most-once command journal."""
from __future__ import annotations
import base64
import contextlib
import hashlib
import io
import itertools
import json
import os
from pathlib import Path
import re
import sys
import time
import traceback
import uuid

MAX_CODE = 96_000
MAX_TEXT = 24_000
MAX_IMAGE = 1_500_000
ID_RE = re.compile(r'^[a-zA-Z0-9_-]{8,80}$')
SCRIPT_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}\.py$')


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    with open(temp, 'w', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=True, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(temp, 0o600)
    os.replace(temp, path)


class BoundedOutput(io.TextIOBase):
    def __init__(self):
        self.parts, self.count, self.truncated = [], 0, False

    def write(self, text):
        available = max(0, MAX_TEXT - self.count)
        self.parts.append(text[:available]) if available else None
        self.count += min(len(text), available)
        self.truncated |= len(text) > available
        return len(text)

    def getvalue(self):
        return ''.join(self.parts) + ('\n[output truncated]' if self.truncated else '')


class Runtime:
    def __init__(self, bpy, native, root):
        self.bpy, self.native, self.root = bpy, native, Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.scripts = self.root / 'scripts'
        self.scripts.mkdir(exist_ok=True)
        self.boot_id = uuid.uuid4().hex
        self.scene_id = uuid.uuid4().hex
        self._scene_pointer = None
        self.started = time.time()
        self.state_path = self.root / 'journal.json'
        self.journal = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        for entry in self.journal.values():
            if entry['state'] == 'running':
                entry.update(state='uncertain', error='app_restarted_during_command')
        self.outbox_path = self.root / 'outbox.json'
        self.outbox = json.loads(self.outbox_path.read_text()) if self.outbox_path.exists() else None
        self._save()

    def _save(self):
        # Keep the most recent 256 command records; relay owns older history.
        self.journal = dict(list(self.journal.items())[-256:])
        atomic_json(self.state_path, self.journal)

    def scene_changed(self):
        self.scene_id = uuid.uuid4().hex
        self._scene_pointer = None

    def _sync_scene(self):
        context = getattr(self.bpy, 'context', None)
        scene = getattr(context, 'scene', None)
        if scene is not None:
            pointer = scene.as_pointer()
            if self._scene_pointer is not None and self._scene_pointer != pointer:
                self.scene_id = uuid.uuid4().hex
            self._scene_pointer = pointer

    def heartbeat(self):
        self._sync_scene()
        return {'boot_id': self.boot_id, 'scene_id': self.scene_id,
                'blender_version': self.bpy.app.version_string,
                'python_version': sys.version.split()[0],
                'scene_name': self.bpy.context.scene.name,
                'native': self.native.status(), 'bridge_version': '0.1.0',
                'observed': self.observed()}

    def observed(self):
        """What the person at the iPad is doing, so a teaching agent can follow along (target.md §19)."""
        try:
            context = self.bpy.context
            layer = context.view_layer
            active = layer.objects.active if layer else None
            return {'mode': context.mode, 'active': active.name if active else None,
                    'selected': [o.name for o in context.selected_objects][:12],
                    'object_count': len(context.scene.objects), 'frame': context.scene.frame_current,
                    'unsaved_changes': bool(self.bpy.data.is_dirty),
                    'file': Path(self.bpy.data.filepath).name if self.bpy.data.filepath else None,
                    'rendering': bool(self.bpy.app.is_job_running('RENDER'))}
        except Exception:
            return None

    def acknowledge(self, job_id):
        if self.outbox and self.outbox['job_id'] == job_id:
            self.outbox = None
            self.outbox_path.unlink(missing_ok=True)

    def execute(self, job):
        """Journal before execution. A duplicate or changed scene never repeats an edit."""
        self._sync_scene()
        job_id = job.get('job_id', '')
        if not isinstance(job_id, str) or not ID_RE.fullmatch(job_id):
            raise ValueError('invalid_job_id')
        if self.outbox:
            raise RuntimeError('result_waiting_for_acknowledgement')
        digest = hashlib.sha256(json.dumps(job, sort_keys=True).encode()).hexdigest()
        if job_id in self.journal:
            result = {'ok': False, 'error': 'duplicate_command_not_reexecuted',
                      'record': self.journal[job_id]}
        else:
            self.journal[job_id] = {'state': 'running', 'operation': job.get('operation'),
                                    'started_at': time.time(), 'digest': digest}
            self._save()
            try:
                if job.get('boot_id') != self.boot_id or job.get('scene_id') != self.scene_id:
                    raise ValueError('scene_or_session_changed; inspect the current scene again')
                if time.time() > job.get('expires_at', 0):
                    raise ValueError('command_expired')
                if not self.native.status()['foreground']:
                    raise ValueError('app_not_foreground')
                if self.bpy.app.is_job_running('RENDER'):
                    raise ValueError('render_in_progress; wait before changing the scene')
                value = self.dispatch(job['operation'], job.get('arguments', {}))
                result = {'ok': True, 'value': value, 'scene_id': self.scene_id}
            except BaseException as exc:
                # A script's SystemExit must not unregister the dispatcher.
                result = {'ok': False, 'error': str(exc)[:2000],
                          'traceback': traceback.format_exc()[-MAX_TEXT:]}
            self.journal[job_id].update(state='completed' if result['ok'] else 'failed',
                                       ended_at=time.time())
            self._save()
        self.outbox = {'job_id': job_id, 'boot_id': job['boot_id'], 'result': result}
        atomic_json(self.outbox_path, self.outbox)
        return self.outbox

    def dispatch(self, operation, args):
        handlers = {'inspect_scene': self.inspect_scene, 'execute_python': self.execute_python,
                    'capture': self.capture, 'diagnostics': self.diagnostics,
                    'list_scripts': self.list_scripts, 'read_script': self.read_script,
                    'write_script': self.write_script}
        if operation not in handlers:
            raise ValueError('unknown_operation')
        return handlers[operation](**args)

    def inspect_scene(self, offset=0, limit=80):
        bpy = self.bpy
        offset, limit = int(offset), min(200, max(1, int(limit)))
        if offset < 0:
            raise ValueError('negative_offset')
        scene = bpy.context.scene
        objects = []
        for obj in itertools.islice(scene.objects, offset, offset + limit):
            item = {'name': obj.name, 'type': obj.type, 'location': list(obj.location),
                    'rotation_euler': list(obj.rotation_euler), 'scale': list(obj.scale),
                    'dimensions': list(obj.dimensions), 'parent': obj.parent.name if obj.parent else None,
                    'materials': [s.material.name if s.material else None for s in obj.material_slots],
                    'modifiers': [{'name': m.name, 'type': m.type} for m in obj.modifiers]}
            if obj.type == 'MESH':
                item['mesh'] = {'vertices': len(obj.data.vertices), 'polygons': len(obj.data.polygons)}
            objects.append(item)
        return {'scene_id': self.scene_id, 'boot_id': self.boot_id, 'scene_name': scene.name,
                'object_count': len(scene.objects), 'offset': offset, 'objects': objects,
                'selected': [o.name for o in bpy.context.selected_objects],
                'active': bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None,
                'mode': bpy.context.mode, 'camera': scene.camera.name if scene.camera else None,
                'render_engine': scene.render.engine, 'frame': scene.frame_current,
                'filepath': bpy.data.filepath, 'unsaved_changes': bpy.data.is_dirty,
                'collections': [c.name for c in bpy.data.collections][:200]}

    def execute_python(self, code, time_limit=5.0):
        """Privileged Python, not a security sandbox. Time limit is Python-cooperative only."""
        if not isinstance(code, str) or len(code.encode()) > MAX_CODE:
            raise ValueError('code_too_large')
        timeout = min(15.0, max(0.1, float(time_limit)))
        compiled = compile(code, '<GhostBlender agent>', 'exec')
        namespace = {'bpy': self.bpy, '__name__': '__ghostblender_agent__', 'result': None,
                     'scripts_dir': str(self.scripts)}
        output = BoundedOutput()
        deadline = time.monotonic() + timeout
        previous_trace = sys.gettrace()
        def trace(frame, event, arg):
            if time.monotonic() > deadline:
                raise TimeoutError('Python time limit reached; partial edits may exist')
            return trace
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                sys.settrace(trace)
                exec(compiled, namespace, namespace)
        except BaseException as exc:
            raise RuntimeError(f'{type(exc).__name__}: {exc}\n{output.getvalue()}') from exc
        finally:
            sys.settrace(previous_trace)
        self.bpy.context.view_layer.update()
        for window in self.bpy.context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()
        value = namespace.get('result')
        # Require explicit JSON results; do not stringify arbitrary RNA objects.
        encoded = json.dumps(value, allow_nan=False)
        if len(encoded.encode()) > MAX_TEXT:
            raise ValueError('result_too_large; script already executed, inspect before retrying')
        return {'stdout': output.getvalue(), 'result': value,
                'elapsed_seconds': timeout - max(0, deadline - time.monotonic())}

    def capture(self, source='screenshot', max_size=1024):
        bpy = self.bpy
        max_size = min(1536, max(128, int(max_size)))
        path = self.root / 'capture.png'
        if source == 'screenshot':
            windows = bpy.context.window_manager.windows
            if not windows:
                raise RuntimeError('no_window_available')
            with bpy.context.temp_override(window=windows[0]):
                status = bpy.ops.screen.screenshot(filepath=str(path))
            if 'FINISHED' not in status:
                raise RuntimeError('screenshot_failed')
        elif source == 'render_result':
            source_image = bpy.data.images.get('Render Result')
            if not source_image:
                raise ValueError('no_render_result; run a render first')
            # A finished Render Result reports has_data=False and size 0x0 on device
            # (its pixels live in the render, not an image buffer), so only a failed
            # save means there is nothing to capture.
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            # Saving a Render Result uses the active scene's color management.
            settings = bpy.context.scene.render.image_settings
            old_format = settings.file_format
            try:
                settings.file_format = 'PNG'
                source_image.save_render(str(path), scene=bpy.context.scene)
            except RuntimeError as exc:
                raise ValueError('no_render_result; run a render first') from exc
            finally:
                settings.file_format = old_format
            if not path.is_file() or not path.stat().st_size:
                raise ValueError('no_render_result; run a render first')
        else:
            raise ValueError('source must be screenshot or render_result')
        image = bpy.data.images.load(str(path), check_existing=False)
        try:
            width, height = image.size
            if not width or not height:
                raise RuntimeError('empty_capture')
            factor = min(1.0, max_size / max(width, height))
            image.scale(max(1, int(width * factor)), max(1, int(height * factor)))
            image.file_format = 'PNG'
            image.filepath_raw = str(path)
            image.save()
            size = tuple(image.size)
        finally:
            bpy.data.images.remove(image)
        data = path.read_bytes()
        if len(data) > MAX_IMAGE:
            raise ValueError('image_too_large; request a smaller max_size')
        return {'mime_type': 'image/png', 'data': base64.b64encode(data).decode(),
                'width': size[0], 'height': size[1], 'source': source}

    def diagnostics(self, log_tail=12000):
        limit = min(MAX_TEXT, max(0, int(log_tail)))
        logs = {}
        # Narrow built-in reader. Privileged execute_python is intentionally broader.
        for name in ('BlenderFiles.log', 'BlenderRuntimeProbe.txt'):
            path = Path.home() / 'Documents' / name
            if path.is_file():
                with open(path, 'rb') as f:
                    f.seek(max(0, path.stat().st_size - limit))
                    logs[name] = f.read(limit).decode('utf-8', errors='replace')
        return {**self.heartbeat(), 'uptime_seconds': time.time() - self.started,
                'build_hash': bpy_bytes(self.bpy.app.build_hash),
                'commands': list(self.journal.items())[-20:], 'logs': logs}

    def _script_path(self, name):
        if not isinstance(name, str) or not SCRIPT_RE.fullmatch(name) or '..' in name:
            raise ValueError('invalid_script_name')
        path = self.scripts / name
        if path.is_symlink() or path.resolve().parent != self.scripts.resolve():
            raise ValueError('script_path_outside_workspace')
        return path

    def list_scripts(self):
        return [{'name': p.name, 'bytes': p.stat().st_size} for p in self.scripts.glob('*.py')
                if p.is_file() and not p.is_symlink()][:200]

    def read_script(self, name):
        path = self._script_path(name)
        data = path.read_bytes()
        if len(data) > MAX_CODE:
            raise ValueError('script_too_large')
        return {'name': name, 'code': data.decode(), 'sha256': hashlib.sha256(data).hexdigest()}

    def write_script(self, name, code, expected_sha256=None):
        path = self._script_path(name)
        if not isinstance(code, str) or len(code.encode()) > MAX_CODE:
            raise ValueError('script_too_large')
        compile(code, name, 'exec')
        if path.exists():
            current = hashlib.sha256(path.read_bytes()).hexdigest()
            if expected_sha256 != current:
                raise ValueError('script_changed; read current script before replacing')
        elif expected_sha256 is not None:
            raise ValueError('script_no_longer_exists')
        temp = path.with_suffix('.py.tmp')
        with open(temp, 'w', encoding='utf-8') as f:
            f.write(code)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        return {'name': name, 'sha256': hashlib.sha256(code.encode()).hexdigest(), 'executed': False}


def bpy_bytes(value):
    return value.decode(errors='replace') if isinstance(value, bytes) else str(value)
