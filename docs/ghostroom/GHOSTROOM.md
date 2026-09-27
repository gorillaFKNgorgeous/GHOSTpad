# GHOSTroom — native AI workspace: design and target status

GHOSTroom is the native iPadOS workspace inside GHOSTpad. You and whichever agents
you choose work in the same live Blender project there. This document records how it
is built and, for every numbered requirement in `target.md`, what is implemented, what
exists as extensible architecture rather than UI today, and what is externally blocked.

## Architecture

```
 iPadOS (native shell)            Blender main thread (GhostBlender)         Relay / Agent Router
 ─────────────────────            ──────────────────────────────────         ────────────────────
 ghostroom_ui.mm                  ghostroom.py            core.Runtime       room.py ── router.py ── adapters
  pill · panel · composer   JSON   presentation model     inspect/capture      │           │        chat.py (Codex)
  pickers · timeline  ◀────────▶  context attachments ─▶ execute (jobs) ◀────┤ store.py   │        claude_agent.py
  evidence viewer   snapshot/cmds local recovery cache                        │ tasks · room events · artifacts
                                  device exchange `room` ───────────────────▶ │ Shared Workspace Ledger · leases
                                                                              └ MCP tools ◀── every agent, incl.
                                                                                               GhostBlender Simple
```

- **There is one path for scene manipulation:** agent → GhostBlender MCP tools →
  relay job → `core.Runtime.execute` on Blender's main thread. GHOSTroom, Codex,
  Claude, and ChatGPT through GhostBlender Simple are all clients of that path. The
  native layer and `ghostroom.py` never change the scene. Context attachments use
  the Runtime's read-only `inspect_scene` and `capture`.
- **The native shell presents; it does not decide.** It renders the snapshot built
  by `ghostroom.py` and sends back small JSON commands (`send`, `stop`, `attach`,
  `select`, `recovery`, `forward_note`, `fetch`, …). All presentation logic is
  in Python and is tested on Linux (`agent/tests/test_ghostroom_runtime.py`).
- **The relay is the authority.** Tasks, room events, artifacts and the ledger live
  in its SQLite store. The device keeps a cache for crash recovery and offline display.
- **Activity comes from the ledger,** not from extra model output. Every job an agent
  runs is already a ledger entry. `room.activity_label` names it from the step
  comment agents are asked to write (`# Shape the wheel arches`), or from what the
  code visibly does, and consecutive jobs with the same label merge into one phase.
  A long task therefore reads as a handful of evolving phases, while the agent stays
  free to run hundreds of operations. Codex commentary it produces anyway is shown
  as narration.

## Requirement status against target.md

Status: **Done** = implemented and tested. **Architecture** = the data structures and
protocol exist and are exercised, and the UI surface is deliberately thin or deferred.
**Blocked** = needs something outside this repository.

