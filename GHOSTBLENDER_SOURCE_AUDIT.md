# GhostBlender source migration audit

Updated: **12 September 2026**

This audit records findings observed while preparing to convert the build-harness-only
repository into the canonical GhostBlender source repository. It deliberately separates
migration blockers from issues that should be handled later. The working build remains
the existing reconstructed Blender 5.2 baseline.

## Executive status

**CRITICAL — canonical source import is blocked.** The required upstream commit
`2bc556e58e82eb3a801895f2cb1881c0267e5cd5` is not present locally, and outbound GitHub
access returned HTTP 403. The full Blender source, its Git LFS payload, and its exact tree
identity could not be obtained. CI still clones and reconstructs Blender under
`work/blender`; it does not yet compile checked-in Blender source.

The migration plan is in [GHOSTBLENDER_MIGRATION_PLAN.md](GHOSTBLENDER_MIGRATION_PLAN.md).

## Current reconstruction inventory

The active source preparation order is:

1. Clone `salmazov/blender-ios`, fetch and detach at
   `2bc556e58e82eb3a801895f2cb1881c0267e5cd5`, then fetch its LFS content.
2. Checkout the `lib/ios_arm64` submodule at
   `393201c7c8525941553f6a96e19b909d6b3bfc4f` and `lib/macos_arm64` at
   `a3e20428fb0ab2231903608cdca90301e130dbfc`.
3. Apply, in order, `ios-5.2-m4-full.patch`, `ios-live-view-geometry.patch`,
   `ios-native-files.patch`, `ios-runtime-linkage.patch`, and
   `ios-files-lifecycle-v2.patch`.
4. Run `apply-ios-codec-frameworks.py` and `apply-ios-agent-bridge.py`.
5. CI additionally applies `ios-codec-frameworks.patch` after source preparation.
6. NumPy 2.3.4 is downloaded separately and receives
   `numpy-2.3.4-ios-support.diff` and `numpy-2.3.4-fwork-bundle-root.diff`.

### First-party deltas to convert to direct source

When the pinned tree becomes available, these active first-party deltas should be
applied and committed directly to Blender source:

- `patches/ios-5.2-m4-full.patch`
- `patches/ios-live-view-geometry.patch`
- `patches/ios-native-files.patch`
- `patches/ios-runtime-linkage.patch`
- `patches/ios-files-lifecycle-v2.patch`
- `patches/ios-codec-frameworks.patch`
- Blender-tree edits and copied files produced by
  `scripts/apply-ios-codec-frameworks.py`
- Blender-tree edits and copied files produced by
  `scripts/apply-ios-agent-bridge.py`

### External deltas to retain

The following patches modify externally downloaded NumPy rather than Blender and should
remain external:

- `patches/numpy-2.3.4-ios-support.diff`
- `patches/numpy-2.3.4-fwork-bundle-root.diff`

The iOS and macOS library bundles should remain pinned external dependencies. Their
compiled contents must not be committed merely to make the source import appear
complete.

## Prioritized findings

### CRITICAL

1. **Exact source is unavailable in this environment.** Importing any substitute would
   make provenance and behavioral equivalence unverifiable. This blocks the requested
   canonical-source end state.

### HIGH

1. **CI still reconstructs first-party source.** `SOURCE_DIR` points at `work/blender`,
   and `prepare-source.sh` clones Blender and applies GhostBlender deltas. This must only
   change atomically with the real source import.
2. **The source delta has two codec stages.** The Python codec transform runs during
   preparation and a separate codec patch runs in CI. Their combined output and order
   require a byte-for-byte comparison before the patch files are retired.
3. **Device correctness is only partly validated.** The Scene partial-write crash
   described in `PROJECT_STATUS.md` remains unresolved. A green build must not be
   represented as resolving it.
4. **Physical iPad validation is unavailable here.** UIKit lifecycle, Files providers,
   Metal rendering, signing, memory allowance, background/resume, and the live MCP
   connection require the target device and provisioning route.

### MEDIUM

1. **Most save tests are structural.** The existing native-save tests inspect source
   transformation text and cannot exercise UIKit or File Provider behavior.
2. **Stale first-party patch exists.** `ios-desktop-input.patch` is present but is not
   applied by the authoritative preparation script. It should be classified as historical
   or removed after confirming its intended changes are already represented elsewhere.
3. **Unused source transform exists.** `apply-ios-files-scene.py` is not invoked by the
   authoritative preparation workflow. Its relationship to the active lifecycle patch
   should be documented before cleanup.
4. **Hard-coded platform assumptions are extensive.** The build intentionally targets
   Apple silicon, Xcode 26.3, iOS 26.0, iPadOS 27, `arm64`, and an M4-oriented memory/
   shader policy. These are baseline constraints, but each will need deliberate review
   for additional devices or newer toolchains.
5. **Build timeout and cache coupling are brittle.** The six-hour CI job mutates checked
   out dependency submodule directories and relies on cache overlay clearing. Cache-key,
   dependency-pin, and cleanup changes must remain synchronized.
6. **Python embedding needs device regression coverage.** CPython 3.13.13, framework
   placement, `.fwork` resolution, native NumPy modules, certificates, and extension
   manager behavior are checked mainly through packaging/static inspection.

### LOW

1. **Historical repository language is now confusing.** Documentation repeatedly calls
   the project a harness and references the former repository/branch policy. Update it
   only when the canonical source actually lands so documentation does not claim a state
   that has not been achieved.
2. **Patch hashes cease to describe first-party provenance after import.** Replace them
   in the build manifest with the upstream commit/tree ID and GhostBlender repository
   commit, while retaining hashes for external NumPy patches.

### INFO

1. The current configuration intentionally retains Python 3.13, Metal/EEVEE, Cycles
   Metal, USD, OpenVDB, OpenSubdiv, FFmpeg, audio, internationalization, Draco,
   meshoptimizer, agent/MCP runtime, native Files handling, and two unsigned IPA variants.
2. Hydra/Storm, OpenXR, and Cycles OSL are deliberately disabled; the migration must not
   silently reinterpret those flags.
3. Build products, dependency bundles, caches, DerivedData, archives, logs, and IPAs are
   already excluded conceptually and must remain outside version control.

## Recommended follow-up order

1. Restore access to the pinned repository and Git LFS payload (or provide a verified
   local checkout).
2. Record the upstream commit and tree ID, import the source snapshot without dependency
   submodule payloads, and apply active deltas directly.
3. Generate and compare legacy-versus-canonical manifests, including file modes and
   content hashes; investigate every difference.
4. Update CI and `prepare-source.sh` to use the repository root and bootstrap only
   external dependencies.
5. Add migration tests that reject source drift, first-party patch application in CI,
   unresolved LFS pointers, nested Git repositories, and accidental build outputs.
6. Run the full macOS/Xcode build, packaging/linkage checks, agent tests, and native-save
   structural tests.
7. Install the resulting IPA and execute the provider, lifecycle, rendering, memory,
   Python, codec, and live-agent device matrix. Track the Scene partial-write failure as
   a separate defect.

## Assumptions and exceptions

- The workflow is the source of truth where narrative documentation differs.
- “Complete source” means Blender and GhostBlender source are tracked, while large
  precompiled dependency bundles remain externally fetched at exact pins.
- No naming-convention Markdown file or `AGENTS.md` was present. The requested uppercase
  audit filename is therefore used without exception.
- CI has intentionally not been changed during this blocked run. Pointing it at an absent
  source tree would break the currently working build and violate the preservation
  requirement.
