# Device scripts (snapshot from the live GHOSTpad)

These files were fetched **verbatim** from the persistent agent-script workspace
on the running iPad via the GhostBlender Simple MCP `read_script` tool on
2026-09-27. **The device copy was the source**; nothing here was edited.

| File | Bytes | sha256 reported by the device |
| --- | --- | --- |
| `hero_retarget.py` | 22103 | `9ca249850d7869b564ba311147e395ace73552cd34790363aca212cf00667571` |
| `gb_cloth_bake.py` | 11912 | `b9c16e6abd7cde9b82cf57f208bf2bf7edab393b801eafc7473529658f849204` |
| `ghostblender_insight.py` | 16336 | `aeacad8206b4c60e6a9b6e4872c7fdb4b03739b89b9f622a8f09698aaca3dee6` |

Local `sha256sum` of each committed file matches the device hash, so they are
byte-identical to what `read_script` returned.

Status of each:

- `ghostblender_insight.py` is the temporary development shim that bolts the
  Conversation / Workspace Insight panel onto the installed build (see
  `REPO-STATE.md`). Its cleaned, canonical descendant is
  `agent/runtime/insight.py`. It is kept here as evidence of the wire shape the
  live iPad actually speaks, **not** as a second source tree.
- `hero_retarget.py` and `gb_cloth_bake.py` are project/content scripts
  (Mixamo/Meshy retarget onto `GB_Rig`, cloth-sheet bake onto skirt bones) used
  in live sessions. They are not part of the bridge and are not bundled into
  the IPA or checked by CI.

To re-verify against the device: call `read_script` for each name and compare
the returned `sha256` with the table above. If they differ, the device copy has
moved on; re-snapshot rather than editing these by hand.
