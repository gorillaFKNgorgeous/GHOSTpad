# Live iPad render investigation — 2026-09-13 UTC

## Status
Investigation incomplete: the device stopped reporting during the first unchanged synchronous render. This is not yet a confirmed process termination; a blocked main thread also stops this bridge. No crash cause or fix has been established. Reconnect the app and retrieve the durable render-stage log before running another test.

## Device and baseline
- Live Ghostblender Simple connection; Blender 5.2.0 LTS, Python 3.13.13, Blender source hash 2bc556e58e82.
- That hash identifies the Blender base, not the complete patched build or installed signing profile.
- User device: M4 iPad Pro, 1 TB storage. Storage capacity is not process memory allowance.
- Imported file: blender-4.5-splash (1)-76DE4F01-F239-478A-A250-DB80EB1DA022.blend.
- File had no unsaved changes before testing. No scene settings, textures, geometry, camera, or original file were modified.
- Frame 85, CAM-Camera, BLENDER_EEVEE, METAL backend.
- Resolution 4000 × 2000 at 25% = 1000 × 500.
- EEVEE 128 samples, ray tracing enabled, shadow pool 512.
- Compositing/sequencer enabled; simplify disabled; persistent data disabled.
- 185 scene objects; 512 object datablocks, 267 meshes, 73 images across file.
- At least 13 loaded 4096 × 4096 image datablocks. No subdivision-surface modifiers found in the inspected object datablocks.
- Cycles device CPU is irrelevant to the active EEVEE render; changing it would not switch EEVEE to GPU.

## Measured observations
| Observation | Physical footprint bytes | Available process memory bytes |
|---|---:|---:|
| Initial heartbeat | 3399796872 | 3042654072 |
| Last heartbeat before synchronous render callback | 4392160592 | 2050290352 |

Both sum to approximately 6 GiB. This suggests a process budget around that level at these observations, not unrestricted access to the device's RAM. It does not establish signing entitlement state or prove jetsam. The second heartbeat precedes the scheduled render callback; it is NOT a measured render peak and its increase must not be attributed to rendering without further evidence.

## Test S01
Installed temporary render_init/pre/post/complete/cancel handlers and scheduled bpy.ops.render.render('EXEC_DEFAULT') through a main-thread Blender timer. Original settings were retained. Timer scheduling lets the bridge acknowledge the test before the blocking native call and avoids the bridge's 15-second Python execution deadline confounding a long render.

Each stage appends JSON with timestamp and native measured memory, flushes and fsyncs:
Library/Application Support/Blender/5.2/config/ghostbridge/render-investigation-2026-09-13/events.jsonl
(relative to the app container).

Arming job b7ec29bc4dd147c6b5c60ec19e0af091 completed. Boot c15f4498148442f7b691442b1d88f54e; scene 0fd4aabb1a5c41058f59fabee5801743.
Last heartbeat timestamp 1789317465.0761375. Subsequent execution returned device_offline_or_suspended; repeated status online=false. No returned render image or completed render-stage log has yet been retrieved.

Handlers are removed when the callback returns; process termination also removes them. An interrupted render may leave partial logs. These stage samples cannot capture allocations between callbacks.

## Source observations
Reviewed GHOSTpad main at 90df05088be46a318332fca1cddcce9dbe0826ab.
- agent/native/ghostbridge_transport.mm measures os_proc_available_memory and TASK_VM_INFO phys_footprint.
- agent/runtime/core.py runs Python on the main thread. Heartbeats become unavailable when synchronous native work blocks it; offline is not a crash verdict.
- patches/ios-5.2-m4-full.patch raises max_parallel_compilations when physical RAM exceeds 12 GiB. It considers thermal state at capabilities initialization but does not inspect current process allowance/headroom. This is a plausible memory-pressure contributor, not a proven root cause.
- Existing README distinguishes fallback and increased-memory IPA profiles. The installed profile has not been verified.
- No behavioral native patch was applied on speculation.

## Required continuation
1. Establish whether the app closed or remains rendering. If closed, reopen and retrieve events.jsonl before another render.
2. Obtain the matching iPad Analytics Blender .ips / JetsamEvent entry if present. Distinguish memory termination, watchdog, GPU fault, assertion, and invalid memory access.
3. Inspect installed build/signing evidence and process memory budget; do not infer entitlement effectiveness from IPA filename.
4. If S01 completed, capture the actual Render Result, then compare a repeat synchronous render and interactive render with identical settings.
5. If memory pressure is implicated, test one reversible change at a time (ray tracing, shadow allocation, texture working set) and record quality and memory effects. Preserve source textures and original project.
6. Add native render-stage/thread/GPU-context, allocation and cleanup telemetry in a separate diagnostic commit once the failure stage is known. Use actual headroom in future shader concurrency policy, with device validation.
7. A native fix requires a new IPA and repeat device tests. A successful reduced-quality render alone would not establish that interactive rendering is fixed.
