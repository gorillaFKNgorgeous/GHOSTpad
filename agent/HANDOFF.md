# GhostBlender live MCP — resume here

Updated 2026-09-25. **The bridge source is fully tracked in this repository and end-to-end remote Blender control on the physical iPad is working in development through the private single-owner GhostBlender Simple deployment.** The cloud deployment is the running relay host and private configuration, not the canonical source location. Do not restart this work or restore the old MCP prototype.

## Embedded AI chat checkpoint — 25 September 2026

The Higgsfield-inspired companion work from the live iPad session has now been recovered into source rather than left as an ephemeral agent script. The current physical iPad already has the prototype Conversation / Workspace Insight panel registered as `ghostblender_insight.py`; its visible blocker was correctly identified as **server-side chat support**, not Blender UI registration. The current relay returns no `chat` extension, so that prototype displays “Chat service unavailable on relay”.

The repository now implements the missing last-mile architecture:

- `agent/runtime/insight.py` is the bundled Conversation + Workspace Insight UI. In future builds it rides the existing GhostBlender heartbeat directly, so there is still exactly one native network poller.
- The existing live prototype remains wire-compatible, so **the first end-to-end chat acceptance does not require another IPA**. Redeploying the relay is enough to test the Conversation panel already visible on the iPad.
- `agent/relay/store.py` now journals chat messages and cursor-based events in SQLite. Message IDs are idempotent. A relay restart never replays a model turn that had started: it becomes `uncertain`, emits an error event and drops the possibly-partial Codex thread state.
- `agent/relay/chat.py` adds one serial Codex worker using the user's existing Codex/ChatGPT account session. It reuses one Codex thread for continuity, uses the relay's own MCP endpoint at localhost for Blender tools, runs read-only / deny-escalation locally, and explicitly approves this owner's GhostBlender MCP tools.
- `agent/relay/chat_login.py` provides the one-time device-code ChatGPT account sign-in. No OpenAI API key is required for this path.
- The relay image pins `openai-codex==0.156.1`. The worker defaults to medium reasoning; `CODEX_MODEL` and `CODEX_REASONING_EFFORT` can be overridden privately in the relay environment later.
- `agent/relay/deploy-gce.sh` now deploys from canonical `GHOSTpad` even when the VM checkout previously pointed at the old repository.
- New relay tests cover chat idempotency, cursor delivery, interrupted-turn non-replay, thread reset and device-exchange transport. CI also syntax-checks the new chat/runtime modules.

**Immediate remaining acceptance steps:** (1) redeploy the relay from current `main`; (2) run `sudo docker-compose exec relay python chat_login.py` once and complete the displayed device-code sign-in; (3) in the already-open iPad Conversation panel send a harmless request such as “inspect the scene and tell me the active object”; (4) verify the model uses GhostBlender MCP, the reply returns to the panel, and a second instruction can mutate then inspect the scene without duplicate execution. Only after that acceptance should a new IPA be built to prove the companion is permanently bundled rather than relying on the current persistent prototype script.

## Goal and project constraints

Direct, persistent, authenticated access from ChatGPT/OpenAI to running iPad
Blender 5.2: scene inspection, Python execution, actual images, diagnostics,
script creation/modification and autonomous iteration. The user supplies goals,
not Blender operator names. One-time signed-app installation and pairing remain
necessary; the intended steady state has no manual code/log transfer.

- Repo: https://github.com/gorillaFKNgorgeous/GHOSTpad
- Work on main only. No new branches or PRs. The obsolete MCP prototype is rejected.
- Initial main: bb977ea0a16d0f09942ca04b5b59270042c46461.
- Initial durable checklist: abca12cb5e2cb5342b401d71bc09e686bd9434b5.
- Initial implementation: 90e9416f7c5a0cf44443fe2eb3d33aafc866c770.
- Final integration commit: 1b70fac63ff2c45a3878f3adecfba2b806c6cecf.
- Framework-resolution fix / successful build 86 commit: 4a2c008d424b13972b060ce7f6dbd430234fe33f.
- Installed physical-device bridge proof is build 86; build 81 remains the prior
  owner-confirmed baseline for normal save/file behavior.
