# Current wire and storage shapes (ground truth, 2026-09-27)

This document records what **exists today**, not what GHOSTroom should become.
Every shape is cited to source. "Live" means it was also observed on the
running iPad through the GhostBlender Simple MCP bridge on 2026-09-27
(read-only calls only: `status`, `diagnostics`, `list_scripts`, `read_script`).

Source revision: branch base `dcde3a9` (main). Line numbers refer to that tree.

---

## 1. Relay job (SQLite `jobs` row)

`agent/relay/store.py:25-31`

| column | type | notes |
| --- | --- | --- |
| `job_id` | TEXT PK | `uuid4().hex` (32 hex), `store.py:132` |
| `device_id` | TEXT | single device, default `ipad` |
| `request_id` | TEXT | caller idempotency key, `^[a-zA-Z0-9_-]{8,80}$` (`store.py:13`); `UNIQUE(device_id, request_id)` |
| `digest` | TEXT | `sha256(operation + "\n" + scene_id + "\n" + canonical_args_json)` (`store.py:116`) |
| `operation` | TEXT | one of `OPERATIONS` (`store.py:11-12`): `inspect_scene, execute_python, capture, diagnostics, list_scripts, read_script, write_script` |
| `arguments` | TEXT | canonical JSON (sorted keys, no spaces), ≤110 000 bytes (`store.py:113-115`) |
| `boot_id`, `scene_id` | TEXT | copied from the device heartbeat at submit time (`store.py:134`) |
| `state` | TEXT | see state machine below |
| `created`, `expires` | REAL | epoch seconds; `expires = created + 90` (`store.py:135`) |
| `result` | TEXT | JSON of the device result (§3); nulled after 1 day (`store.py:92`) |

**Job state machine (relay)**

```
queued ──issue──▶ issued ──result ok──▶ completed
  │                 │     └─result !ok──▶ failed
  │                 └─expires passes──▶ uncertain ──late result──▶ completed|failed
  ├─expires passes──▶ expired            (store.py:87-89, 157-162)
  ├─boot/scene change on heartbeat──▶ expired   (store.py:167-168)
  └─cancel_job──▶ cancelled              (store.py:197)
```

Retention: rows deleted 7 days after `created` unless `queued|issued`
(`store.py:93`). One `issued` job per device at a time (`store.py:171-175`);
at most 8 `queued|issued` (`store.py:128-130`).

### 1a. Public job view (MCP `job_result`, `status` after submit)

`Store._public`, `store.py:182-185`:

```json
{"job_id": "…32hex…", "state": "queued|issued|completed|failed|expired|uncertain|cancelled",
 "operation": "read_script", "scene_id": "…32hex…", "created_at": 1790475836.69,
 "result": null | <device result §3>}
```

Live example (read_script of `hero_retarget.py`):
`{"job_id": "5a6a2612cd614283b715ee31b4d2f4d8", "state": "completed", "operation": "read_script", "scene_id": "e6072cdd…", "created_at": 1790475836.69, "result": {"ok": true, "value": {"name": …, "code": …, "sha256": …}, "scene_id": …}}`

MCP wrapping (`server.py:150-163`): the view is JSON-encoded into one text
content block; a PNG capture's `data` is lifted into a separate MCP `image`
block. `isError` is true for `failed|uncertain|expired`. Tool-level errors
(`ValueError`) become a plain text block, e.g. `device_offline_or_suspended`,
`scene_changed; call status and inspect_scene again`, `device_queue_full`,
`idempotency_key_reused_with_different_arguments`.

## 2. Job as delivered to the device (`/device/exchange` reply `.job`)

`store.py:176-178`

```json
{"job_id": "…", "operation": "…", "arguments": {…}, "boot_id": "…", "scene_id": "…", "expires_at": 1790475926.69}
```

Full exchange reply: `{"protocol": 1, "ack": <job_id|null>, "job": <above|null>, "chat"?: <§5 reply>}`
(`store.py:179`, `server.py:268-271`). `chat` is present **only** when the
relay has a chat worker (`app.chat`, set by `simple_server.py:62-67`) **and**
the request body carried a `chat` key.

## 3. Device result (outbox)

`agent/runtime/core.py:128-138`

```json
{"job_id": "…", "boot_id": "…",
 "result": {"ok": true,  "value": <operation-specific>, "scene_id": "…"}
        | {"ok": false, "error": "≤2000 chars", "traceback": "≤24000 chars"}
        | {"ok": false, "error": "duplicate_command_not_reexecuted", "record": <journal entry §4>}}
```

