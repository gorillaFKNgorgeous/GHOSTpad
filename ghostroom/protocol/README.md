# GHOSTroom protocol (draft `ghostroom/0`)

Language-neutral JSON Schema (draft 2020-12) for the GHOSTroom collaboration
layer. Swift (native GHOSTroom UI) and Python (relay, router, Blender runtime)
both consume these files. Neither language's types are the source of truth.

Status: **draft**. Nothing in `agent/` emits or consumes these shapes yet.
`MAPPING.md` shows how every current wire and storage shape maps onto this
draft, and lists the gaps.

## Files

| schema | describes |
| --- | --- |
| `schemas/common.schema.json` | ids, RFC 3339 timestamps, origin, lane, stream cursor, job evidence refs, media refs |
| `schemas/event.schema.json` | event envelope + per-kind bodies: `discuss`, `inspect`, `plan`, `execute`, `wait`, `review`, `request_input` |
| `schemas/artifact.schema.json` | `screenshot`, `render`, `object_ref`, `checkpoint`, `task`, `review_card`, `lesson_step` |
| `schemas/agent.schema.json` | agent descriptor: provider, availability, auth state, capabilities, quota |
| `schemas/lease.schema.json` | edit lease: holder, acquired, expiry, scope |
| `schemas/ledger.schema.json` | shared workspace ledger entry |
| `schemas/failure.schema.json` | typed failure codes |
| `schemas/legacy/*.schema.json` | today's protocol-1 shapes (chat request/reply, journal entry, job view), for fixtures and mapping |

Schema `$id`s live under `https://schemas.ghostpad.invalid/ghostroom/v0/`.
The `.invalid` TLD is deliberate: nothing is fetched over the network.
Consumers load the directory into a local registry, and cross-file `$ref`s
are relative (`common.schema.json#/$defs/...`).

## Design rules encoded in the schemas

- **Events and artifacts, not strings.** Chat text is one event kind
  (`discuss`) among seven.
- **Origin, not ownership.** Every event and ledger entry names its
  `origin` (`user|agent|ghostblender|relay|system`). Lanes separate
  threads, and nothing needed to continue the work may live only in a lane.
- **Causality.** `parent` is required (null only for roots), plus an optional
  `correlation` for grouping one turn or task.
- **Cursor safety.** `stream = {stream_id, seq}`. A consumer holding a cursor
  for a different `stream_id` resets to 0. This fixes the silent skip in
  CURRENT-SHAPES §7.
- **Evidence by reference.** `job_evidence` points at a GhostBlender job by
  `job_id` + `device_digest` and/or `relay_digest`. Ledger entries of
  category `tool_call`/`mutation`/`inspection` must contain one. Media is a
  `media_ref` (hash, size, uri), never inline bytes.
- **Mutation needs a lease.** An `execute` event whose `mutation` is
  `possible` or `confirmed` must name its `lease_id`.
- **Failures are specific.** A `failed`/`uncertain` event must carry a
  typed `failure` with `layer`, `mutation_possible` and `retry`. `uncertain`
  implies `mutation_possible: true`, and `interrupted_after_possible_mutation`
  never allows a safe retry. `unclassified` exists only to map legacy
  free-text errors.
- **No Scene partial-write checkpoints.** `checkpoint.method` omits it (known
  crash on this build).
- **Teachability.** `lesson_step` has the five modes and the observable
  completion signals from the target doc (§18–19).

Rules JSON Schema cannot express, which consumers must enforce:
`lease.expires_at > acquired_at`; at most one `active` lease with overlapping
scope; `stream.seq` strictly increasing per `stream_id`; `parent` refers to an
earlier event; `supersedes` refers to an earlier entry.

## Validation

`ghostroom/tests/test_protocol.py` validates every file in
`fixtures/valid/<schema>/` (must pass) and `fixtures/invalid/<schema>/` (must
fail) with `jsonschema`. It runs in `.github/workflows/agent-checks.yml`.

```sh
python -m pip install 'jsonschema==4.25.1' 'pytest==8.4.2'
python -m pytest ghostroom/tests -q
```
