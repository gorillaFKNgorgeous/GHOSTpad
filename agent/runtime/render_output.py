# SPDX-License-Identifier: GPL-2.0-or-later
"""Renders whose output folder the app may not write to still succeed.

On iPadOS a .blend saved into a Files location (iCloud Drive, On My iPad
folders of other apps, external providers) is reached through a one-time,
security-scoped grant for that file only. Rendering "next to the .blend", or
to Blender's desktop default /tmp/, then fails because the folder is not
writable by GHOSTpad.

At render start the output is redirected into GHOSTpad's own
Documents/renders folder. When the render ends the scene's path is restored
(the .blend keeps the user's intent) and the native layer offers the iPad
"Save to..." sheet, opened at the original folder, to move the files there.
"""
import os
from pathlib import Path
import time

import bpy
from bpy.app.handlers import persistent

_native = None
_pending = None


def renders_dir():
    path = Path.home() / 'Documents' / 'renders'
    path.mkdir(parents=True, exist_ok=True)
    return path


def _writable(directory):
    try:
        return bool(directory) and os.path.isdir(directory) and os.access(directory, os.W_OK)
    except OSError:
        return False


def redirect_target(filepath, blend_path=''):
    """(original_dir, redirected_filepath), or None when the output is writable as set."""
    absolute = bpy.path.abspath(filepath) if filepath else ''
    directory = absolute if absolute.endswith(('/', os.sep)) else os.path.dirname(absolute)
    if _writable(directory):
        return None
    name = os.path.basename(absolute.rstrip('/'))
    if not name or absolute.endswith(('/', os.sep)):
        stem = Path(blend_path).stem if blend_path else 'render'
        name = f'{stem}_'
    return directory, str(renders_dir() / name)


@persistent
def _on_render_init(scene, *_):
    global _pending
    _pending = None
    try:
        target = redirect_target(scene.render.filepath, bpy.data.filepath)
    except Exception:
        return
    if target is None:
        return
    original_dir, redirected = target
    _pending = {'scene': scene.name, 'original': scene.render.filepath, 'original_dir': original_dir,
                'redirected': redirected, 'started': time.time(),
                'before': set(os.listdir(renders_dir()))}
    scene.render.filepath = redirected
    print(f'GhostBlender: render output folder is not writable; rendering to {redirected}')


def _finish(scene, completed):
    global _pending
    pending, _pending = _pending, None
    if not pending:
        return
    try:
        if scene.render.filepath == pending['redirected']:
            scene.render.filepath = pending['original']
    except Exception:
        pass
    directory = renders_dir()
    prefix = os.path.basename(pending['redirected'])
    created = sorted(str(directory / name) for name in os.listdir(directory)
                     if name not in pending['before'] and name.startswith(prefix))
    if not created or not completed:
        return
    if _native is not None and hasattr(_native, 'export_files'):
        import json
        _native.export_files(json.dumps({
            'paths': created,
            'directory': pending['original_dir'] if os.path.isdir(pending['original_dir']) else '',
            'title': f'Rendered {len(created)} file(s) into GHOSTpad/renders because the chosen output '
                     f'folder is not writable. Choose where to move them.'}))


@persistent
def _on_render_complete(scene, *_):
    _finish(scene, True)


@persistent
def _on_render_cancel(scene, *_):
    _finish(scene, False)


_HANDLERS = (('render_init', _on_render_init), ('render_complete', _on_render_complete),
             ('render_cancel', _on_render_cancel))


def register(native=None):
    global _native
    _native = native
    for name, handler in _HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if handler not in handlers:
            handlers.append(handler)


def unregister():
    for name, handler in _HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if handler in handlers:
            handlers.remove(handler)