- Blender source pin: 2bc556e58e82eb3a801895f2cb1881c0267e5cd5.
- CPython 3.13.13, Xcode 26.3, iPad Pro M4 16 GB; existing Signulous signing route.
- Scene partial library-write crash is unresolved. Never use Scene partial writes
  for bridge checkpoints. The source already copies PartialWriteContext colorspace;
  the prior claim it was missing was wrong. Do not reintroduce that backport.
- The Apple/Siri attachment is background, not verified implementation authority.
  No second Python interpreter, PyEval_InitThreads, speculative FoundationModels
  APIs or assumed unlimited background execution have been introduced.

## Where the bridge lives

The bridge is source-controlled in this repository. Cloud Console / GCE is only a deployment target for the relay process and its private runtime configuration. Rebuilding or moving the relay should start from the files in `agent/relay/`; do not treat the live VM/container as the source of truth.

The bridge is split into three layers:

- **iPad native transport:** `agent/native/ghostbridge_transport.mm`, compiled into the Blender/iPad build.
- **Blender runtime:** `agent/runtime/`, loaded inside Blender and responsible for scene inspection, Python execution, captures, diagnostics, job state and the Agent Connection UI.
- **Remote relay / MCP service:** `agent/relay/`, containing the HTTP/MCP server, OAuth, durable job store, Docker files and GCE deployment helper.

Build integration is also in-repo through `scripts/apply-ios-agent-bridge.py`, `scripts/prepare-source.sh`, `scripts/package-ipa.sh` and `.github/workflows/agent-checks.yml` / the main IPA workflow. Secrets and generated `.env` files are intentionally not committed.

## Implemented files

- `agent/native/ghostbridge_transport.mm`: asynchronous NSURLSession HTTPS,
  bounded response buffers, redirect rejection, no Python callbacks/background
  Python threads, app foreground and measured process memory status.
- `agent/runtime/__init__.py`: startup registration, persistent main-thread timer,
  one-time connection panel, saved pairing, reconnect/backoff/disconnect, plus the
  single heartbeat path used by the embedded chat extension.
- `agent/runtime/insight.py`: embedded Conversation and Workspace Insight panels,
  local scene/object/missing-asset reports and the durable chat cursor/pending-message UI.
- `agent/runtime/core.py`: scene inspection, privileged Python with bounded stdout
  and cooperative Python deadline, screenshot/Render Result capture, app logs,
  persistent script workspace and durable command journal/outbox. Detects file
  loads and switches to a different active scene before executing stale commands.
- `agent/relay/{server,store,oauth}.py`: dependency-free relay core with
  single-owner MCP Streamable HTTP, SQLite jobs/chat events and OAuth state.
  Separate device/agent tokens, static OAuth client, S256 PKCE, audience/issuer
  binding, expiration and rotating refresh tokens. Correct tool annotations and
  MCP image content.
- `agent/relay/{chat,chat_login}.py`: optional Simple-mode Codex worker and one-time
  existing-account sign-in for the embedded Blender conversation.
- `agent/relay/{Dockerfile,compose.yml,Caddyfile,configure.py}`: HTTPS + persistent
  Docker volume deployment; credentials generated locally, never committed.
- `agent/relay/deploy-gce.sh`: Cloud Shell helper for a single persistent GCE relay
  host; secrets are generated on the VM and are not committed or echoed.
- `agent/relay/set-chatgpt-redirect.py`: safely adds the exact ChatGPT OAuth callback
  displayed by app management to the private relay allowlist.
- `scripts/apply-ios-agent-bridge.py`: validates exact pinned source anchors,
  adds native file/CMake frameworks/built-in registration and startup package.
- `scripts/prepare-source.sh`: invokes the transform after existing transforms.
- `scripts/package-ipa.sh`: verifies both bundled agent startup/core files.
- `.github/workflows/agent-checks.yml`: unit/protocol and official-client tests,
  plus Objective-C++ compilation against the actual iOS SDK.
