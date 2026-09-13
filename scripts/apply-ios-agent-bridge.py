#!/usr/bin/env python3
"""Install GhostBlender's native transport against the audited source tree."""
from pathlib import Path
import shutil
import sys


def transform(root: Path, harness: Path):
    interface = root / 'source/blender/python/intern/bpy_interface.cc'
    cmake = interface.parent / 'CMakeLists.txt'
    content = interface.read_text()
    replacements = [
        ('#include <Python.h>\n', '#include <Python.h>\n#ifdef WITH_GHOSTBRIDGE_IOS\nextern "C" PyObject *PyInit__ghostbridge_transport(void);\n#endif\n'),
        ('static _inittab bpy_internal_modules[] = {\n', 'static _inittab bpy_internal_modules[] = {\n#ifdef WITH_GHOSTBRIDGE_IOS\n    {"_ghostbridge_transport", PyInit__ghostbridge_transport},\n#endif\n'),
    ]
    if 'WITH_GHOSTBRIDGE_IOS' in content:
        raise RuntimeError('Agent bridge already installed; use a clean source checkout')
    for old, new in replacements:
        if content.count(old) != 1:
            raise RuntimeError(f'Pinned bpy_interface.cc anchor changed: {old!r}')
        content = content.replace(old, new, 1)
    cm = cmake.read_text()
    anchor = 'blender_add_lib(bf_python "${SRC}" "${INC}" "${INC_SYS}" "${LIB}")'
    if cm.count(anchor) != 1 or 'ghostbridge_transport.mm' in cm:
        raise RuntimeError('Pinned bf_python CMake anchor changed')
    addition = '''if(WITH_APPLE_CROSSPLATFORM)
  list(APPEND SRC ghostbridge_transport.mm)
  add_definitions(-DWITH_GHOSTBRIDGE_IOS)
  set_source_files_properties(ghostbridge_transport.mm PROPERTIES COMPILE_FLAGS "-fobjc-arc")
  # Blender's cross-platform setup currently points CMAKE_SYSTEM_FRAMEWORK_PATH at
  # the platform directory rather than the selected iPhoneOS SDK. Resolve these
  # two bridge-only system frameworks explicitly inside CMAKE_OSX_SYSROOT so the
  # rest of Blender's dependency search behavior remains untouched.
  set(GHOSTBRIDGE_IOS_FRAMEWORKS "${CMAKE_OSX_SYSROOT}/System/Library/Frameworks")
  find_library(GHOSTBRIDGE_FOUNDATION
    NAMES Foundation
    PATHS "${GHOSTBRIDGE_IOS_FRAMEWORKS}"
    NO_DEFAULT_PATH
    REQUIRED
  )
  find_library(GHOSTBRIDGE_UIKIT
    NAMES UIKit
    PATHS "${GHOSTBRIDGE_IOS_FRAMEWORKS}"
    NO_DEFAULT_PATH
    REQUIRED
  )
  list(APPEND LIB ${GHOSTBRIDGE_FOUNDATION} ${GHOSTBRIDGE_UIKIT})
endif()

'''
    target = root / 'scripts/startup/ghostbridge'
    if target.exists():
        raise RuntimeError(f'Refusing to overwrite {target}')
    # Validate every anchor and destination before modifying source files.
    interface.write_text(content)
    cmake.write_text(cm.replace(anchor, addition + anchor, 1))
    shutil.copyfile(harness / 'agent/native/ghostbridge_transport.mm', interface.parent / 'ghostbridge_transport.mm')
    shutil.copytree(harness / 'agent/runtime', target, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))

    # Surface the persistent native render diagnostics through the existing MCP diagnostics
    # operation. This is intentionally a build-time transform so the source runtime can stay
    # platform-neutral while the iPad package exposes its native Documents logs after a crash.
    runtime_core = target / 'core.py'
    runtime_content = runtime_core.read_text()
    log_anchor = "for name in ('BlenderFiles.log', 'BlenderRuntimeProbe.txt'):"
    log_replacement = (
        "for name in ('BlenderFiles.log', 'BlenderRuntimeProbe.txt', "
        "'BlenderRenderCache.log', 'BlenderMetalRedraw.log'):"
    )
    if runtime_content.count(log_anchor) != 1:
        raise RuntimeError('Pinned GhostBlender diagnostics log anchor changed')
    runtime_core.write_text(runtime_content.replace(log_anchor, log_replacement, 1))

    print('Installed native transport and persistent GhostBlender startup dispatcher')


if __name__ == '__main__':
    transform(Path(sys.argv[1]).resolve(), Path(__file__).resolve().parents[1])
