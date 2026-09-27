# GHOSTroom protocol groundwork — progress

A fresh session should be able to resume from this file alone.

## Context

- Target and repo-state docs: `target.md` and `REPO-STATE.md` at the **repo
  root** (the brief referred to `docs/ghostroom/TARGET.md` /
  `docs/ghostroom/REPO-STATE.md`; they were not moved).
- Working branch: `claude/ghostroom-protocol-groundwork-moht84`. The brief
  asked for `ghostroom/protocol`, but this session's push permissions are
  scoped to the branch above. Rename or re-push when you merge.
- Rules: do not move or refactor `agent/`; do not touch
  `build-unsigned-ipa.yml` or the IPA path; no secrets or capability URLs; the
  live bridge is read-only (`status`, `diagnostics`, `inspect_scene`,
  `list_scripts`, `read_script`) unless a mutation is approved.

## Done

- [x] **Step 0**: snapshotted `hero_retarget.py`, `gb_cloth_bake.py`,
      `ghostblender_insight.py` from the live device into
      `agent/device-scripts/` (sha256 verified, see its README).
- [x] **Step 1**: `docs/ghostroom/CURRENT-SHAPES.md` documents job, result,
      journal, heartbeat and chat shapes with file:line refs, confirmed against
      live `status` + `diagnostics`. Includes the cursor audit (§7).
- [x] **Step 2**: `ghostroom/protocol/` draft `ghostroom/0`: schemas for
      common defs, event envelope (7 kinds), artifacts (7 types), agent
      descriptor, edit lease, ledger entry, typed failures, plus legacy
      protocol-1 schemas. `MAPPING.md` maps every CURRENT-SHAPES shape and
      lists the gaps.

- [x] **Step 3**: 46 valid and 40 invalid fixtures (invalid = patches on a
      valid base, each with an expected error). `ghostroom/tests/test_protocol.py`
      runs 92 tests: schemas are valid 2020-12, every schema/kind/artifact type/
      required failure code is exercised, and no URLs appear in fixtures. Wired
      into `.github/workflows/agent-checks.yml` (protocol job). A
      `pull_request` trigger was added so PRs run it too.

## Next (awaiting approval; nothing started)

- Stopped after Step 3 as instructed. No UI or relay work yet.
- Candidate next steps, in the order I'd suggest:
  1. Fix the chat cursor skip (`stream_id` in the relay chat reply, device
     resets when it changes). Small; touches `agent/relay/store.py` and
     `agent/runtime/insight.py`, so it needs approval.
  2. Relay adapter that emits `ghostroom/0` events and ledger entries from
     the existing chat and job tables (read-only projection, no behavior
     change).
  3. Typed failure codes on relay chat errors (MAPPING §2 gap).
  4. Edit lease enforcement in `Store.submit` (MAPPING §8 gap).

## How to resume

```sh
python -m pip install 'jsonschema==4.25.1' 'pytest==8.4.2'
python -m pytest ghostroom/tests -q          # protocol
python -m unittest discover -s agent/tests   # existing bridge tests
```
Read `docs/ghostroom/CURRENT-SHAPES.md` (today), then
`ghostroom/protocol/README.md` and `MAPPING.md` (draft and gaps).

## Open questions

1. **Cursor skip (CURRENT-SHAPES §7)**: the relay can answer a device cursor
   that is ahead of its own `MAX(seq)` with a clamped cursor and no events,
   and the device keeps its higher cursor. After a relay database reset, all
   events up to the old cursor are skipped silently. Proposed fix: a
   `stream_id` per relay database, and the device resets its cursor when the
   id changes. Not applied, because relay/runtime changes need approval.
2. Could not confirm read-only whether the deployed relay currently returns
   the `chat` extension. That needs `execute_python` on the device, or relay
   logs.
3. Should the live `ghostblender_insight.py` shim be removed before the next
   IPA ships the bundled `insight.py`? Running both would conflict (§7).