Persisted to `outbox.json` before sending (`core.py:137`) and cleared only by
the relay's `ack` (`core.py:97-100`). Device-side rejection reasons raised
inside `execute` (all become `ok:false`): `scene_or_session_changed; …`,
`command_expired`, `app_not_foreground`, `render_in_progress; …`
(`core.py:119-126`), plus per-operation errors (e.g.
`script_changed; read current script before replacing`, `core.py:295`;
`Python time limit reached; partial edits may exist`, `core.py:187`).

Operation `value` shapes: `inspect_scene` `core.py:165-172`; `execute_python`
`{stdout, result, elapsed_seconds}` `core.py:206-207`; `capture`
`{mime_type, data(base64), width, height, source}` `core.py:251-252`;
`diagnostics` heartbeat + `{uptime_seconds, build_hash, commands, logs}`
`core.py:264-266`; `list_scripts` `[{name, bytes}]` `core.py:277`;
`read_script` `{name, code, sha256}` `core.py:285`; `write_script`
`{name, sha256, executed:false}` `core.py:304`.

## 4. Bridge journal entry (device, `journal.json`)

`agent/runtime/core.py:115-116, 133-134, 65-66`

The journal is a JSON **object keyed by job_id**; the entry itself does **not**
contain `job_id`:

```json
{"<job_id>": {"state": "running|completed|failed|uncertain",
              "operation": "read_script",
              "started_at": 1790475837.786,
              "digest": "<sha256 of the whole delivered job JSON, sort_keys>",
              "ended_at": 1790475837.808,          // absent while running / uncertain
              "error": "app_restarted_during_command"  // only on uncertain
             }}
```

- `digest` here is **not** the relay digest: it is
  `sha256(json.dumps(job, sort_keys=True))` over the delivered job (§2)
  (`core.py:110`), so it covers `job_id`, `boot_id`, `scene_id`,
  `expires_at` and the arguments.
- Bounded to the most recent 256 entries (`core.py:73`).
- `running` at startup becomes `uncertain` (`core.py:64-66`).
- Exposed via `diagnostics.commands` as the last 20 `[job_id, entry]`
  **pairs** (`core.py:266`).

**Live confirmation** (diagnostics job `39b41962…`): `commands` is a list of
`[job_id, {state, operation, started_at, digest, ended_at}]` pairs, e.g.
`["5a6a2612cd614283b715ee31b4d2f4d8", {"state": "completed", "operation": "read_script", "started_at": 1790475837.786, "digest": "cfab15f2…8877", "ended_at": 1790475837.808}]`.
Live heartbeat: `bridge_version "0.1.0"`, `blender_version "5.2.0 LTS"`,
`python_version "3.13.13"`, `build_hash "2bc556e58e82"` (matches the pinned
Blender source).

So the canonical flattened journal record is
`{job_id, operation, state, started_at, ended_at, digest}` — `job_id` comes
from the key.

## 5. Heartbeat (device → relay)

`core.py:89-95`; request body `__init__.py:99-101`

```json
{"protocol": 1, "device_id": "ipad",
 "heartbeat": {"boot_id": "…", "scene_id": "…", "blender_version": "…", "python_version": "…",
               "scene_name": "…", "native": {"foreground": true, "available_memory_bytes": …,
               "physical_footprint_bytes": …}, "bridge_version": "0.1.0"},
 "completed": <outbox §3 | null>,
 "chat": {"cursor": <int>, "messages": [{"id": "<32 hex>", "text": "≤2000"}]}}
```

`status` tool = stored heartbeat + `{online, last_seen}` where
`online = seen < 12 s ago AND native.foreground` (`store.py:100-108`).
Live `status` matched this exactly (`online: true`, `foreground: true`).

## 6. Chat wire shape

### 6a. Device → relay (`chat` in exchange body)

Bundled: `agent/runtime/insight.py:59-64`. Live shim:
`agent/device-scripts/ghostblender_insight.py:107-108` (identical shape).

```json
{"cursor": <last seq applied>, "messages": [] | [{"id": "<uuid4 hex>", "text": "…"}]}
```

At most one pending message is sent per request (UI blocks a second send,
`insight.py:284-287`). Relay validation: `messages` ≤4, each exactly
`{id, text}`, `id` matches `^[a-f0-9]{32}$`, text 1–2000 after strip
(`store.py:203-225`).

### 6b. Relay → device (`chat` in exchange reply)

`store.py:241-256`

```json
{"cursor": <int>, "ack_ids": ["<id>", …],
 "events": [{"seq": <int>, "message_id": "<id>|null", "type": "status|final|error", "text": "…"}]}
```

Event `type` is enforced to `status|final|error` (`store.py:51`); text is
1–2000 chars. At most 50 events per reply, ascending `seq`.

