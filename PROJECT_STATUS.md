# Blender iPad project status

Updated: **23 September 2026**, including the working GhostBlender Simple bridge.

## Canonical-source migration — 12 September 2026

The migration that will make GHOSTpad contain its pinned Blender source is planned but
blocked: this execution environment could not retrieve commit
`2bc556e58e82eb3a801895f2cb1881c0267e5cd5` or its Git LFS objects. The existing build
workflow remains unchanged so the working reconstruction path is not broken. See
[GHOSTBLENDER_MIGRATION_PLAN.md](GHOSTBLENDER_MIGRATION_PLAN.md) and
[GHOSTBLENDER_SOURCE_AUDIT.md](GHOSTBLENDER_SOURCE_AUDIT.md). Do not claim that CI builds
checked-in Blender source until the import and equivalence checks are complete.

## Live MCP bridge — current status 23 September 2026

The bridge-enabled IPA is installed on the physical iPad, a persistent relay is deployed and paired, and **end-to-end ChatGPT control of Blender is working in development through GhostBlender Simple**. It is being used for live scene inspection and remote Blender operations.

GhostBlender Simple is the private-development mode in `agent/relay/simple_server.py`. It uses the same native transport, Blender runtime and durable relay machinery, keeps `DEVICE_TOKEN` authentication for the iPad relay exchange, but uses a Caddy-protected capability URL instead of interactive OAuth on the ChatGPT-facing MCP endpoint. It is intentionally not the production/multi-user security model.

The stronger OAuth-capable implementation remains in `agent/relay/server.py` and `agent/relay/oauth.py` and has CI coverage. Live end-to-end acceptance of that full OAuth deployment remains a security-hardening milestone. See [agent/HANDOFF.md](agent/HANDOFF.md).

## Build #83 correction and Scene partial-write investigation

[Build #83](https://github.com/gorillaFKNgorgeous/-blender-ipad-M4/actions/runs/34084686140)
failed before compilation in `Apply iOS codec framework integration`:
`source/blender/blenkernel/intern/blendfile.cc:1783: patch does not apply`.
It produced no new IPA and did not test the proposed runtime fix.

The pinned source `2bc556e58e82eb3a801895f2cb1881c0267e5cd5` already initializes
`PartialWriteContext::bmain.colorspace` from `reference_main.colorspace`.
The exact `blendfile.cc` blob is `ab8fb8f0012f827680efc12b3acf081eb260244f`.
The earlier claim that this initialization was missing was incorrect. Remove the
redundant `blendfile.cc` hunk from `ios-codec-frameworks.patch`; retain its two
codec bridge changes. This repairs patch application, with no new runtime fix.

The owner's finite probes still identify a Scene partial-write failure: F and G
crash, while H (ordinary Scene copy), I (embedded master Collection copy), and J
(Collection/Object/Mesh/Material partial write) pass. The Scene-writing crash
remains unresolved. Do not treat this build repair or a subsequent green build
as evidence that F is fixed, or request a repeat of F solely for this repair.

## Working baseline

**Build #81 is the current installed, working Blender 5.2 build.** The owner confirms saving and file functions work in normal use. It has not been exhaustively tested across every provider and edge case. Do not describe its normal saving/file behavior as unconfirmed or broken.

- Target device: 1 TB / 16 GB iPad Pro M4 running iPadOS 27; deployment minimum iPadOS 26.0.
- [Build #81](https://github.com/gorillaFKNgorgeous/-blender-ipad-M4/actions/runs/34008472726) completed successfully at 04:10 UTC on 6 September 2026.
- Exact build harness revision: `c9b9d486b390333e396ba882b33d9a2b6ae591e1`.
- Blender source pin: `2bc556e58e82eb3a801895f2cb1881c0267e5cd5`.
- Run #81 diagnostics record all 20 required feature flags enabled, 45 packaged Mach-O binaries, Python/NumPy/Zstandard frameworks, and both glTF codec bridge frameworks. All eight recorded patch hashes matched the audited checkout.
- Installation remains GitHub Actions → IPA → Signulous → iPad.

## Repository policy

**`main` is the sole development branch and contains build #81's application implementation.** Work directly on `main`. Do not create additional branches or pull requests unless the owner explicitly changes this instruction. Old version branches have been deleted, and obsolete PRs are closed.

PR #5 transferred 72 accumulated 5.2 commits into the older `main`. Its head differed from build #81 only in README.md. The merge was verified to preserve every file from build #81 apart from project documentation. The workflow trigger is now `main` plus manual dispatch; its build job, pins, patches, and packaging implementation are unchanged.

The obsolete MCP PR #3 and older input PR #1 are closed. The old MCP implementation is rejected: do not merge, restore, or port it as the basis for future agent work. Future Siri and agent integration must be implemented against the current working 5.2 runtime.

Closed/merged PR records and Git commit history describe past work; they are not alternate maintained versions or pending work.

## Follow-up validation

The normal save/file workflow is accepted as working. Broader provider, cancellation/replacement, repeated-operation, cold/warm launch, rendering, and lifecycle coverage remain useful regression work. Earlier builds had unexpected closures; their cause was not established, and a continuing failure in build #81 is not confirmed by the owner's latest report.

Use `Documents/BlenderFiles.log` for file handoffs. Collect exact build/signing route and reproduction steps with any new failure. Establish memory/render telemetry and recovery behavior using the installed process's actual memory allowance. The full-memory IPA name or the iPad's physical 16 GB does not establish that allowance.

The seven existing Files checks passed during the audit. They inspect transformation source text and do not exercise UIKit or File Provider extensions.

## Product direction

Deep, actionable Siri integration; a capable native assistant that can work on-device without internet; and the ability for users to authorize an agent of their choosing remain the goals. The [README](README.md) describes a new shared Blender action layer for App Intents/Shortcuts, on-device Foundation Models tools, and compatible external-agent adapters such as MCP.

These integrations are planned, not features of build #81. Build them on `main` against the current runtime and document handling, with broad operation coverage, real scene context, results, undo/checkpoints, and user-controlled access.
