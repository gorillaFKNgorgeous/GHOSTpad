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
| next | stacked on #2 | B1.5 participant identity, B2 ledger, B3 leases, script-workspace lock |

- [x] **Cursor fix split into PR #3** and removed from this branch. The
      docs here describe it as "PR #3".
- [ ] **Secret-scan CI**: capability URLs, bearer/access tokens, provider
      credentials, device/agent/OAuth secrets, public IPs, IP-embedding relay
      hostnames. Synthetic examples use reserved values.
- [ ] **Script workspace investigation** (`docs/ghostroom/SCRIPT-WORKSPACE.md`),
      written before any `write_script` policy.
- [ ] **B1.5 participant identity**: a capability maps server-side to a
      participant_id. `clientInfo` is an unverified label only. The existing
      shared Simple capability maps to `legacy-unattributed`.
- [ ] **B2 ledger** in the relay database, written in the same transaction as
      the state change. Never infers success.
- [ ] **B3 edit leases**, enforced from the start: explicit `lease_id` or an
      implicit per-job scene lease; `lease_conflict` otherwise.
- [ ] **Script workspace lock**, separate from the scene lease. Every
      `write_script` is ledgered as a persistent-code risk.

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
