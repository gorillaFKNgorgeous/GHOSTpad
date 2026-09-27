the canonical source code for GhostBlender Simple and the beginnings of GHOSTroom are now in the GHOSTpad repo, but the running system also has deployed state in Google Cloud and installed/runtime state on the iPad. Those should not be confused with source-of-truth code.

The repo is currently distributed like this:

GHOSTpad/
│
├── agent/
│   ├── native/
│   │   └── ghostbridge_transport.mm
│   │
│   ├── runtime/
│   │   ├── __init__.py
│   │   ├── core.py
│   │   └── insight.py
│   │
│   ├── relay/
│   │   ├── server.py
│   │   ├── simple_server.py
│   │   ├── store.py
│   │   ├── chat.py
│   │   ├── chat_login.py
│   │   ├── oauth.py
│   │   ├── Caddyfile
│   │   ├── Dockerfile
│   │   ├── compose.yml
│   │   ├── deploy-gce.sh
│   │   └── ...
│   │
│   ├── tests/
│   │   ├── test_bridge.py
│   │   └── sdk_smoke.py
│   │
│   ├── README.md
│   └── HANDOFF.md
│
├── scripts/
│   ├── apply-ios-agent-bridge.py
│   ├── package-ipa.sh
│   ├── prepare-source.sh
│   └── ...
│
├── patches/
├── docs/
│
└── .github/workflows/
    ├── agent-checks.yml
    └── build-unsigned-ipa.yml

What each part means:

agent/native/ghostbridge_transport.mm is the native iPad transport. This is the Objective-C++ bridge between Blender/Python and iPad networking/native state.

agent/runtime/ is the code that actually runs inside Blender. core.py implements the GhostBlender tool operations, __init__.py manages the heartbeat/job loop, and insight.py is the current beginning of the in-Blender conversation/workspace UI that eventually led us to the GHOSTroom idea.

agent/relay/ is the cloud side. This contains both GhostBlender Simple and the early embedded-chat architecture.

The especially relevant pieces are:

* simple_server.py = current GhostBlender Simple mode
* server.py = common MCP/relay server
* store.py = durable jobs plus the first chat journal
* chat.py = current Codex-specific embedded chat worker
* oauth.py = full authentication path
* Caddyfile = HTTPS/capability routing
* deploy-gce.sh = deploy/update the Google Cloud VM
* Dockerfile / compose.yml = relay container

So GhostBlender Simple is not some mystery service sitting only in Google Cloud anymore. Its implementation is in GHOSTpad.

The Google VM is now effectively a deployed copy of that repo.

There are, however, three categories of things that deliberately do not live in GitHub.

First, secrets and live relay state. On the Google VM we have things such as:

.env
device tokens
agent/capability credentials
OAuth credentials
Codex login session
SQLite relay database
chat/job history

Those belong on the server’s persistent volume, not GitHub.

The SQLite database is also where the very early version of what we’ve now described as the Shared Workspace Ledger is beginning to emerge.

Second, the installed iPad application. The build process copies agent/runtime/ into Blender and compiles the native transport into the app.

So the installed GHOSTpad contains copies of:

ghostbridge runtime Python
compiled ghostbridge native transport

but those installed copies are build products. We should treat the GitHub versions as authoritative.

There is also one temporary historical wrinkle on the iPad right now: the live build has a persistent development script called:

ghostblender_insight.py

That is the prototype we used to bolt the conversation panel onto the currently installed build without making another IPA.

A cleaned permanent descendant of that work already exists as:

agent/runtime/insight.py

in GHOSTpad.

So the iPad script is a temporary development shim, not something we should preserve as a second source tree.

Third, your ChatGPT and Claude plugin/app configurations. The actual GhostBlender MCP server they talk to is in the GHOSTpad repo, but the plugin registrations themselves live in ChatGPT/Claude’s configuration systems. They contain things like the endpoint/capability URL and connection metadata. They’re not application source code.

There’s another important distinction: the entire Blender 5.2 source tree itself is not duplicated in GHOSTpad. GHOSTpad contains the scripts and patches needed to transform the pinned Blender source into our iPad build. GitHub Actions pulls the appropriate Blender source and applies our changes.

So conceptually today the project looks like this:

                    GHOSTpad GitHub
                  CANONICAL SOURCE
                         │
        ┌────────────────┼────────────────┐
        │                │                │
  GhostBlender       Cloud relay       iPad build
   source code          source          transforms
        │                │                │
        ▼                ▼                ▼
  external agents   Google Cloud VM   GHOSTpad IPA
 ChatGPT / Claude   runtime + state     on iPad

One thing does not exist yet, and I want to be very clear about it:

The native GHOSTroom UIKit/SwiftUI interface has not been written yet.

I checked the repo. There currently isn’t a SwiftUI/UIKit GHOSTroom implementation, nor even a GHOSTroom-named source area.

What we have are its ancestors:

agent/runtime/insight.py
        ↓
proof that embedded chat can live beside Blender
agent/relay/chat.py
        ↓
proof that a selected AI worker can consume messages
agent/relay/store.py
        ↓
beginning of persistent conversation/event state
GhostBlender Simple
        ↓
proof that external agents can control the same Blender

Those are exactly the pieces from which we can now build GHOSTroom properly.

And before we do much more coding, I think we should clean the architecture visually inside the repo. I would create something closer to:

GHOSTpad/
│
├── ghostblender/
│   ├── native/
│   ├── runtime/
│   ├── protocol/
│   └── tests/
│
├── ghostroom/
│   ├── native/
│   │   ├── UI/
│   │   ├── Models/
│   │   └── Bridge/
│   ├── protocol/
│   └── tests/
│
├── relay/
│   ├── router/
│   ├── agents/
│   │   ├── codex/
│   │   ├── claude/
│   │   └── ...
│   ├── workspace/
│   └── server/
│
└── build/

Not necessarily that exact reshuffle immediately, because moving working bridge code just for aesthetic tidiness is risky. But GHOSTroom itself deserves a clearly bounded home from day one, rather than growing sideways through insight.py and chat.py.