- Main IPA workflow: runs agent tests before bootstrap; agent runtime/native
  changes now trigger an IPA build. Existing pins/features/entitlements preserved.

## Proven checks

- Initial CI all green: https://github.com/gorillaFKNgorgeous/-blender-ipad-M4/actions/runs/34223282161
  - Actual iOS SDK Objective-C++ syntax compilation passed (job 102051223490).
  - Official MCP Python client 1.26.0 initialization, tool discovery and call passed
    (job 102051223883). Production server does not depend on that package.
- Full IPA build 86 green: https://github.com/gorillaFKNgorgeous/-blender-ipad-M4/actions/runs/34229150516
  - Agent validation, source preparation, full Blender CMake configure, application
    build, Python.framework embedding and both IPA packages completed successfully.
  - This supersedes build 85's narrow CMake `Foundation` lookup failure.
- Physical iPad, build 86: GhostBlender → Agent Connection panel is visible in the
  3D View sidebar, proving the bundled startup runtime registered on-device.
- Physical iPad native transport import also succeeds. User executed:
  `import _ghostbridge_transport as gb; print(gb.status())`
  and received `foreground=True`, `available_memory_bytes=4792465872`,
  `physical_footprint_bytes=1649985072`.
  The two byte values sum exactly to 6 GiB (6442450944 bytes). Treat this as strong
  evidence of an approximately 6 GiB current process memory budget for this
  Signulous-installed profile, not as a guaranteed fixed jetsam threshold.
- Prior bridge behavior suite: 17 passed before the embedded-chat patch. The current
  CI run adds chat protocol/restart tests. It covers duplicate prevention, lost
  acknowledgements, persisted issued jobs, app restart journal/outbox, scene changes
  including in-file scene switching, expiry/uncertain outcomes, offline device,
  queue limits/cancellation, script traversal/symlinks/hash conflicts, Python
  time/output bounds, HTTP authentication/origin checks, image-content mapping,
  OAuth code single use, PKCE and refresh rotation.
- Existing Files transformation suite: all seven passed.
- Transform applied successfully to exact pinned bpy_interface.cc/CMakeLists.txt;
  duplicate application rejected. Upstream creator CMake installs the whole
  scripts directory, including the new startup package.
- Python compilation, shell syntax and git diff whitespace checks passed.

These checks prove the native module and startup UI load on a physical device. Subsequent development use also proves live relay connectivity and end-to-end ChatGPT MCP control of the running iPad Blender instance through **GhostBlender Simple**, including remote scene inspection and Blender operations. This is the current working development path.

That success is not completion of the full OAuth security design. `agent/relay/simple_server.py` deliberately removes per-tool OAuth requirements on the ChatGPT-facing MCP endpoint after Caddy's capability URL has gated access. The iPad-to-relay `/device/exchange` path still uses the device credential. The full OAuth-capable relay remains in `server.py` / `oauth.py` and has CI coverage, but production-style end-to-end OAuth acceptance has not yet been recorded.

## Current bridge status and next actions

### Embedded conversation checkpoint — 25 September 2026

The in-Blender conversation path is implemented on `main` and its bridge checks
are green at commit `dfda0b8a555ecd9ba3ecb413a9c84266c41b6268` (Actions run
`36139469748`). The implementation deliberately reuses the authenticated
`/device/exchange` heartbeat rather than adding a second iPad poller.

- `agent/runtime/insight.py` provides the Conversation and Workspace Insight UI.
  It sends one pending message at a time, consumes cursor-based status/final/error
  events, and is copied into future IPAs by the existing runtime copy transform.
- The currently installed iPad can test this before a rebuild through the matching
  persistent `ghostblender_insight.py` prototype already loaded in development.
- `agent/relay/store.py` durably journals chat messages/events and makes message
  delivery idempotent. A relay restart marks an in-flight model turn uncertain
  and never silently replays it because Blender may already have changed.
- `agent/relay/chat.py` runs one serialized Codex thread, persists its thread ID,
  and gives Codex the existing local GhostBlender MCP tools for live inspection,
  edits and captures.
