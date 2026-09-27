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

- [x] **Step 3**: 48 valid and 41 invalid fixtures (invalid = patches on a
      valid base, each with an expected error). `ghostroom/tests/test_protocol.py`
      runs 95 tests: schemas are valid 2020-12, every schema/kind/artifact type/
      required failure code is exercised, and no URLs appear in fixtures. Wired
      into `.github/workflows/agent-checks.yml` (protocol job). A
      `pull_request` trigger was added so PRs run it too.

## After Step 3: review round on PR #2 (2026-09-27)

PR layout. GHOSTpad stays one public repository; there is no separate service repo.

| PR | branch | contents |
| --- | --- | --- |
| #2 | `claude/ghostroom-protocol-groundwork-moht84` | protocol groundwork, secret-scan CI, script-workspace investigation doc |
| #3 | `claude/chat-cursor-reset-fix` (from `main`) | cursor fix only: `store.py`, `insight.py`, `test_chat_cursor.py` |
| #4 | `claude/relay-participants-ledger-leases` (stacked on #2) | B1.5 participant identity, B2 ledger, B3 leases, script-workspace lock |

- [x] **Cursor fix split into PR #3** and removed from this branch. The
      docs here describe it as "PR #3".
- [x] **Secret-scan CI** (`.github/workflows/secret-scan.yml`,
      `.github/scripts/scan_secrets.py`, policy in `REPOSITORY-POLICY.md`).
      Runs on every push/PR. One allowlisted public value (the Google IAP range).
- [x] **Script workspace investigation** (`docs/ghostroom/SCRIPT-WORKSPACE.md`).
      The bridge never auto-loads the workspace. Whether the live device has a
      loader installed by earlier privileged Python is UNKNOWN; the read-only
      probe needs approval.
- [x] **B1.5 participant identity**: `/mcp/p/<capability>` maps to a
      participant through a sha256 lookup. `clientInfo` is only an unverified
      label. The shared Simple capability, bearer and OAuth routes map to
      `legacy-unattributed`. The embedded worker is `codex-embedded`.
      `participants.py` is the CLI.
- [x] **B2 ledger**: the `ledger` table is append-only, written in the same
      transaction as the change, and read with `read_ledger`. Outcomes are
      never inferred.
- [x] **B3 edit leases**: explicit leases (`acquire_lease`/`release_lease`) or
      an implicit per-job scene lease, with typed `lease_conflict`/`lease_invalid`.
      Inspection is concurrent.
- [x] **Script workspace lock**: an implicit `script_workspace` lease per write
      (`script_workspace_busy`). Every write is a `persistent_code` ledger entry.
      Design: `docs/ghostroom/RELAY-IDENTITY-LEDGER-LEASES.md`.
- Tests: `agent/tests/test_participants_ledger_leases.py` (29 tests, stdlib) and
  `ghostroom/tests/test_relay_ledger_schema.py`, which checks real relay output
  against the schema.
- Deploy notes: redeploying the relay (it now contains `participants.py`) also
  reloads the Caddyfile with the new `/mcp/p/*` route. The existing connector
  URL keeps working. Leases are enforced as soon as the relay restarts.

## How to resume

```sh
python -m pip install 'jsonschema==4.25.1' 'pytest==8.4.2'
python -m pytest ghostroom/tests -q          # protocol
python -m unittest discover -s agent/tests   # existing bridge tests
```
Read `docs/ghostroom/CURRENT-SHAPES.md` (today), then
`ghostroom/protocol/README.md` and `MAPPING.md` (draft and gaps).

## Open questions

1. **Cursor skip**: fixed in PR #3 for the relay and the bundled `insight.py`.
   Open: should the live `ghostblender_insight.py` shim be patched too? That
   needs an approved `write_script`. Otherwise it stays affected until the
   next IPA.
2. Could not confirm read-only whether the deployed relay currently returns
   the `chat` extension. That needs `execute_python` on the device, or relay
   logs.
3. Should the live `ghostblender_insight.py` shim be removed before the next
   IPA ships the bundled `insight.py`? Running both would conflict (§7).

## GHOSTroom native workspace (PR #6, branch `claude/ghostroom-native-interface-wwocyj`)

Design and the full requirement-by-requirement status: [GHOSTROOM.md](GHOSTROOM.md).

- [x] Agent Router (`agent/relay/router.py`): Codex is `CodexAdapter` (with interrupt/steer
      and narration); Claude is an optional `ClaudeAdapter`; descriptors per `agent.schema.json`.
- [x] Room exchange (`agent/relay/room.py`): tasks, controls (stop), artifact upload/fetch,
      ledger-derived activity, workspace brief; new MCP tools `workspace_brief`,
      `read_artifact`, `post_note`.
- [x] Relay enforcement: `stopped_by_user`, `read_only_role` (added to `failure.schema.json`).
- [x] GhostBlender runtime (`agent/runtime/ghostroom.py`): presentation model, context
      attachments through read-only Runtime tools, crash-recovery cache, evidence store.
- [x] Native UIKit workspace (`agent/native/ghostroom_ui.mm`), compiled into `bf_python` by
      `scripts/apply-ios-agent-bridge.py`; `package-ipa.sh` verifies it is linked.
- [x] CI: iOS SDK compile of both native files; iPad-simulator harness driving the real
      native code with snapshots from the real relay/runtime.
- [ ] Device acceptance on the next IPA; relay redeploy with the new files.
