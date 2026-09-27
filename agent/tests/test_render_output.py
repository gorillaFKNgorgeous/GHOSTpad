"""Renders into unwritable folders are redirected into Documents/renders and restored after."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[2]


def load(home):
    handlers = types.SimpleNamespace(render_init=[], render_complete=[], render_cancel=[], persistent=lambda f: f)
    bpy = types.ModuleType('bpy')
    bpy.path = types.SimpleNamespace(abspath=lambda p: p)
    bpy.data = types.SimpleNamespace(filepath=str(Path(home) / 'shot.blend'))
    bpy.app = types.SimpleNamespace(handlers=handlers)
    app_mod = types.ModuleType('bpy.app')
    handlers_mod = types.ModuleType('bpy.app.handlers')
    handlers_mod.persistent = lambda f: f
    sys.modules.update({'bpy': bpy, 'bpy.app': app_mod, 'bpy.app.handlers': handlers_mod})
    spec = importlib.util.spec_from_file_location('render_output_under_test', ROOT / 'agent/runtime/render_output.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, bpy


class RenderOutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.old_home = os.environ.get('HOME')
        os.environ['HOME'] = str(self.home)
        self.module, self.bpy = load(self.home)
        self.locked = self.home / 'provider'
        self.locked.mkdir()
        os.chmod(self.locked, 0o500)

    def tearDown(self):
        os.chmod(self.locked, 0o700)
        if self.old_home is None:
            os.environ.pop('HOME', None)
        else:
            os.environ['HOME'] = self.old_home
        for name in ('bpy', 'bpy.app', 'bpy.app.handlers'):
            sys.modules.pop(name, None)
        self.temp.cleanup()

    def test_unwritable_output_is_redirected_then_restored_and_offered(self):
        # Like a Files provider folder: it exists but the app may not write to it.
        locked = str(self.locked)
        writable = self.module._writable
        self.module._writable = lambda d: d != locked and writable(d)
        exported = []
        native = types.SimpleNamespace(export_files=exported.append)
        self.module.register(native)
        original = str(self.locked / 'shot.mp4')
        scene = types.SimpleNamespace(name='Scene', render=types.SimpleNamespace(filepath=original))
        self.module._on_render_init(scene)
        self.assertEqual(scene.render.filepath, str(self.home / 'Documents' / 'renders' / 'shot.mp4'))
        Path(scene.render.filepath).write_bytes(b'movie')
        self.module._on_render_complete(scene)
        self.assertEqual(scene.render.filepath, original)
        self.assertEqual(len(exported), 1)
        self.assertIn('shot.mp4', exported[0])
        self.assertIn(str(self.locked), exported[0])

    def test_writable_output_is_left_alone(self):
        target = self.home / 'out' / 'shot.mp4'
        target.parent.mkdir()
        self.assertIsNone(self.module.redirect_target(str(target)))

    def test_directory_only_output_gets_a_name_from_the_blend(self):
        _, redirected = self.module.redirect_target('/nonexistent-folder/', str(self.home / 'shot.blend'))
        self.assertTrue(redirected.endswith('shot_'))


if __name__ == '__main__':
    unittest.main()