- The relay image includes the pinned `openai-codex` Python SDK and
  `chat_login.py` for one-time ChatGPT device-code sign-in. Codex auth lives
  under the existing persistent `/data` volume; no OpenAI API key is required
  for this development path.
- `deploy-gce.sh` now deploys from canonical `GHOSTpad` rather than the obsolete
  repository and safely updates an existing VM checkout's origin.

**Not yet accepted live:** deploy current `main` to the existing GCE relay, run
`python chat_login.py` inside the relay container, then send a message from the
Blender Conversation panel and verify a model reply plus a small inspected edit.
Do not start another IPA build before this relay-side acceptance test; the current
prototype already speaks the new protocol.


- [x] Native iPad transport, Blender main-thread runtime, durable relay jobs and full OAuth-capable relay code implemented.
- [x] Bridge-enabled IPA installed on the physical iPad.
- [x] Persistent cloud relay deployed and paired.
- [x] Live ChatGPT-to-iPad Blender control established using **GhostBlender Simple** and used for real development.
- [x] Embedded Conversation / Workspace Insight prototype proven visible on the live iPad.
- [x] Durable relay chat protocol and Codex worker implemented in source.
- [ ] Redeploy the current relay and complete the one-time Codex/ChatGPT device-code sign-in.
- [ ] Run live in-Blender AI chat acceptance on the existing iPad prototype, including one inspected mutation.
- [ ] Build/install a later IPA to prove the now-bundled companion UI survives a clean install.
- [ ] Keep Simple as the private development bridge while active development benefits from its lower setup friction.
- [ ] Before broader/public/multi-user use, move the ChatGPT-facing side to the full authenticated OAuth path and run end-to-end OAuth acceptance.
- [ ] Expand regression evidence for reconnect, suspension/resume, crash recovery, capture, stale-job handling and normal Files behavior.

### Authentication modes

**GhostBlender Simple (current working development deployment):** `agent/relay/simple_server.py` reuses the normal relay implementation but removes the interactive OAuth requirement from ChatGPT-facing MCP calls. Caddy exposes an unguessable capability URL as the access gate. Device authentication is not removed: `/device/exchange` still requires `DEVICE_TOKEN`. This is suitable for the owner's private development environment, but intentionally is not a multi-user or production authentication design.

**Full relay (implemented security-hardening path):** `agent/relay/server.py` and `agent/relay/oauth.py` retain OAuth, PKCE and token handling. CI has tested this implementation, but the current day-to-day live bridge is Simple. Do not claim the full OAuth path is deployed/proven until its live acceptance sequence has been recorded.

## Relevant current documentation

- https://developers.openai.com/api/docs/guides/developer-mode
- https://developers.openai.com/plugins/build/auth
- https://developers.openai.com/api/docs/guides/tools-connectors-mcp
- https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
- https://github.com/ahujasid/blender-mcp (community project; not an established
  Blender Foundation release and not the rejected earlier project prototype)
- https://developer.apple.com/documentation/foundation/urlsession
- https://developers.openai.com/api/docs/guides/secure-mcp-tunnels — an alternative
  OpenAI-managed ingress path; requires Platform tunnel identity/runtime key and
  a running tunnel-client host. No verified iPadOS tunnel-client packaging was
  established. Do not assume it removes every host/device integration requirement.

## Run record

- Build 85: failed during CMake configure because bridge-specific `find_library`
  calls could not resolve Foundation/UIKit under the iOS cross-compile root path.
- Build 86: green at commit `4a2c008d424b13972b060ce7f6dbd430234fe33f`,
  Actions run `34229150516`.
- Physical-device verification, 2026-09-09: Agent Connection UI present;
  `_ghostbridge_transport.status()` succeeded with foreground state and real memory
  telemetry.
- Subsequent development milestone: persistent relay deployed and paired; **GhostBlender Simple** is working end-to-end from ChatGPT to the running iPad Blender instance. Full OAuth deployment remains a later security-hardening milestone.
