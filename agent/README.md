# GhostBlender live agent bridge

> **Current status — 23 September 2026:** End-to-end remote control of the physical iPad Blender build is working in private development through **GhostBlender Simple**. Simple uses the normal native transport, Blender runtime and durable relay, while replacing interactive OAuth on the ChatGPT-facing MCP endpoint with a Caddy-protected capability URL. iPad-to-relay device authentication remains enabled. The full OAuth-capable relay code is retained and tested in this repository, but it is not the currently proven day-to-day deployment. See [HANDOFF.md](HANDOFF.md).


Implementation milestone, 8 September 2026. **The live iPad connection has not yet
been demonstrated.** The native code and protocol checks pass; a new signed IPA,
a deployed relay, and one-time client/device pairing are still required.

This code is newly implemented against the working Blender 5.2 baseline. It does
not restore the rejected MCP prototype. Siri and Apple Foundation Models are
separate future consumers of this runtime and are not required for this bridge.

## What is built

The app initiates authenticated HTTPS requests to a persistent relay. The relay
exposes MCP Streamable HTTP at `/mcp`; ChatGPT can use OAuth and an OpenAI API or
Codex client can use the separate agent bearer credential. No public listener,
port forwarding, subprocess, third-party Python package or OpenAI key is needed
on the iPad.

`native/ghostbridge_transport.mm` is compiled into Blender's Python library and
registered through its existing built-in module table. It uses NSURLSession and
never calls Python from its networking callbacks. `runtime/` is installed as a
Blender startup package. All scene access and Python execution occur in a
persistent `bpy.app.timers` callback on Blender's main thread.

Tools: `status`, `inspect_scene`, `execute_python`, `capture`, `diagnostics`,
`list_scripts`, `read_script`, `write_script`, `job_result`, `cancel_job`.
The model can inspect the actual scene, write and run Python, see resulting
images and errors, then choose its next edit. The user specifies the creative
or diagnostic goal; the agent chooses the Blender operations.

## Command behavior

1. Call `status` to obtain connectivity and the current `scene_id`.
2. Enqueue an inspection or other runtime tool with that `scene_id` and a unique
   `request_id` (8–80 letters, digits, underscores or hyphens).
3. Call `job_result` with the returned `job_id`. By default it waits up to two
   seconds; `wait_seconds` permits up to ten seconds. Await completion before
   dependent work. Captures return an actual MCP image content block.
4. Reuse a request ID only to retry the exact same request. A different payload
   with the same ID is rejected. Never blindly replay an `uncertain` job.
5. Inspect and capture after changes. Native Blender calls cannot be forcibly
   interrupted; the Python time limit only interrupts cooperative Python code.

Queued jobs expire after 90 seconds. Issued commands are never automatically
reissued. The device journals a command before running it and retains its last
result until acknowledged. An app crash during execution records an uncertain
outcome. File loads and active scene switches invalidate stale scene IDs.
Cancellation prevents queued work; it cannot undo or abort an issued native call.
Python errors and time limits can leave partial edits, so inspect before retrying.

The bridge is available while GhostBlender is active. Pairing survives ordinary
app restarts and updates that preserve the sandbox, and network failures use
bounded reconnection backoff. iPadOS can suspend an inactive app. Keep Blender
visible/active while the agent works (including an appropriate iPad multitasking
layout). Deleting the app removes its local pairing and journal. A persistent
connection does not keep a ChatGPT turn running indefinitely.

## Deployment

Use a single always-on host with an HTTPS domain and a persistent disk/volume.
The `relay/` server uses only Python's standard library. Its SQLite database must
survive process and host/container restarts. **Do not deploy the SQLite version
onto ephemeral Cloud Run/Functions storage or run multiple replicas.** A
serverless deployment needs a shared durable backend adaptation first.

No host, domain or paid service was provisioned by this implementation. No
OpenAI API requests or paid model calls were made.

For a Docker host, point the domain to that host, then in `agent/relay`:

```sh
python configure.py --origin https://YOUR-RELAY-DOMAIN \
  --redirect-uri https://chatgpt.com/connector_platform_oauth_redirect

docker compose up -d --build
```

`configure.py` writes a private `.env` file and refuses to overwrite an existing
one. The included Caddy reverse proxy terminates HTTPS. Ports 80 and 443 must be
reachable for that hostname. The relay itself has no public host port.

Credentials have separate roles:

| Setting | Where it is used |
| --- | --- |
| `PUBLIC_ORIGIN` | HTTPS origin on the host and in the app |
| `DEVICE_ID` / `DEVICE_TOKEN` | GhostBlender's connection panel |
| `AGENT_TOKEN` | Optional OpenAI Responses/Codex bearer integration |
| `OAUTH_CLIENT_ID` / `OAUTH_CLIENT_SECRET` | ChatGPT developer app configuration |
| `OWNER_KEY` | One-time owner consent form shown during OAuth linking |
| `OAUTH_REDIRECT_URIS` | Exact callbacks allowed for the configured client |

