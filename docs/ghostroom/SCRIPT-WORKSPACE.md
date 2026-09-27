# Persistent script workspace: what it is and what can run from it

Written before any change to `write_script` semantics. Source references are to
`main` at `dcde3a9`. "Live" facts come from read-only MCP calls against the
running iPad on 2026-09-27 (`status`, `diagnostics`, `list_scripts`,
`read_script`). Anything not settled by source or those calls is marked
**UNKNOWN**, with the probe that would settle it.

## Summary

| question | answer | confidence |
| --- | --- | --- |
| Where are scripts stored? | `<Blender user CONFIG>/ghostbridge/scripts/*.py` in the app sandbox | source |
| Do they survive app restarts? | Yes: plain files, fsynced and atomically replaced | source + live |
| Does the bridge import or execute them at startup? | **No** | source |
| Does Blender auto-load that directory? | Not by Blender's standard resource layout | source (not verified on the iPad build) |
| What loads them? | Only an explicit `execute_python` that reads and `exec`s one | source + live script contents |
| Can replacing a script change the next launch with no further action? | **Not through the bridge.** On the device: **UNKNOWN**, because a privileged `execute_python` may have installed a loader | see below |

## Where scripts live

- `Runtime.root` is `bpy.utils.user_resource('CONFIG', path='ghostbridge', create=True)`
  (`agent/runtime/__init__.py:201`). This is Blender's per-user config
  directory inside the iPad app sandbox. The absolute path on the device has
  not been observed.
- The workspace is `Runtime.root / 'scripts'`, created at runtime start
  (`agent/runtime/core.py:56-57`).
- The same root also holds `config.json`, which contains the **device pairing
  token** (`__init__.py:35-43`), plus `journal.json`, `outbox.json` and
  `capture.png` (`core.py:62, 67, 212`). Any script that is executed can read
  the pairing token, because execution is privileged.

## How they persist

- `write_script` compiles the code, checks the caller's `expected_sha256`
  against the current file (compare-and-swap), writes a temp file, fsyncs it
  and calls `os.replace` (`core.py:287-304`). A replace without the current
  hash fails with `script_changed; read current script before replacing`.
- Names are confined to `^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}\.py$`, with no
  `..`, no symlinks, and the parent must be the workspace (`core.py:22, 268-274`).
  `write_script` cannot write outside the workspace.
- The files survive app restarts and updates that keep the sandbox. Deleting
  the app removes them. **Live:** after the app restarted (boot `2f034d00…`,
  uptime 36 s at the time of `diagnostics`), `list_scripts` still returned
  `gb_cloth_bake.py`, `hero_retarget.py` and `ghostblender_insight.py`.

## What loads them

- **The bridge does not.** `Runtime.__init__` only creates the directory. The
  runtime lists, reads and writes scripts (`core.py:276-304`) but never imports
  or executes one. `execute_python` only exposes the directory path as
  `scripts_dir` in the execution namespace (`core.py:180-181`).
- **The bundled startup package is not the workspace.** The IPA transform
  copies `agent/runtime` to Blender's bundled `scripts/startup/ghostbridge`
  inside the signed app bundle (`scripts/apply-ios-agent-bridge.py:51, 58`).
  Blender imports that package and calls `register()` (`__init__.py:197-223`).
  Nothing in it reads the workspace.
- **Blender's own auto-load locations are different directories.** They are
  `scripts/startup` and enabled add-ons under the SCRIPTS resource roots, and
  text datablocks marked *Register* in a `.blend` when auto-run is allowed. The
  bridge workspace sits under the CONFIG root, which Blender does not scan for
  code. This is Blender's standard layout and has not been verified on the iPad
  build.
