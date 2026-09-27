# Mapping today's shapes onto the GHOSTroom protocol

Every shape in `docs/ghostroom/CURRENT-SHAPES.md` is mapped here. The mapping
is **descriptive**: nothing in `agent/` emits GHOSTroom events yet. A future
adapter in the relay (or router) will perform it. **Gap** marks something
the current system cannot supply.

Common conversions:

- Epoch float seconds → RFC 3339 UTC with milliseconds
  (`1790475837.786` → `2026-09-27T02:23:57.786Z`).
- `job_id`, chat `message_id` (32 hex) already satisfy `common#/$defs/id`.
- The relay's single `device_id` (`ipad`) becomes part of `lane`/`origin`
  only where needed; GHOSTroom does not assume a single device.

## 1. Chat request message `{id, text}` (device → relay)

| legacy | GHOSTroom `event` |
| --- | --- |
| `id` | `id` and `correlation` |
| `text` | `body.text`, `body.role = "user"` |
| — | `kind = "discuss"`, `status = "completed"`, `parent = null` |
| — | `origin = {kind: user, id: owner}`, `lane = "user"` |
| relay `chat_messages.created` | `timestamp` |

Gaps: the device sends no timestamp, so the relay's receive time is the only
one available. Messages carry no addressee: every message goes to the one
Codex worker. There is no way to attach context (scene/selection/screenshot)
yet.

## 2. Chat event `{seq, message_id, type, text}` (relay → device)

All three types get `correlation = message_id`, `parent = message_id` (the
user message event), and `stream = {stream_id: reply.stream_id, seq}`. The
relay sends `stream_id` once PR #3 (cursor fix) is merged; older relays leave a gap.

| `type` | `kind` | `status` | `origin` | `body` |
| --- | --- | --- | --- | --- |
| `status` (`"AI working"`, `chat.py:237-242`) | `wait` | `started` | `{relay}` | `{on: "agent", label: text}` |
| `final` | `discuss` | `completed` | `{agent, id: codex, provider: codex}` | `{role: "assistant", text}` |
| `error` | `discuss` | `failed` or `uncertain` | `{relay}` | `{role: "system", text}` + `failure` (below) |

`error` has no code, only fixed texts. Map them by exact text:

| error text (source) | `status` | `failure.code` | `layer` | `mutation_possible` | `retry` |
| --- | --- | --- | --- | --- | --- |
| "AI sign-in required on the relay…" (`chat.py:256`) | failed | `auth_expired` | agent_provider | false | after_user_action |
| "AI turn failed. It may have changed Blender…" (`chat.py:258-261`) | failed | `interrupted_after_possible_mutation` | agent_provider | true | after_inspect |
| "AI backend error. Inspect the scene…" (`chat.py:269-273`) | failed | `unclassified` | agent_provider | true | after_inspect |
| "Previous AI turn was interrupted and was not replayed automatically." (`store.py:78`) | uncertain | `interrupted_after_possible_mutation` | relay | true | never |

Gaps:
- **No typed failure on the wire.** Codex quota, provider outage and
  sign-in expiry all turn into the same `RuntimeError` text unless the SDK
  message happens to be `codex_not_signed_in`. `quota_exhausted` and
  `provider_unavailable` cannot be produced today.
- **`status` is not phase-aware.** "AI working" cannot say whether the agent
  is discussing, inspecting or executing. GHOSTroom `inspect`/`execute`
  events for an agent's turn would need the worker to report its MCP tool
  calls (the Codex SDK streams these; `chat.py` discards them).