The OAuth server supports pre-registered clients, authorization code + S256
PKCE, issuer identification, audience binding, expiring access tokens and
rotating refresh tokens. Verify the callback displayed by ChatGPT app management
matches the allowlist; use that exact URI if it differs. Changing the owner key
or OAuth client secret invalidates previously issued OAuth credentials. Restart
the relay after changing its environment. Device and direct-agent tokens can be
rotated separately. Do not paste credentials into GitHub, logs or a public URL.

The server is for a single trusted owner. Full Python access has the app's
filesystem/network privileges; it is not an AST sandbox. Pair only trusted
agents. OAuth consent describes that access. Use the app's Disconnect button to
stop command delivery. Secret configuration is outside the managed script
workspace, but fully privileged Python can still access app-owned files.

Runtime tools and diagnostics preserve recent outcomes. The relay retains job
metadata for seven days and result/image payloads for one day. Include ordinary
backups of the persistent volume if longer history is required. The device
retains 256 journal entries, the pending result, capture and managed scripts.

## One-time iPad and ChatGPT connection

1. Install the new **Signulous-profile IPA** using the established signing route.
   Build 81 does not contain this bridge.
2. In a 3D View sidebar, open **GhostBlender → Agent Connection**. Enter the
   HTTPS relay origin, device ID and device token. Tap **Connect Agent**. This
   grants inspection, Python, image capture, diagnostic and script access.
3. In ChatGPT on the web, enable Developer mode and create an app for
   `https://YOUR-RELAY-DOMAIN/mcp`, with OAuth and the static client ID/secret.
   Authorize it with the owner connection key, then select the app in a chat.
4. Keep GhostBlender active and ask the agent to perform the acceptance sequence
   below. No further script or log copying should be needed.

OpenAI client controls may request confirmation for write tools. A remembered
approval applies according to that client's policy; this bridge does not bypass
those controls. Developer mode is documented for Plus on the web, but the live
account UI and final OAuth round trip still require acceptance testing.

For OpenAI Responses, the MCP tool configuration is:

```json
{
  "type": "mcp",
  "server_label": "ghostblender",
  "server_url": "https://YOUR-RELAY-DOMAIN/mcp",
  "authorization": "AGENT_TOKEN_FROM_YOUR_SECRET_STORE",
  "require_approval": "never"
}
```

This API example assumes the owner has authorized the current task on this private
instance. Approval handling and task budgets belong in the agent client. Do not treat a
ChatGPT subscription as an API credential or put an OpenAI API key on the iPad.

## Acceptance sequence — do not mark complete without device evidence

- [ ] `status` reports this installed build, foreground state and measured memory.
- [ ] `inspect_scene` matches the open scene and selection.
- [ ] Create a uniquely named temporary object in a separate collection; read its
      transform back; capture a genuine image and evaluate it.
- [ ] Modify that object, recapture and verify the change. Remove only the
      acceptance-test objects/collection afterward.
- [ ] Write/read/update an agent script, and execute it using `execute_python`.
- [ ] Cause a harmless Python exception; retrieve its traceback through diagnostics.
- [ ] Switch scenes with a queued request and confirm rejection of stale work.
- [ ] Interrupt network access and restore it; confirm reconnection without a
      duplicated edit. Reopen the app and confirm saved pairing.
- [ ] Suspend the app and verify offline/paused behavior; resume and inspect state.
- [ ] Capture a small existing Render Result; check colors/size and the actual
      iOS screenshot path. Neither was rendered on the device during implementation.
- [ ] Confirm normal Files open/save behavior remains intact.

Do not use `bpy.data.libraries.write({Scene})` for checkpointing or acceptance:
the known Scene partial-write crash remains unresolved. This bridge does not fix it.

## Validation evidence and resuming

`python3 -m unittest discover -s agent/tests -v` checks durable jobs, duplicate
prevention, loss of acknowledgements, restart behavior, scene invalidation,
bounds, script path confinement, Python timeout/output limits, OAuth and HTTP.
`tests/sdk_smoke.py` uses the official MCP Python client (1.26.0) in CI, independent
of the server implementation. The iOS SDK syntax check validates the Objective-C++
translation unit; a complete IPA build still owns final target linking/packaging.

See [HANDOFF.md](HANDOFF.md) for exact revisions, build status and remaining work.

## Sources

- [OpenAI developer mode](https://developers.openai.com/api/docs/guides/developer-mode)
- [OpenAI MCP authentication](https://developers.openai.com/plugins/build/auth)
- [OpenAI MCP/Responses](https://developers.openai.com/api/docs/guides/tools-connectors-mcp)
- [MCP Streamable HTTP](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
- [Community Blender MCP pattern](https://github.com/ahujasid/blender-mcp)
- [Apple URLSession](https://developer.apple.com/documentation/foundation/urlsession)

The cited Blender MCP implementation is a community project. It demonstrates
live Python access; it is not evidence that an official Blender Foundation MCP
release or an iPad-ready remote connection is already present.