### 6c. Chat storage

`store.py:33-45`

- `chat_messages(device_id, message_id, text, state, created, started, finished)`;
  `state` ∈ `queued → running → completed|failed`, `running → uncertain` on
  relay restart (`store.py:61-83`), `queued → failed` via `chat_fail`.
- `chat_events(seq AUTOINCREMENT, device_id, message_id, type, text, created)`;
  pruned after 7 days (`store.py:94`).
- `chat_state(device_id, thread_id, updated)` — the single Codex thread;
  deleted when a turn becomes uncertain (`store.py:80-83`).

Worker events (`chat.py:237-275`): `status "AI working"` on claim, then either
`final <reply>` or `error` with one of three fixed texts
(`codex_not_signed_in` → "AI sign-in required…", `RuntimeError` → "AI turn
failed. It may have changed Blender…", other → "AI backend error…").
The failure *kind* is only encoded in free text.

### 6d. Device consumption

Bundled `insight.py:67-108`; live shim `ghostblender_insight.py:67-91`.
For each event with `seq > _CHAT_CURSOR`: set `_CHAT_CURSOR = seq`; `status`
→ status line, `final` → assistant message (source `model`), `error` → system
message (source `bridge`); unknown types are consumed silently. Then
`_CHAT_CURSOR = max(_CHAT_CURSOR, reply.cursor)`. The cursor and history are
in memory only (reset to 0 on app restart).

Local UI message (not on the wire), `insight.py:34-40`:
`{id:int, role:user|assistant|system, source:local|bridge|model, text, created_at}`.

## 7. Cursor audit (requested check)

**Question:** can `chat_exchange` return a cursor beyond the last event it
delivered, so `insight.py` silently skips events?

**Normal operation: no.** `next_cursor = rows[-1].seq` when events are
returned, else the (clamped) request cursor (`store.py:255`). Pagination at 50
advances exactly to the last delivered seq. Reproduced locally: 60 events →
page 1 returns 50 events and `cursor 50`, page 2 returns 10 and `cursor 60`.

**But yes, when the device is ahead of the relay.** `store.py:210-214` clamps
the request cursor to `MAX(seq)` of the *current* table and, when no newer
rows exist, returns that clamped value as the new cursor without delivering
anything. The device then applies `max(local, reply.cursor)` and keeps its
higher value. Either way, events whose `seq` is ≤ the device's stale cursor
are never shown. This happens whenever `seq` restarts below the device
cursor while the app stays running:

- the relay SQLite volume is lost, recreated, or restored from an older
  backup (AUTOINCREMENT restarts);
- the device is repaired against a different relay/deployment.

Reproduced locally: fresh store with one `final` event (seq 1), device sends
`cursor 60` → reply `{"cursor": 1, "events": []}`; the device keeps 60, so the
reply at seq 1 and every later event up to seq 60 are skipped. There is no
relay epoch/instance id in the reply that would let the device detect this.
Pruning after 7 days is **not** a trigger, because AUTOINCREMENT never reuses
seq values.

Related gaps (not skips):
- A device restart resets the cursor to 0 and replays up to 7 days of events
  (50 per heartbeat) as if they were new messages.
- If a future IPA bundles `insight.py` **and** the live shim
  `ghostblender_insight.py` is still loaded, the shim's `_TransportProxy`
  replaces the bundled `chat` payload (`ghostblender_insight.py:107`) and both
  modules consume the same reply with independent cursors. The shim should be
  removed when the bundled companion ships.

Proposed fix (for Step 2+, not applied): the relay adds a stable
`stream_id` (random per database, stored once) to every chat reply; the device
resets its cursor to 0 when `stream_id` changes or when `reply.cursor <
request.cursor`. The GHOSTroom protocol carries this as `stream_id` on the
cursor contract.

## 8. Live observations

- `status`: online, foreground, `scene_id e6072cdd…`, `boot_id 2f034d00…`,
  bridge `0.1.0`, Blender `5.2.0 LTS`.
- Persistent scripts on device: `gb_cloth_bake.py` (11912 B),
  `hero_retarget.py` (22103 B), `ghostblender_insight.py` (16336 B). All three
  are snapshotted in `agent/device-scripts/` with matching sha256.
- The live shim speaks the same chat wire shape as the bundled `insight.py`
  (§6). It proxies `ghostbridge.native.request/poll`; the bundled version is
  called directly from `_tick` (`__init__.py:87, 101`).
- Whether the deployed relay currently returns a `chat` extension could not be
  observed read-only (it is visible only inside the device exchange, not
  through MCP). Checking it would require `execute_python` on the device,
  which has not been approved.
