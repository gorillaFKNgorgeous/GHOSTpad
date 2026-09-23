# GhostBlender: Blender on iPad

GhostBlender brings Blender to iPad as a native application, with the ambition of making its creative tools deeply accessible through Siri, an assistant running on the device, and AI agents chosen by the user. The goal is an assistant that understands the open project, carries out substantial work, inspects the result, and helps the user refine it.

This repository contains the workflows, dependency bootstrap, packaging scripts, and compatibility changes used to build a pinned Blender iOS source tree. The current target is **Blender 5.2 on a 1 TB iPad Pro M4 with 16 GB RAM**, running iPadOS 27. The deployment minimum is iPadOS 26.0. This remains an experimental community port.

> **Canonical-source migration:** The planned import of the complete pinned Blender
> source is currently blocked because the migration environment could not access the
> upstream Git/LFS objects. The repository therefore still uses its working source
> reconstruction workflow; it must not yet be described as a canonical source checkout.
> See the [migration plan](GHOSTBLENDER_MIGRATION_PLAN.md) and
> [source audit](GHOSTBLENDER_SOURCE_AUDIT.md) for the exact status and resume steps.

**Current working baseline: build #81, confirmed by the owner on 6 September 2026.** Saving and file functions work in normal use; exhaustive edge-case testing remains follow-up work. **`main` is the sole development branch** and contains this implementation. Siri, the offline assistant, and agent access are future development on this baseline.

## Live agent connection implementation

The bridge source is **in this repository**, not only in the cloud deployment. The cloud host runs the relay service and keeps private runtime configuration/secrets, while GitHub remains the source of truth.

The bridge is split across:
- `agent/native/ghostbridge_transport.mm` — native iPad HTTPS transport compiled into Blender.
- `agent/runtime/` — Blender-side runtime, Agent Connection UI, scene inspection, Python execution, captures, diagnostics and job state.
- `agent/relay/` — MCP/HTTP relay, OAuth, durable store, Docker deployment files and GCE deployment helper.
- `scripts/apply-ios-agent-bridge.py`, `scripts/prepare-source.sh`, `scripts/package-ipa.sh` and the agent/build workflows — build and packaging integration.

Generated credentials and private `.env` values are intentionally **not** committed. Rebuilding or moving the cloud relay should be done from the repository files rather than by copying code out of Cloud Console.

A new [live MCP bridge](agent/README.md) is implemented against this 5.2 baseline.
It includes native outbound HTTPS networking, a main-thread Python dispatcher,
scene inspection, code execution, capture, diagnostics and persistent scripts,
plus a durable relay. **End-to-end remote control of the physical iPad is now working in development through the private single-owner GhostBlender Simple deployment.** The Simple mode uses the same native iPad transport and Blender runtime as the full bridge, but uses a Caddy-protected capability URL instead of interactive OAuth on the ChatGPT-facing MCP endpoint. Device-to-relay pairing remains authenticated with `DEVICE_TOKEN`.

The stronger OAuth-capable relay remains implemented in `agent/relay/server.py` and `agent/relay/oauth.py`; live acceptance of that path is a future security-hardening milestone. See the [resumable checklist](agent/HANDOFF.md) for the exact distinction and current status. The rejected earlier prototype remains
excluded. Siri and the offline assistant remain future work.

## Current project state

