# GhostBlender canonical-source migration plan

Status: **BLOCKED before source import** (12 September 2026)

## Goal

Make this repository contain the exact Blender 5.2 source currently reconstructed by
the build, based on upstream commit
`2bc556e58e82eb3a801895f2cb1881c0267e5cd5`, with the GhostBlender/iPad changes
applied as ordinary tracked source changes.

## Short migration sequence

1. Obtain and cryptographically verify the pinned `salmazov/blender-ios` checkout,
   including Git LFS objects, while leaving this repository's existing history intact.
2. Import the upstream tracked source at the repository root, but do **not** vendor the
   large `lib/ios_arm64` and `lib/macos_arm64` dependency submodules or generated build
   products.
3. Apply the currently active Blender patches and source-transform scripts in their
   existing order. Commit the resulting Blender source changes directly and retain only
   the NumPy patches, which apply to an externally downloaded dependency.
4. Change `scripts/prepare-source.sh` into a checked-in-source verifier/dependency
   bootstrap and change CI's `SOURCE_DIR` to the checkout root. Remove the fresh Blender
   clone and all first-party patch application from CI.
5. Update source manifests, path filters, documentation, and tests. Compare the imported
   result byte-for-byte with a tree produced by the old reconstruction workflow before
   accepting the migration.

## Required verification gate

The migration must not infer, approximate, upgrade, or replace the upstream source. The
following inputs are authoritative:

| Input | Required revision/version |
|---|---|
| Blender iOS source | `2bc556e58e82eb3a801895f2cb1881c0267e5cd5` |
| iOS dependency bundle | `393201c7c8525941553f6a96e19b909d6b3bfc4f` |
| macOS host-tool bundle | `a3e20428fb0ab2231903608cdca90301e130dbfc` |
| CPython | 3.13.13 |
| NumPy | 2.3.4 |
| Zstandard | 0.25.0 |

Before importing, verify `git rev-parse HEAD`, resolve every Git LFS pointer, and record
the upstream tree ID. After applying the current migration delta, run `git diff --check`
and compare a deterministic file manifest against the output of the legacy preparation
pipeline. Only then should CI be switched to build from the repository root.

## Current blocker

This execution environment rejects outbound GitHub HTTPS traffic with `CONNECT tunnel
failed, response 403`. No local copy of the pinned Blender checkout exists in the
container. Consequently, the exact source cannot be imported or truthfully verified in
this run. Substituting another Blender release or reconstructing files from patch hunks
would violate the source pin and the requirement to preserve behavior.

The repository therefore remains on its legacy reconstruction workflow. No CI/source
switch should be made until the exact checkout is available, because doing so now would
make the build non-functional.

## Resume instructions

Provide network access to `github.com`, `codeload.github.com`, and the Git LFS object
host used by `salmazov/blender-ios`, or place a verified checkout at a local path. Resume
at step 1 above. Preserve the current branch history by importing the snapshot in new
commits; do not replace `.git`, reinitialize the repository, or force-push rewritten
history.
