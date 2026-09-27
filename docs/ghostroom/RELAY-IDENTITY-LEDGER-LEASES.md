# Relay: participant identity, shared ledger and edit leases

Implements review items B1.5, B2 and B3 in `agent/relay/`. The protocol shapes
are in `ghostroom/protocol/schemas/` (`ledger`, `lease`, `failure`).
`ghostroom/tests/test_relay_ledger_schema.py` validates the relay's real ledger
output against them.

## B1.5 Participant identity

**Identity comes from the capability the caller presents, never from MCP
`clientInfo`.**

| route | participant |
| --- | --- |
| `/mcp/p/<capability>` | the participant that capability belongs to |
| `/mcp` (Caddy's existing shared capability, the agent bearer token or OAuth) | `legacy-unattributed` |
| relay-internal embedded Codex worker | `codex-embedded`, via its own `/mcp/p/` capability |

- The `participants` table maps `sha256(capability)` to `participant_id`,
  `kind`, `provider` and `display_name`. The capability itself is never stored.
  A lost capability can only be rotated.
- An unknown or revoked capability gets `404`, the same as any missing path.
- `clientInfo.name`/`version` from `initialize` is stored as
  `client_label` and shown as *unverified*. It never selects, grants or changes
  identity, authorization or ownership. The test
  `test_identity_comes_from_the_capability_not_client_info` sends a client
  claiming to be `legacy-unattributed` over Claude's capability and checks the
  job is still attributed to `claude`.
- **The existing shared capability is untouched.** Caddy's
  `/mcp/{$AGENT_TOKEN}` rewrite still reaches `/mcp` and maps to
  `legacy-unattributed`. Participant capabilities use a separate Caddy route,
  `/mcp/p/*`, that is passed through without rewriting.
- `codex-embedded` gets a fresh capability at every relay start
  (`simple_server.py`). Only its digest is stored. The URL is written into the
  worker's own Codex config under `/data`, which is created with the relay's
  `0077` umask.
- The job idempotency key (`request_id`) now belongs to its participant: reusing
  another participant's key fails. Only a job's submitter can cancel it.

Operator steps (on the relay VM):

```sh
cd ~/ghostblender/agent/relay
sudo docker-compose exec relay python participants.py add claude --kind agent --provider claude --name "Claude"
# prints once:  MCP path (shown once): /mcp/p/<capability>
# connector URL = <PUBLIC_ORIGIN> + that path. Never paste it into GitHub or chat.
sudo docker-compose exec relay python participants.py list     # shows unverified client labels
sudo docker-compose exec relay python participants.py rotate claude
sudo docker-compose exec relay python participants.py revoke claude
```

## B2 Shared workspace ledger

- **Authoritative and in the relay database.** The `ledger` table is
  append-only and never pruned. Job results are deleted after 1 day and job
  rows after 7 days, but ledger entries reference jobs only by id, digests and
  `result_sha256`, never by payload.
- **Same transaction as the state change.** Every write that changes a job,
  lease or rejection appends its entry inside the same SQLite transaction
  (`Store.submit`, `exchange`, `_expire`, `cancel`, `acquire_lease`,
  `release_lease`). If the ledger write fails, the state change rolls back:
  `test_ledger_write_and_state_change_commit_together` checks this for both
  submission and result recording. Nothing is reconstructed later from
  timestamps.
- **Agent-neutral.** `origin` identifies who acted, and `lane` is their
  participant id. The relay owns every entry. `read_ledger` returns every
  participant's entries in order, with the same `stream_id`/`reset` cursor
  contract as chat.

### Outcomes of jobs that may mutate (`execute_python`, `write_script`)

| what happened | ledger `outcome` | failure |
| --- | --- | --- |
| accepted by the relay | `requested` | – |
| delivered to the device | `dispatched` (this is **not** success) | – |
| device reported `ok: true` for this job_id + boot_id | `completed` | – |
| device reported an error raised before running (scene changed, expired, app backgrounded, render running) | `failed` | typed; `mutation_possible: false` |
| device reported a Python error or time limit | `failed` | `tool_error`/`tool_timeout`; `mutation_possible: true`, `retry: after_inspect` |
| expired in the relay queue, or dropped because the scene changed before delivery | `expired` | `mutation_possible: false` |
| delivered, then no result before expiry | `uncertain` | **`interrupted_after_possible_mutation`** |
| device says it already ran the job but the first result was lost | `uncertain` | **`interrupted_after_possible_mutation`** |
| a result arrives after `uncertain` | `completed`/`failed` with `supersedes` = the uncertain entry | as above |
| cancelled while queued | `cancelled` | – |

Success is never inferred from dispatch, issuance, a timeout, connection loss
or the absence of an error. The schema enforces the pairing: `completed` cannot
carry a failure, and an uncertain mutation must be
`interrupted_after_possible_mutation`.

## B3 Edit leases

Enforced from the first deploy. There is no recording-only phase.

- **Scope:** the Blender scene (`scope_kind: scene`, bound to a `scene_id`).
  Only `execute_python` needs it. Inspection, capture, diagnostics and script
  reads run concurrently at all times.
- **Explicit lease:** `acquire_lease(scene_id, duration_seconds 10–600)`
  creates the lease, or renews it for the same holder. `release_lease(lease_id)`
  ends it. Passing `lease_id` to `execute_python` uses that lease. The lease
  must be the caller's own, explicit, active and for the same scene, otherwise
  the call fails with `lease_invalid`. A holder that omits `lease_id` is still
  covered by its own explicit lease.
- **Implicit lease for callers without `lease_id` (all legacy callers):** taken
  only when no other participant holds the scene. It identifies the caller
  (including `legacy-unattributed`), covers exactly one job
  (`lease_id = implicit-<job_id>`) and expires with that job's 90-second expiry
  at the latest. It ends as soon as the job reaches a final state. It is
  ledgered at acquisition and at its end, so it never becomes a long-running
  session.
- **Conflict:** if another participant holds the scene lease, explicit or
  implicit, the mutation is rejected before anything is queued. The rejection
  is a typed `lease_conflict` failure naming the holder and expiry, and it is
  ledgered with `lease.event: rejected`.
- **Scene changes** end explicit leases for the old scene. Expired leases are
  ledgered as `expired`.
- `status` lists the active leases, so GHOSTpad always knows who holds
  authority to mutate.

Limits, stated plainly:
- Every legacy caller is the same participant. Two different clients on the
  shared capability are not protected from each other. Give each one its own
  participant capability.
- The iPad executes one job at a time on Blender's main thread. If an implicit
  lease ends because its job became `uncertain` while the device was still
  running it, a later job still waits for the first to finish on the device.
- Only whole-scene scope is enforced. `objects`/`collection` scopes cannot be
  enforced for arbitrary Python and are not offered.

## Persistent script workspace (`write_script`)

See `SCRIPT-WORKSPACE.md` for the investigation this policy rests on.

- `write_script` **does not** use the scene lease. It takes its own implicit
  `script_workspace` lease for the one job. While any workspace write is pending,
  every other write, from any participant, fails with `script_workspace_busy`.
  The device-side compare-and-swap on `expected_sha256` remains, and the relay
  now rejects a malformed `expected_sha256`.
- Every `write_script` ledger entry has category `persistent_code` and a
  `persistent_code` record with:
  - `risk: true`;
  - the participant (from `origin`);
  - `script`;
  - `replaces_existing`;
  - `previous_sha256` when replacing;
  - `resulting_sha256`, with `confirmed_by_device` once the device reports it;
  - `loaded_by_bridge_at_startup: false`;
  - `device_auto_load` (`unknown` until the device probe runs);
  - `request_id` and `job_id`.

  The code itself is never copied into the ledger.