| # | Requirement | Status | Where / evidence |
| --- | --- | --- | --- |
| 1 | Preserve full Blender | Done | GHOSTroom is an overlay in Blender's own window, next to the Metal view rather than inside it. Touches outside it reach Blender unchanged, and it minimises to a pill. No Blender functionality is removed. |
| 2 | GhostBlender Simple stays independent | Done | Simple's endpoint, tools and behaviour are unchanged; three read-only/structured tools were added (`workspace_brief`, `read_artifact`, `post_note`). Simple is not needed by GHOSTroom, and GHOSTroom is not needed by Simple. External-agent activity appears in GHOSTroom as "External agent (GhostBlender Simple)" cards. |
| 3 | Native GHOSTroom experience | Done | `ghostroom_ui.mm`: large multiline composer (⌘↩ send, Return newline, Scribble, keyboard-following layout), selectable/copyable text, context-menu Copy/Quote, native scrolling, 44pt+ controls, pointer effects, attachments, agent/mode/role pickers, task/progress presentation, minimise to pill, dock left/right, resizable width. The N-panel chat (`insight.py`) remains only as a fallback transport. |
| 4 | Part of Blender, not a separate app | Done | Subviews of Blender's `UIWindow`, never a separate window or scene; Blender stays visible beside the panel. ⌘⇧G and the N-panel button open it. |
| 5 | One Blender-control layer | Done | No native-side manipulation. Attachments use `Runtime.inspect_scene`/`capture`. The Claude adapter calls the same MCP endpoint, under its own participant capability. |
| 6 | Agent Router | Done | `router.py`: `AgentAdapter` interface; per-agent workers; availability, auth, quota, model, capabilities, roles, active task and activity published as `agent.schema.json` descriptors (schema-tested). Specific failures (`quota_exhausted`, `auth_expired`, `provider_unavailable`) are shown before and after a turn. Codex is `CodexAdapter`; Claude is `ClaudeAdapter`. Quota is reported when the provider tells us (errors/429); proactive quota polling is **Architecture** (the field exists; neither SDK exposes a cheap read today). |
| 7 | Independent lanes without silos | Done | Each adapter keeps its own thread/lane. Before every turn, the router adds a **workspace brief** built from the ledger (other participants' instructions, replies, phases, failures, notes since that lane last looked). Switching agents does not restart the project. |
| 8 | Shared Workspace Ledger | Done | New entries: `instruction`, `response`, `task`, notes (`decision`, `review`, `handoff`, `question`, `summary`, `warning`), and captures with `artifacts`, all schema-validated (`ghostroom/tests/test_relay_ledger_schema.py`). Existing tool-call/mutation/lease/failure entries are unchanged. Files opened and saved are recorded as `file` entries from GhostBlender's heartbeat observation. `checkpoint` exists in the schema, but there is no checkpoint tool yet (**Architecture**; Scene partial writes are a known crash). |
| 9 | Project recovery | Done | Relay: interrupted turns become `uncertain` with `interrupted_after_possible_mutation` and are never replayed. Device: `ghostroom.py` notices that the previous session ended mid-task, reads the Runtime journal for an operation that was running during a crash, and shows a recovery card (last activity, evidence, possible partial change) with **Continue** and **What happened?**. Answering questions like "what did Claude change to this rig" works through the brief and ledger; a dedicated history-search UI is **Architecture**. |
| 10 | Multi-agent roles | Done (core) | Primary/Reviewer/Specialist/Critic/Verifier are selectable per message. Reviewer, Critic and Verifier are enforced read-only at the relay. Agents leave structured notes with `post_note`, can address them with `to`, and the user can **Hand to** that agent in one tap. Agents do not dispatch work to each other automatically; the user stays in the loop by design. |
| 11 | No scene collisions | Done | The existing lease system is unchanged. Each adapter is its own participant, so mutations serialize while inspection stays concurrent. Explicit lease events appear in the timeline ("Claude took editing control"). |
| 12 | Visible collaboration | Done | Agent picker subtitles show each agent's activity. Task cards show the agent, state and phases. The pill shows the active agent and phase while GHOSTroom is minimised. External participants get their own cards. |
| 13 | Evidence as part of work | Done | Captures become relay artifacts linked from the ledger entry of the job that produced them. The device keeps copies (`evidence/`) and fetches missing ones. Thumbnails sit on the task card and open in a zoomable, shareable viewer. Agents are told captures are shown as evidence, and `read_artifact` lets any agent re-examine the same basis. |
| 14 | Explicit context controls | Done | Current scene, selected objects, viewport, render result, image/reference (PHPicker) and file (document picker: text content, or name/size). Images are uploaded as artifacts before the message is sent and delivered to vision-capable agents. |
| 15 | Discuss / inspect / plan / execute / wait / review / input | Done | `activity_state` gives *inspecting*, *executing*, *reviewing*, *planning*, *thinking*, *waiting for Blender*, *needs input*, *queued*, *stopping*, *stopped*, *failed*. These are derived from real events, so agents are never forced through states. |
| 16 | Long-running work | Done | Elapsed time, per-phase counts and spinners. Task state survives app backgrounding, relay restarts (tasks marked uncertain, never replayed) and app crashes (local cache and recovery card). Unsent instructions are saved before sending. |
| 17 | Specific failures | Done | Typed failure cards show code, layer and whether Blender may have changed. An agent failure is not a bridge failure: the layer is shown, and "switch agent" is offered through the picker. New codes `stopped_by_user` and `read_only_role` were added to `failure.schema.json`. |
| 18 | Teachability | Architecture + first UI | Modes *Do it for me*, *Do it with me*, *Teach me*, *Explain this*, *Review my work* are selectable and travel with each task. Mode instructions are provider-neutral, and teach/explain/review are enforced read-only. A step-by-step lesson UI (lesson_step artifacts) is future work; the protocol already defines it. |
| 19 | Observe learner actions | Architecture | The heartbeat reports `observed` (mode, active, selection, frame, unsaved, rendering). Agents read it through `status`, so a teaching agent can check what the learner did. Event-level change detection (modifier edits, per-step completion) is future work. |
| 20 | Adaptive learning | Architecture | Builds on 18/19. Not a product surface yet. |
| 21 | Richer than chat bubbles | Done (core) | The timeline renders tasks with phases, evidence strips, notes/decisions with rationale, typed failures, lease/system lines, external activity, recovery and pending messages. The protocol already carries artifacts/events; lesson steps, before/after comparisons and interactive choices are **Architecture**. |
| 22 | GHOSTpad visual language | Done (v1) | Graphite glass with ghost-mint signal, spectral violet for collaboration, amber caution, coral failure, rounded heavy wordmark. It does not imitate Higgsfield, stock Blender or web chat. Design review on device is still needed. |
| 23 | Clean responsibilities | Done | See the architecture section. The native layer, GhostBlender, Blender and the relay each own what the target assigns them. |
| 24 | No vendor structurally essential | Done | Codex and Claude are adapters chosen by `GHOSTROOM_AGENTS`. Either, both or neither can run; the room, ledger and GHOSTroom work with external agents only. |
| 25 | Ultimate experience | See below | |

### Acceptance walkthrough (§25)

| Step | How |
| --- | --- |
| Open GHOSTpad; work in full Blender | GHOSTroom is minimised to a pill; Blender is untouched. |
| Open GHOSTroom without leaving Blender | Tap the pill, press ⌘⇧G, or use the N-panel button; the panel docks beside the viewport. |
| Talk naturally to a selected agent | Composer plus agent picker, with availability shown up front. |
| Let it inspect and work on the same live project | The agent uses GhostBlender tools on the live scene. Context attachments share what you are looking at. |
| Understand what it is doing | Activity bar, pill and task-card phases from real tool calls, narration and evidence thumbnails. |
| Intervene at any time | Redirect by sending a message (steered into the running Codex turn, queued for others), or Stop (enforced at the relay). |
| See useful evidence | Captures and renders appear on the task and open full screen. |
| Recover after interruption | Recovery card from the ledger and device journal, with Continue and What happened?. |
| Another agent joins or takes over | Pick another agent. It gets the workspace brief; notes can be handed over in one tap. |

## Validation

- `python3 -m unittest discover -s agent/tests`: relay, router, room exchange, stop/steer/read-only enforcement, artifacts, Claude adapter, runtime presentation model, recovery.
- `python3 -m pytest ghostroom/tests -q`: protocol schemas plus the relay's real ledger output, including GHOSTroom entries.
- `agent-checks.yml / native-syntax`: both native files compile against the iOS 26 SDK with `-Wall`.
- `agent-checks.yml / ghostroom-simulator`: the real `ghostroom_ui.mm` runs in an iPad simulator, fed snapshots produced by the real relay and runtime (`agent/native/harness/make_snapshots.py`). It is driven like a user (open, send multiline, stop, dock, keyboard, 160-item timeline, minimise), the commands it returns are checked, Auto Layout conflicts are counted, and screenshots are uploaded.
- Device acceptance still requires the next IPA on the physical iPad, and a relay redeploy (new files: `router.py`, `room.py`, `claude_agent.py`).

## Update: device feedback round 1

- **External agents are full participants.** Agents using GhostBlender Simple or a personal connector call `room_read`, `room_start` and `room_post`. Their jobs automatically form a session task that the user can watch, stop and recover. The user can message them from the picker, and they read it on their next `room_read`.
- **Agents & connections in GHOSTroom.** Codex device-code sign-in and sign-out, Claude API key, and connector URLs are all managed from the agent menu → *Agents & connections…*, with no relay redeploy. Credentials live only in `/data/agent-secrets.json` (0600) on the relay, and only in memory on the device.
- **Own window.** GHOSTroom now runs in its own passthrough `UIWindow` above Blender's. Blender's `GHOSTUIWindow` consumes hardware key presses and has window-level gestures, which blocked typing and the attach/agent menus.