- **`stream_id` (fixed in PR #3).** CURRENT-SHAPES §7: with PR #3 the relay sends a
  per-database `stream_id` plus `reset`, and the bundled `insight.py` resets
  its cursor on either. The live shim still lacks it.
- No intermediate assistant text, no artifacts (captures the agent took stay
  inside Codex's thread), and no evidence references from a chat turn to the
  jobs it caused. The link has to be rebuilt from timing.

## 3. Bridge journal entry `{job_id, operation, state, started_at, ended_at, digest}`

Journal entries become **ledger entries** that reference the job. They are
not copied into events. Evidence object:

```json
{"type": "job", "job_id": "<job_id>", "operation": "<operation>", "state": "<state>",
 "device_digest": "<digest>", "started_at": "<rfc3339>", "ended_at": "<rfc3339>"}
```

| `operation` | ledger `category` |
| --- | --- |
| `inspect_scene`, `diagnostics`, `list_scripts`, `read_script` | `inspection` |
| `capture` | `evidence` (plus a `screenshot`/`render` artifact, §5) |
| `execute_python` | `mutation` (conservative: privileged Python may change anything) |
| `write_script` | `tool_call` (changes the script workspace, not the scene) |

| journal `state` | ledger |
| --- | --- |
| `running` | not yet a ledger entry; the live event is `execute`/`inspect` with `status: started` |
| `completed` | entry as above |
| `failed` | entry as above plus `failure` from the result error (§4); the `failure` category when the failure itself matters |
| `uncertain` (`error: app_restarted_during_command`) | `category: failure`, `failure.code: interrupted_after_possible_mutation`, `layer: blender`, `retry: never` |

Gaps:
- The journal entry holds **no `scene_id`, `boot_id`, originating agent or
  `request_id`**. A `mutation` ledger entry requires `scene_id`, so the
  adapter must join with the relay `jobs` row (which does have
  `scene_id`/`boot_id`) by `job_id`.
- Two different digests exist: the device digest (hash of the delivered job
  JSON, including `expires_at`) and the relay digest (hash of
  operation+scene+args). Neither is exposed together with the other. The
  relay `_public` view shows neither; `diagnostics` shows only the device
  digest. GHOSTroom evidence accepts either and should carry both once the
  relay exposes its digest.
- No result hash: evidence can point at a result but cannot yet verify it
  after the relay drops result payloads (1 day, `store.py:92`).
- The journal holds 256 entries on the device and relay jobs last 7 days, so
  the ledger must be written when events happen, not rebuilt later.
- `operation` can be null in the journal (`job.get('operation')`).

## 4. Relay job view and device result errors

`job_result` → job evidence (above) using `job_id`, `operation`, `state`,
`scene_id`. Relay job states not in the journal:

| relay `state` | meaning | GHOSTroom |
| --- | --- | --- |
| `queued` / `issued` | pending | `wait {on: blender}` / `execute|inspect status: started` |
| `expired` (from queued) | never delivered | failure `tool_timeout`, layer relay, mutation_possible false, retry safe |
| `uncertain` (issued past expiry) | may have run | failure `interrupted_after_possible_mutation`, layer bridge, retry after_inspect |
| `cancelled` | never delivered | `status: cancelled`, no failure |

Device/relay error strings → `failure` (`legacy_error` keeps the original):

| error | code | layer | mutation_possible | retry |
| --- | --- | --- | --- | --- |
| `scene_changed; call status and inspect_scene again` (relay) / `scene_or_session_changed; …` (device) | `scene_changed` | relay / bridge | false | after_inspect |
| `device_offline_or_suspended` / `app_not_foreground` | `blender_suspended` | bridge | false | after_user_action |
| `command_expired` | `tool_timeout` | bridge | false | safe |
| `Python time limit reached; partial edits may exist` | `tool_timeout` | tool | true | after_inspect |
| `render_in_progress; …` | `tool_error` | blender | false | after_inspect |
| other `ok:false` from `execute_python` | `tool_error` | tool | true | after_inspect |
| other `ok:false` from read-only ops | `tool_error` | tool | false | safe |
| `duplicate_command_not_reexecuted` | `tool_error` | bridge | per `record.state` | never |
| device `/device/exchange` 401/403 (`__init__.py:77-81`) | `auth_expired` | relay | false | after_user_action |
| device `Reconnecting (…)` (`__init__.py:106-114`) | `relay_unavailable` | relay | false | safe |

Gaps: no `device_queue_full` or `render_in_progress` code, so they map to
`tool_error`. If these need distinct UI they should become codes. Errors are
free text, so the adapter depends on string prefixes.

## 5. Capture result → artifact

`{mime_type, data, width, height, source}` becomes a `screenshot`
(`source: screenshot`, `capture_source: app_window`) or `render`
(`source: render_result`) artifact. `media = {media_type, sha256(decoded
bytes), bytes, width, height, uri}` and `evidence` = the capture job. The
base64 `data` goes to a media store, never into the event or ledger.

Gap: there is no media store yet, and the relay nulls results after 1 day.

## 6. Heartbeat / `status`

The bridge is a participant, not an agent. The heartbeat maps to origin
`{kind: ghostblender, id: bridge}` presence plus the `scene_id`/`boot_id`
used by object refs, leases and evidence. `online = false` with a recent
`last_seen` and `foreground = false` → `blender_suspended`; a stale
`last_seen` → `relay_unavailable` or device offline (the current data cannot
tell these apart).

## 7. Agent descriptor

The only agent today is the Codex worker (`chat.py`):
`{agent_id: codex, provider: codex, model: $CODEX_MODEL|unknown,
capabilities: [discuss, inspect, execute, capture, vision], roles:
[primary], auth: {method: device_code, state: signed_in|signed_out
(codex.account())}, quota: {state: unknown}}`.

Gaps: auth state is checked only when a message arrives, and is never
published. Quota is unobtainable today. There is no availability or
active-task state, and there is no router, so there is one agent per relay.
External MCP clients (ChatGPT, Claude via Simple) are invisible to the relay
as agents: every MCP call is anonymous at the relay.

## 8. Edit lease

**Gap: no lease exists.** The only serialization is one `issued` job per
device (`store.py:171-175`). This orders single jobs, but it does not stop two
agents interleaving multi-step edits. An external MCP client and the embedded
Codex worker can both mutate the scene today. The GHOSTroom lease must be
enforced where jobs are submitted (`Store.submit`) and checked against
`execute_python`/`write_script`.

## 9. Local UI message (`insight.py:34-40`)

`{id:int, role, source, text, created_at}` is a render cache for the Blender
sidebar, not a wire shape. It is replaced by rendering `discuss` events.
`source` (`local|bridge|model`) corresponds to `origin.kind`
(`user|relay|agent`).