- **In practice an agent loads them explicitly** with `execute_python`, e.g.
  `exec(open(scripts_dir + '/hero_retarget.py').read())`. The live scripts
  follow this pattern: `hero_retarget.py` and `gb_cloth_bake.py` register a
  namespace in `bpy.app.driver_namespace` when executed.
  `ghostblender_insight.py` defines `register()` and only calls it when
  `__name__ == "__main__"`. The bridge's execution namespace uses
  `__name__ = '__ghostblender_agent__'`, so someone must have called
  `register()` explicitly.

## Can replacing a script affect the next launch?

**Through the bridge alone: no.** `write_script` changes a file that nothing in
this repository loads at startup.

**On the live device: UNKNOWN.** `execute_python` is privileged and could have
installed an auto-loader outside the workspace at any time, for example:

- a module in Blender's user `scripts/startup/` that execs a workspace script;
- an enabled add-on;
- a `load_post` handler or *Register* text block saved into a `.blend` or
  `startup.blend`.

`agent/HANDOFF.md` says `ghostblender_insight.py` is "registered" and "already
loaded" on the device, and REPO-STATE calls it a "persistent development
script". That is consistent both with an agent re-running it each session and
with such a loader existing. Which one is true decides whether replacing
`ghostblender_insight.py` changes the next launch.

### Probe that would settle it (read-only, needs approval)

A single `execute_python` that only reads:

```python
import bpy, sys, os
startup = [p for p in bpy.utils.script_paths(subdir="startup")]
result = {
    "startup_dirs": {p: sorted(os.listdir(p)) for p in startup if os.path.isdir(p)},
    "addons": sorted(bpy.context.preferences.addons.keys()),
    "insight_loaded": [m for m in sys.modules if "insight" in m],
    "insight_panel_registered": hasattr(bpy.types, "GHOSTBLENDER_PT_conversation"),
    "register_texts": [t.name for t in bpy.data.texts if t.use_module],
    "load_post": [getattr(h, "__module__", "?") + "." + getattr(h, "__name__", "?")
                  for h in bpy.app.handlers.load_post],
    "workspace": scripts_dir,
}
```

It writes nothing. The one side effect is the standard `view_layer.update()` and
redraw that `execute_python` always does.

## Consequences for policy

1. **Every `write_script` is a persistent-code risk.** The file outlives the
   session, the participant and the relay. A different participant may later
   execute it with full privileges, including access to the pairing token in
   the same root.
2. **The ledger records the known and unknown facts separately.**
   `loaded_by_bridge_at_startup: false` is known from source.
   `device_auto_load` stays `unknown` until the probe runs. It becomes `none`
   only if the probe shows no loader, and `known` if it shows one.
3. **The scene edit lease does not apply** to `write_script`. It changes no
   Blender scene state. The workspace gets its own serialization, and the
   device-side compare-and-swap on `expected_sha256` remains.
4. **`execute_python` remains the real persistence risk,** because it can write
   anywhere the app can. It is covered by the scene edit lease. The ledger
   records it as a mutation, not as persistent code, since the relay cannot see
   what the code writes.

## Probe result (2026-09-27, approved, read-only)

`execute_python` job `c867d5bbf9d54300b038a40d3a9ceceb`, after an app restart:

- Only startup dir: the signed app bundle's `scripts/startup` (`bl_app_templates_system`,
  `bl_operators`, `bl_ui`, `ghostbridge`, two builtin `.py` files). No user startup dir.
- Add-ons: only stock ones (`bl_pkg`, `cycles`, importers/exporters, `pose_library`).
- No `insight` module loaded; the Conversation panel is **not** registered.
- No text blocks marked Register; `load_post` handlers are Blender's and `ghostbridge._load_post`.
- Workspace: `…/Library/Application Support/Blender/5.2/config/ghostbridge/scripts`.

**Conclusion: nothing auto-loads the workspace.** `ghostblender_insight.py` was only ever
registered by an explicit `execute_python` in a session, and is gone after restart. Replacing
a workspace script cannot change the next launch by itself. The relay now records
`device_auto_load: "none"` (`store.py` `SCRIPT_DEVICE_AUTO_LOAD`).
