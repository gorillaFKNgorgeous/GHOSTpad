# GhostBlender live MCP — resume here

Updated 2026-09-23. **The bridge source is fully tracked in this repository, and the native iPad side has been verified on-device. The cloud deployment is only the running relay host, not the canonical source location.** Do not
restart this work or restore the old MCP prototype. Read this file and
`agent/README.md`, then continue from relay deployment/pairing.

## Goal and project constraints

Direct, persistent, authenticated access from ChatGPT/OpenAI to running iPad
Blender 5.2: scene inspection, Python execution, actual images, diagnostics,
script creation/modification and autonomous iteration. The user supplies goals,
not Blender operator names. One-time signed-app installation and pairing remain
necessary; the intended steady state has no manual code/log transfer.

- Repo: https://github.com/gorillaFKNgorgeous/-blender-ipad-M4
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
  one-time connection panel, saved pairing, reconnect/backoff/disconnect.
- `agent/runtime/core.py`: scene inspection, privileged Python with bounded stdout
  and cooperative Python deadline, screenshot/Render Result capture, app logs,
  persistent script workspace and durable command journal/outbox. Detects file
  loads and switches to a different active scene before executing stale commands.
- `agent/relay/{server,store,oauth}.py`: dependency-free single-owner/single-process
  MCP Streamable HTTP, SQLite jobs and OAuth state. Separate device/agent tokens,
  static OAuth client, S256 PKCE, audience/issuer binding, expiration and rotating
  refresh tokens. Correct tool annotations and MCP image content.
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
- Latest local behavior suite: 17 passed. Covers duplicate prevention, lost
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

These checks now prove the native module and startup UI load on a physical device.
They still do not prove live relay connectivity, remote scene execution/capture,
network foreground transitions or a completed ChatGPT OAuth/MCP round trip.

## Required next actions

- [x] Preserve implementation directly on main and keep this handoff current.
- [x] Implement device runtime, authenticated relay and deployment configuration.
- [x] Validate protocol, journal and source-transform logic.
- [x] Pass initial iOS SDK syntax and independent official MCP-client CI checks.
- [x] Pass full integration IPA build 86.
- [x] Install build 86 on the physical iPad and verify native bridge load/status.
- [ ] Create/select a Google Cloud project (or another persistent HTTPS host) and
      deploy the single-process relay with durable storage. Do not run the current
      SQLite relay on ephemeral Cloud Run/Functions storage or multiple replicas.
- [ ] Generate distinct credentials using `configure.py`; verify `/health` and
      endpoint authentication before pairing the iPad.
- [ ] Pair via GhostBlender → Agent Connection and confirm `Connected` state.
- [ ] Create/authorize the ChatGPT developer app for `/mcp` using static OAuth
      credentials. Add the exact callback shown by ChatGPT to the relay allowlist.
- [ ] Execute the acceptance sequence in agent/README.md: status → inspect → edit →
      capture → refine, scripts, error diagnostics, scene switch, network
      drop/reconnect, app reopen, suspension and normal Files behavior. Record evidence.

The currently running ChatGPT session has no GhostBlender MCP tool registered.
Do not claim live remote access, successful remote images or background autonomy
until those are observed. The bridge can persist, but active model work still
follows client run/usage/approval limits. iPadOS can suspend an inactive app.
Native Blender calls cannot be forcibly cancelled; a failed/uncertain edit may
have changed the scene and must be inspected before retry.

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
  telemetry. End-to-end MCP relay/ChatGPT pairing remains the next milestone.