| Area | Current state |
|---|---|
| Development branch | [`main`](https://github.com/gorillaFKNgorgeous/-blender-ipad-M4/tree/main), the only maintained branch |
| Working iPad build | [Actions run #81](https://github.com/gorillaFKNgorgeous/-blender-ipad-M4/actions/runs/34008472726), completed 6 September 2026 at 04:10 UTC |
| Build #81 revision | [`c9b9d486b390`](https://github.com/gorillaFKNgorgeous/-blender-ipad-M4/commit/c9b9d486b390333e396ba882b33d9a2b6ae591e1), including the native Open/Save fixes |
| Device confirmation | The owner reports build #81 is installed and working, including saving and file functions. Earlier display/touch alignment fixes and scene loading are retained. |
| Follow-up coverage | Provider and cancellation/replacement edge cases, cold/warm launches, sustained rendering, memory behavior, and recovery |
| Main-branch promotion | PR #5 merged the accumulated 5.2 work into `main`. It contained build #81's implementation plus README updates. Subsequent consolidation changes concern documentation and workflow triggers. |
| Older work | Obsolete input and MCP PRs are closed. Old version branches have been deleted; further work belongs directly on `main`. |
| Siri / assistant / agents | Planned. The obsolete MCP prototype has been rejected and is not part of this baseline. |

PR #5 was large because it moved 72 accumulated commits into the old default branch. **Build #81 was built before that promotion, from `c9b9d48`; the PR did not introduce a different application implementation.** Its head differed from build #81 only in the README. The promotion was checked to preserve every non-documentation file, and the build job remains unchanged apart from its main-only trigger.

The supplied 5 September manifests record harness revision `ecf4b8882de2`. This audit also retrieved **run #81's own diagnostics**: its source manifest records `c9b9d486b390`, its feature flags match the supplied manifest, and its bundle manifest records **45 Mach-O binaries**, including `Python.framework`, native NumPy and Zstandard modules, and both glTF codec bridge frameworks. All eight recorded patch hashes match the audited checkout. Keep the harness revision as well as patch hashes: the harness also contains source-transform scripts.

Build #81 has both successful CI evidence and owner-confirmed normal save/file operation. Feature flags and framework presence alone do not establish correctness for every enabled subsystem or edge case.

## Feature coverage

The full profile preserves major Blender capabilities. Configuration fails if any of its **20 required feature flags** becomes disabled. The complete `WITH_*` cache is recorded in the feature manifest.

| Subsystem | Build #81 configuration / packaging evidence |
|---|---|
| Python | CPython **3.13.13**, embedded `Python.framework`, standard library, retained `bl_pkg` extension manager, requests and certificate data |
| Scientific / compression modules | **NumPy 2.3.4** built for iOS with Accelerate ILP64 BLAS/LAPACK; **Zstandard 0.25.0**; native modules packaged as frameworks |
| Rendering | Metal viewport/EEVEE code; Cycles and its Metal device backend enabled; Embree, path guiding, and OpenImageDenoise enabled |
| Scene interchange | USD with schema/plugin data, MaterialX data, Alembic, Draco, and meshoptimizer |
| Volumes / geometry | OpenVDB and OpenSubdiv; the full manifest records the other geometry and simulation options |
| Video / audio | FFmpeg, Audaspace, OpenAL, and libsndfile enabled; the target CoreAudio option is disabled |
| Language support | Internationalization enabled |

Three explicitly blocked features remain **OFF**: Hydra/Storm, because the pinned iOS USD bundle lacks its required HgiMetal/Storm backend; OpenXR, because this port has no iPad runtime/backend; and Cycles OSL, because the pinned iOS build path does not support the required compilation step. Core USD import/export is separate from Hydra rendering.

The extension manager is retained, but compatibility must be established per extension. Bundled iOS Python does not establish support for desktop subprocess workflows or arbitrary desktop native modules.

## Native Files, saving, and stability

| User action | Implemented behavior in the current source transforms |
|---|---|
| **File → Open** | Uses the picker mode that returned selections in device testing, then copies the temporary import once into `Documents/Blender/Imports`. Collisions receive a unique suffix. Edits target that imported copy. |
| **Open in Blender** from Files | Scene-based handoff retains the provider URL for open-in-place access. Cold and warm launches use a queue until Blender/GHOST is ready. |
| **Save As**, or first Save | Uses UIKit's move/export picker with a temporary cache seed. Blender then writes the real project to the returned document path. The seed is not a saved `.blend` project. |
| **Save** after a path is established | Blender writes the current project path without requesting another picker, subject to actual write access. |

PR #4 addresses a specific duplicated-path cause: the old folder-save callback appended a filename to an export result that already contained it, producing paths such as `Untitled.blend/Untitled.blend`. The owner now confirms saving and file functions work on build #81. Keep provider-specific and repeated-operation cases in the regression matrix; exhaustive edge-case coverage is not claimed.

The patches retain provider security-scoped URLs, route picker results to the originating Blender window, use a normal-level application window, and suspend Blender gestures behind native modal UI. Picker/handoff diagnostics go to **`Documents/BlenderFiles.log`**. The [native Open/Save audit](https://github.com/gorillaFKNgorgeous/-blender-ipad-M4/blob/main/.github/IOS_SAVE_AUDIT.md) contains the implementation history, current device acceptance, and follow-up provider test matrix.

**Long-run stability and recovery need broader validation.** Earlier builds had unexpected closures whose cause was not established. Memory pressure and rendering remain investigation candidates if failures recur; this audit does not establish a continuing failure in build #81. Comprehensive memory/render telemetry and recovery checks remain priorities.

Build #81 is the current working baseline. For broader regression coverage and future builds, record the run number and signing route, then verify:

1. Launch, touch alignment, keyboard/mouse/Pencil input, and viewport interaction.
2. Save As to one regular `.blend` file; modify and Save; close and reopen it. Repeat across On My iPad, iCloud Drive, and supported providers, including cancellation and denied/offline access.
3. In-app imports and Files-app cold/warm launches, checking that paths stay stable and folders do not nest unexpectedly.
4. Representative EEVEE/Cycles scenes, enabled import/export formats, Python modules, and audio/video operations, recording failures by subsystem.
5. Background/resume, larger scenes, peak memory/thermal behavior, and recovery after an unexpected termination.

For a read-only check in Blender's Python Console:

```python
import bpy, sys
print("Blender:", bpy.app.version_string, "Python:", sys.version)
print("Project:", repr(bpy.data.filepath))
print("Project directory:", bpy.path.abspath("//") if bpy.data.filepath else "Unsaved project")
```

Blender's `//` is relative to an established project path. An untitled session does not establish a project directory, and the process working directory is not a reliable document location.

## Download and install

1. Open [build #81](https://github.com/gorillaFKNgorgeous/-blender-ipad-M4/actions/runs/34008472726) and download **`Blender-iPad-M4-5.2-full`** from its artifacts. GitHub may require sign-in.
2. Extract the artifact and use **`Blender-iPad-M4-5.2-Signulous-unsigned.ipa`** for the project's existing Signulous signing/install route.
3. Keep the matching source, feature, and bundle manifests. The separate **`Blender-iPad-M4-5.2-build-diagnostics`** artifact contains logs and configuration evidence.

| IPA | Purpose |
|---|---|
| `Blender-iPad-M4-5.2-Signulous-unsigned.ipa` | Fallback signing profile without the restricted increased-memory entitlement |
| `Blender-iPad-M4-5.2-full-memory-unsigned.ipa` | Same build with an increased-memory entitlement request, requiring a provisioning/signing profile that supports it |

Both packages have ad-hoc handoff signatures and are unprovisioned; an installation service/profile must supply a valid device signature. The full-memory filename does not establish the installed app's memory allowance. See Apple's [increased memory limit entitlement](https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.developer.kernel.increased-memory-limit).

Artifacts are retained for **21 days**. Their run pages remain useful records after downloads expire.

## Building the current branch

The normal workflow is **GitHub Actions → IPA → Signulous → physical iPad testing**. No local Mac is required to use it.

In [GitHub Actions](https://github.com/gorillaFKNgorgeous/-blender-ipad-M4/actions/workflows/build-unsigned-ipa.yml), choose **Build full Blender 5.2 iPad M4 IPA → Run workflow → `main`**. Relevant pushes to `main` also trigger the build. Development stays on `main`; do not create additional branches or pull requests unless the owner explicitly changes this instruction.

| Build input | Pin / requirement |
|---|---|
| Blender iOS source | [`salmazov/blender-ios@2bc556e58e82eb3a801895f2cb1881c0267e5cd5`](https://github.com/salmazov/blender-ios/tree/2bc556e58e82eb3a801895f2cb1881c0267e5cd5) |
| iOS dependency bundle | `393201c7c8525941553f6a96e19b909d6b3bfc4f` |
| macOS host-tool bundle | `a3e20428fb0ab2231903608cdca90301e130dbfc` |
| Host | Apple-silicon `macos-15` runner, **Xcode 26.3**; run #81 records build `17C529` |
| Target | `arm64` / `iphoneos`, deployment target **26.0** |
| Application ID | `com.gorillafkngorgeous.blenderipad52` |

The [workflow](https://github.com/gorillaFKNgorgeous/-blender-ipad-M4/blob/main/.github/workflows/build-unsigned-ipa.yml) is the authoritative recipe. It prepares source and libraries, restores or builds iOS dependencies, configures Blender, builds the **`blender` scheme** so its app-bundling phase runs, verifies linkage, and packages both IPAs. Source preparation alone is not a complete local build recipe. Local reproduction requires an Apple-silicon Mac and the full bootstrap/configuration steps.

The bootstrap replaces the original bundle's Python 3.11 with matching host/target CPython 3.13.13. It uses iOS compiler wrappers and explicit target dependency paths to prevent macOS library contamination. Native Python modules use iOS frameworks and `.fwork`/`.origin` records; the glTF bridge frameworks contain their statically linked codec dependencies.

Packaging checks cover required assets/modules, framework mappings and signatures, Python linkage, resolvable relative library dependencies, architecture/platform checks, unresolved Git LFS pointers, and entitlement separation. These complement physical-device acceptance.

The M4 shader policy uses a **12 GiB physical-memory threshold** to select higher compilation concurrency, leaving a performance core for interaction. Lower-memory devices or serious thermal pressure retain a two-thread limit. It does not yet use the installed process's actual memory allowance for that decision. Measuring this policy under the Signulous profile is part of stability work. The Cycles iOS dispatch cap is retained for GPU watchdog protection.

## Siri, offline assistance, and agent choice

**Product direction:** substantial, context-aware control over Blender by speaking or typing, with useful native on-device operation without internet access and the ability to choose an external agent.

The proposed architecture shares one versioned Blender action layer across three entry points:

| Entry point | Planned integration | Availability boundary |
|---|---|---|
| Siri and Shortcuts | Swift App Intents, App Entities, and App Shortcuts for supported actions and project context | Siri behavior, supported schemas, OS/language/region, and offline invocation require device validation. |
| Assistant inside GhostBlender | Apple's on-device Foundation Models backend with typed Blender tool calls; local text input and on-device speech where available | Requires supported hardware/settings and downloaded model/speech assets. Offline tools and assets must also avoid network dependencies. |
| User-selected agents | MCP and adapters for other compatible clients using the same actions/results | Remote agents/relays require connectivity. Clients must support a compatible transport and authentication method. |

Apple's [App Shortcuts](https://developer.apple.com/documentation/appintents/app-shortcuts) expose intents with invocation phrases. Newer natural-language Siri integration uses [App Schemas](https://developer.apple.com/documentation/appintents/making-actions-and-content-discoverable-by-apple-intelligence); the [current domain catalog](https://developer.apple.com/documentation/appintents/app-schema-domains) has no general Blender/3D-modeling domain. Map matching file/search actions to supported schemas and expose other operations through custom intents/shortcuts and the in-app assistant. Arbitrary modeling language requires validation beyond registering an intent.

The [Foundation Models on-device API](https://developer.apple.com/documentation/foundationmodels/generating-content-and-performing-tasks-with-foundation-models) and [tool calling](https://developer.apple.com/documentation/foundationmodels/expanding-generation-with-tool-calling) provide the basis for the offline assistant. This is a separate integration from the system Siri interface. Select the on-device backend explicitly, check availability, and retain direct controls when unavailable. The current Xcode 26.3 build can target the original iPadOS 26 APIs; adopting iPadOS 27-only APIs requires a separate toolchain update and availability checks.

Blender should supply exact scene data and execute operations. The model interprets intent and selects tools; it should not have to invent Blender code or estimate geometry to complete ordinary commands.

### Planned action coverage

| Area | Work the assistant should be able to perform |
|---|---|
| Scene understanding | Inspect selection, objects, collections, hierarchy, materials, cameras, active mode, and render state; resolve “this object” from actual context. |
| Modeling | Create/arrange geometry, edit transforms, duplicate/align, apply modifiers, manage selection, and build reusable Geometry Nodes workflows. |
| Materials and lighting | Create/edit materials and nodes, assign textures, adjust lights, and prepare a requested look. |
| Animation | Set keyframes, inspect rigs/actions, adjust timing, and construct repeatable animation operations. |
| Rendering and delivery | Configure EEVEE/Cycles, frame cameras, start/cancel jobs, inspect outputs, import/export, and save through the document layer. |
| Project assistance | Explain controls, identify missing assets, report measured resource use, keep checkpoints, and undo supported edits. |

Build the catalog from Blender's Python/RNA/operator interfaces where practical, with context requirements and capability checks. Implement broad, systematic coverage against the current 5.2 runtime. Versioned requests/results should carry object identifiers, validated parameters, actual results/errors, and job state. Run Blender data access and mutations on its main thread through a controlled bridge from Swift or the network layer. Start with foreground operation, bounded queues, and explicit unavailable/disconnected results when Blender cannot service work. Undo, checkpoints, cancellation, and post-action inspection support longer sequences; irreversible file operations need their own handling.

Users should be able to choose and disconnect agents, scope scene/file access, decide what leaves the device, and see what changed. Local assistance must remain independent of an external provider account. A cloud fallback should be an explicit choice.

### Agent implementation policy

The previous MCP prototype and its older build/input baseline are discarded. Its PR is closed, and its code is not integrated into `main`. Implement agent support afresh against the working 5.2 runtime and document layer. Do not merge or restore the obsolete prototype as an implementation shortcut.

[MCP Streamable HTTP](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports) remains an option for compatible external agents. Transport, authentication, connection lifecycle, and job handling will be designed and validated with the new shared action layer. No running MCP service or agent integration is claimed for build #81.

## Development priorities

1. **Preserve and measure the working baseline.** Keep build #81's normal save/file behavior working, expand edge-case coverage, and establish recovery and memory/render measurements under the installed signing profile.
2. **Establish the common action layer.** Implement directly against `main`'s 5.2 runtime; add context/capability discovery, identifiers, results, undo/checkpoints, and cancellation. Verify the same operations through each adapter.
3. **Deliver an offline assistant milestone.** Add the Swift bridge and on-device model; demonstrate a local editing sequence and result inspection with networking disabled. Measure additional memory and latency alongside Blender.
4. **Integrate Siri deeply.** Expose project entities and actions through App Intents/Shortcuts and matching schemas. Test actual invocation, foreground handoff, context resolution, and offline behavior on supported OS versions.
5. **Open agent choice and expand coverage.** Validate multiple clients, reconnect/retry behavior, permissions, and longer creative workflows. Track supported operations against the catalog rather than treating a small tool list as the final product.

## Repository guide and contributions

All current code, build settings, and documentation live on **`main`**.

| Location | Responsibility |
|---|---|
| [Build workflow](https://github.com/gorillaFKNgorgeous/-blender-ipad-M4/blob/main/.github/workflows/build-unsigned-ipa.yml) | Bootstrap, configure, compile, package, and upload |
| [`scripts/prepare-source.sh`](https://github.com/gorillaFKNgorgeous/-blender-ipad-M4/blob/main/scripts/prepare-source.sh) | Source/library pins and ordered transforms |
| [`patches/`](https://github.com/gorillaFKNgorgeous/-blender-ipad-M4/tree/main/patches) | Blender/iOS, geometry, Files, linkage, codec, and NumPy changes |
| [`scripts/apply-ios-files-scene.py`](https://github.com/gorillaFKNgorgeous/-blender-ipad-M4/blob/main/scripts/apply-ios-files-scene.py) | Final native picker and scene handoff transformations |
| [`scripts/verify-app-linkage.sh`](https://github.com/gorillaFKNgorgeous/-blender-ipad-M4/blob/main/scripts/verify-app-linkage.sh) | Embedded Python and relative dependency checks; bundle manifest |
| [`scripts/package-ipa.sh`](https://github.com/gorillaFKNgorgeous/-blender-ipad-M4/blob/main/scripts/package-ipa.sh) | Bundle validation, mappings, signatures, and IPA profiles |
| [`scripts/write-feature-manifest.sh`](https://github.com/gorillaFKNgorgeous/-blender-ipad-M4/blob/main/scripts/write-feature-manifest.sh) | Required feature gates and complete feature manifest |
| [Files regression checks](https://github.com/gorillaFKNgorgeous/-blender-ipad-M4/blob/main/tests/test_ios_native_save_transform.py) | Seven source-text checks for native Files transforms |

Keep feature removals explicit and evidence-based. Preserve the host/iOS dependency boundary, package the complete app, and associate device reports with an exact build and signing route. The seven Files checks passed during this audit; they inspect source text and do not exercise UIKit or Files providers. Run them with `python3 -m unittest discover -s tests -v` from the 5.2 checkout.

Blender and its upstream iOS contributors provide the foundation for this project; the current pin comes from [Sergei Almazov's iOS fork](https://github.com/salmazov/blender-ios). Blender is GPL-licensed. Preserve corresponding source, modifications, build information, and applicable dependency notices when distributing modified builds; see [Blender's licensing information](https://www.blender.org/about/license/).
