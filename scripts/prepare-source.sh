#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_DIR="$(dirname "$SCRIPT_DIR")"
IOS_PATCH="$HARNESS_DIR/patches/ios-5.2-m4-full.patch"
GEOMETRY_PATCH="$HARNESS_DIR/patches/ios-live-view-geometry.patch"
NATIVE_FILES_PATCH="$HARNESS_DIR/patches/ios-native-files.patch"
RUNTIME_LINKAGE_PATCH="$HARNESS_DIR/patches/ios-runtime-linkage.patch"
FILES_LIFECYCLE_PATCH="$HARNESS_DIR/patches/ios-files-lifecycle-v2.patch"
PASS_CACHE_DIAGNOSTICS_PATCH="$HARNESS_DIR/patches/ios-pass-cache-diagnostics.patch"
MTKVIEW_DIAGNOSTICS_PATCH="$HARNESS_DIR/patches/ios-demand-driven-mtkview-diagnostics.patch"
CODEC_TRANSFORM="$HARNESS_DIR/scripts/apply-ios-codec-frameworks.py"

SOURCE_DIR="${1:-$PWD/work/blender}"
UPSTREAM_URL="${BLENDER_UPSTREAM_URL:-https://github.com/salmazov/blender-ios.git}"
PINNED_BLENDER_REF="2bc556e58e82eb3a801895f2cb1881c0267e5cd5"
PINNED_IOS_LIB_REF="393201c7c8525941553f6a96e19b909d6b3bfc4f"
PINNED_MACOS_LIB_REF="a3e20428fb0ab2231903608cdca90301e130dbfc"
BLENDER_REF="${BLENDER_REF:-$PINNED_BLENDER_REF}"
BUNDLE_ID="${BUNDLE_ID:-com.gorillafkngorgeous.blenderipad52}"

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "This build must run on an Apple-silicon macOS host." >&2
  exit 1
fi

for command in git git-lfs cmake xcodebuild plutil brew; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "Missing required command: $command" >&2
    exit 1
  fi
done

# The build-machine CPython 3.13.13 needs macOS libffi headers for its native _ctypes
# module. Keep this strictly a host-tool dependency: do not export CPPFLAGS, LDFLAGS,
# PKG_CONFIG_PATH, or any other Homebrew path into the workflow environment. Homebrew's
# libffi is keg-only, so force-link its headers and pkg-config metadata into the ordinary
# Homebrew prefix where the later host configure can discover it. The iOS CPython build
# disables pkg-config and supplies its own iOS LIBFFI_CFLAGS/LIBFFI_LIBS explicitly.
if ! brew list --versions libffi >/dev/null 2>&1; then
  brew install libffi
fi
host_libffi_prefix="$(brew --prefix libffi)"
brew link --overwrite --force libffi >/dev/null
brew_prefix="$(brew --prefix)"
test -f "$host_libffi_prefix/include/ffi.h"
test -e "$brew_prefix/include/ffi.h"
test -e "$brew_prefix/lib/pkgconfig/libffi.pc"
echo "Host-only libffi headers prepared at $host_libffi_prefix"

# cibuildwheel deliberately sanitizes iOS build environments so macOS/Homebrew libraries
# cannot leak into target wheels. zstandard's isolated build installs CFFI, whose setup.py
# needs ffi.h and resolves -lffi itself. Give every iOS wheel sandbox the already-planned
# target libffi location. These paths do not need to exist yet; external_ffi is built before
# NumPy/zstandard are invoked. Keep all iOS deployment-target state inside the iOS wheel
# sandbox: exporting IPHONEOS_DEPLOYMENT_TARGET through GITHUB_ENV contaminates native host
# CPython, while cibuildwheel 4.2 still derives zstandard's filename from its iOS baseline.
# A repair hook makes the wheel tag truthful; the workflow then verifies the extracted
# Mach-O directly before it can enter the Blender app bundle.
if [[ -n "${GITHUB_ENV:-}" && -n "${GITHUB_WORKSPACE:-}" ]]; then
  ios_libffi_root="$GITHUB_WORKSPACE/work/deps-ios-bootstrap/Release/ffi"
  printf '%s\n' \
    "CIBW_ENVIRONMENT_IOS=RUNNER_OS=macOS RUNNER_ARCH=ARM64 INSTALL_OPENBLAS=false IPHONEOS_DEPLOYMENT_TARGET=26.0 CFLAGS='-I${ios_libffi_root}/include' LDFLAGS='-L${ios_libffi_root}/lib'" \
    "CIBW_CONFIG_SETTINGS=--global-option=--no-cffi-backend" \
    "CIBW_REPAIR_WHEEL_COMMAND_IOS=python ${GITHUB_WORKSPACE}/scripts/retag-ios-wheel.py {wheel} {dest_dir}" \
    >> "$GITHUB_ENV"
  echo "Configured iOS-only cibuildwheel libffi search paths: $ios_libffi_root"
  echo "Configured zstandard cibuildwheel to use its native CPython C backend only"
  echo "Configured deterministic iOS 26 wheel retag repair"
fi

if [[ ! -f "$IOS_PATCH" ]]; then
  echo "Missing Blender 5.2 iPad compatibility patch: $IOS_PATCH" >&2
  exit 1
fi
if [[ ! -f "$GEOMETRY_PATCH" ]]; then
  echo "Missing iOS live-view geometry patch: $GEOMETRY_PATCH" >&2
  exit 1
fi
if [[ ! -f "$NATIVE_FILES_PATCH" ]]; then
  echo "Missing iOS native Files patch: $NATIVE_FILES_PATCH" >&2
  exit 1
fi
if [[ ! -f "$RUNTIME_LINKAGE_PATCH" ]]; then
  echo "Missing iOS runtime linkage patch: $RUNTIME_LINKAGE_PATCH" >&2
  exit 1
fi
if [[ ! -f "$FILES_LIFECYCLE_PATCH" ]]; then
  echo "Missing iOS Files lifecycle patch: $FILES_LIFECYCLE_PATCH" >&2
  exit 1
fi
if [[ ! -f "$PASS_CACHE_DIAGNOSTICS_PATCH" ]]; then
  echo "Missing GPU pass-cache diagnostic patch: $PASS_CACHE_DIAGNOSTICS_PATCH" >&2
  exit 1
fi
if [[ ! -f "$MTKVIEW_DIAGNOSTICS_PATCH" ]]; then
  echo "Missing demand-driven MTKView diagnostic patch: $MTKVIEW_DIAGNOSTICS_PATCH" >&2
  exit 1
fi
if [[ ! -f "$CODEC_TRANSFORM" ]]; then
  echo "Missing iOS codec framework transform: $CODEC_TRANSFORM" >&2
  exit 1
fi
if [[ -e "$SOURCE_DIR" ]]; then
  echo "Source destination already exists: $SOURCE_DIR" >&2
  exit 1
fi
if [[ "$BLENDER_REF" != "$PINNED_BLENDER_REF" ]]; then
  echo "BLENDER_REF must match the audited Blender 5.2 iOS revision." >&2
  exit 1
fi

mkdir -p "$(dirname "$SOURCE_DIR")"

GIT_LFS_SKIP_SMUDGE=1 git clone \
  --filter=blob:none \
  --no-checkout \
  --single-branch \
  --branch ios-new \
  --depth 1 \
  "$UPSTREAM_URL" \
  "$SOURCE_DIR"

git -C "$SOURCE_DIR" lfs install --local --skip-smudge
git -C "$SOURCE_DIR" fetch --no-tags --depth 1 origin "$BLENDER_REF"

actual_blender_ref="$(git -C "$SOURCE_DIR" rev-parse FETCH_HEAD)"
if [[ "$actual_blender_ref" != "$BLENDER_REF" ]]; then
  echo "Pinned Blender 5.2 revision mismatch: $actual_blender_ref" >&2
  exit 1
fi

git -C "$SOURCE_DIR" checkout --detach "$actual_blender_ref"
git -C "$SOURCE_DIR" lfs pull origin

# These submodules opt out of ordinary updates. --checkout is therefore required.
GIT_LFS_SKIP_SMUDGE=0 git -C "$SOURCE_DIR" submodule update \
  --init \
  --checkout \
  --depth 1 \
  lib/ios_arm64 \
  lib/macos_arm64

actual_ios_lib_ref="$(git -C "$SOURCE_DIR/lib/ios_arm64" rev-parse HEAD)"
actual_macos_lib_ref="$(git -C "$SOURCE_DIR/lib/macos_arm64" rev-parse HEAD)"
if [[ "$actual_ios_lib_ref" != "$PINNED_IOS_LIB_REF" ]]; then
  echo "Pinned iOS library revision mismatch: $actual_ios_lib_ref" >&2
  exit 1
fi
if [[ "$actual_macos_lib_ref" != "$PINNED_MACOS_LIB_REF" ]]; then
  echo "Pinned macOS library revision mismatch: $actual_macos_lib_ref" >&2
  exit 1
fi

git -C "$SOURCE_DIR/lib/ios_arm64" lfs pull
git -C "$SOURCE_DIR/lib/macos_arm64" lfs pull

# Apply only the audited compatibility delta. The old 5.1 input patch is deliberately not
# applied wholesale because 5.2 has newer keyboard, Pencil, pointer, and GCMouse code.
git -C "$SOURCE_DIR" apply --check "$IOS_PATCH"
git -C "$SOURCE_DIR" apply "$IOS_PATCH"
git -C "$SOURCE_DIR" apply --check "$GEOMETRY_PATCH"
git -C "$SOURCE_DIR" apply "$GEOMETRY_PATCH"
git -C "$SOURCE_DIR" apply --check "$NATIVE_FILES_PATCH"
git -C "$SOURCE_DIR" apply "$NATIVE_FILES_PATCH"
git -C "$SOURCE_DIR" apply --check "$RUNTIME_LINKAGE_PATCH"
git -C "$SOURCE_DIR" apply "$RUNTIME_LINKAGE_PATCH"
git -C "$SOURCE_DIR" apply --check "$FILES_LIFECYCLE_PATCH"
git -C "$SOURCE_DIR" apply "$FILES_LIFECYCLE_PATCH"
python3 "$CODEC_TRANSFORM" "$SOURCE_DIR"
git -C "$SOURCE_DIR" apply --check "$PASS_CACHE_DIAGNOSTICS_PATCH"
git -C "$SOURCE_DIR" apply "$PASS_CACHE_DIAGNOSTICS_PATCH"
git -C "$SOURCE_DIR" apply --check "$MTKVIEW_DIAGNOSTICS_PATCH"
git -C "$SOURCE_DIR" apply "$MTKVIEW_DIAGNOSTICS_PATCH"
python3 "$HARNESS_DIR/scripts/apply-ios-agent-bridge.py" "$SOURCE_DIR"
git -C "$SOURCE_DIR" diff --check

version_header="$SOURCE_DIR/source/blender/blenkernel/BKE_blender_version.h"
input_system="$SOURCE_DIR/intern/ghost/intern/GHOST_SystemIOS.mm"
input_window_header="$SOURCE_DIR/intern/ghost/intern/GHOST_WindowIOS.hh"
input_window="$SOURCE_DIR/intern/ghost/intern/GHOST_WindowIOS.mm"
native_files_header="$SOURCE_DIR/intern/ghost/intern/GHOST_FilePickerIOS.hh"
native_files="$SOURCE_DIR/intern/ghost/intern/GHOST_FilePickerIOS.mm"
platform_apple="$SOURCE_DIR/build_files/cmake/platform/platform_apple.cmake"
info_plist="$SOURCE_DIR/release/ios/Blender.app/Info.plist"
bundle_script="$SOURCE_DIR/release/ios/scripts/copy_bundle_data.sh"
python_compat_header="$SOURCE_DIR/source/blender/python/generic/python_compat.hh"
python_compat_source="$SOURCE_DIR/source/blender/python/generic/python_compat.cc"
python_interface="$SOURCE_DIR/source/blender/python/intern/bpy_interface.cc"
draco_dependency_cmake="$SOURCE_DIR/build_files/build_environment/cmake/draco.cmake"
meshopt_dependency_cmake="$SOURCE_DIR/build_files/build_environment/cmake/meshoptimizer.cmake"
draco_bridge_cmake="$SOURCE_DIR/intern/draco_bridge/CMakeLists.txt"
meshopt_bridge_cmake="$SOURCE_DIR/intern/meshoptimizer_bridge/CMakeLists.txt"
gltf_library="$SOURCE_DIR/scripts/addons_core/io_scene_gltf2/io/com/library.py"
usd_compat_sources=(
  "$SOURCE_DIR/source/blender/io/usd/intern/usd_capi_export.cc"
  "$SOURCE_DIR/source/blender/io/usd/intern/usd_reader_utils.cc"
  "$SOURCE_DIR/source/blender/io/usd/intern/usd_writer_abstract.cc"
)

grep -Eq '^#define BLENDER_VERSION +502$' "$version_header"
grep -Eq '^#define BLENDER_VERSION_PATCH +0$' "$version_header"
grep -Eq '^#define BLENDER_VERSION_CYCLE +release$' "$version_header"
test "$(grep -Fc '#if PY_VERSION_HEX >= 0x030d0000' "$python_compat_header")" -eq 1
grep -Fq 'The declaration must have C linkage' "$python_compat_header"
grep -Fq 'extern "C"' "$python_compat_header"
test "$(grep -Fc '#if PY_VERSION_HEX >= 0x030e0000' "$python_compat_source")" -eq 1
for usd_compat_source in "${usd_compat_sources[@]}"; do
  test "$(grep -Fc '#if PXR_VERSION >= 2505' "$usd_compat_source")" -eq 3
done
grep -Fq 'GHOST_SystemIOS::setPointerButtonState' "$input_system"
grep -Fq 'ghost_ios_live_view_bounds' "$input_system"
grep -Fq 'const CGRect bounds = ghost_ios_live_view_bounds();' "$input_system"
grep -Fq 'void viewGeometryDidChange();' "$input_window_header"
grep -Fq 'expectedDrawableSize' "$input_window"
grep -Fq '_view.drawableSize = expectedDrawableSize;' "$input_window"
grep -Fq '[m_uiview_controller loadViewIfNeeded];' "$input_window"
grep -Fq 'm_metalView.frame = rootWindow.bounds;' "$input_window"
grep -Fq 'return m_metalView.bounds.size;' "$input_window"
grep -Fq 'BlenderMetalRedraw.log' "$input_system"
grep -Fq 'ghost_ios_log_metal_redraw(@"draw_enter", MTKView);' "$input_system"
grep -Fq '_view.enableSetNeedsDisplay = YES;' "$input_window"
grep -Fq '_view.paused = YES;' "$input_window"
grep -Fq 'updateLink.requiresContinuousUpdates = NO;' "$input_window"
grep -Fq 'ghost_ios_log_metal_redraw_request(@"blender_request", m_metalView);' "$input_window"
python3 - "$native_files" <<'PY'
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_text(encoding="utf-8")
open_initializer = """initForOpeningContentTypes:contentTypes
                                                                           asCopy:YES"""
save_initializer = """initForExportingURLs:@[ saveExportURL ]
                                                                     asCopy:NO"""
assert source.count(open_initializer) == 1, "Open must use the controlled UIKit import mode"
assert source.count(save_initializer) == 1, "Save As must use the writable move/export mode"
assert "initForOpeningContentTypes:@[ UTTypeFolder ]" not in source, \
    "Save still uses an open/select folder picker"
assert "controller.documentPickerMode == UIDocumentPickerModeOpen" in source, \
    "Export callbacks could append the filename twice"
assert "saveFilename = nil;" in source, \
    "The obsolete folder-save filename alert is still active for export"
assert "controlled-import=yes final-path=%@" in source, \
    "Open could leave Blender working from UIKit's temporary Inbox"
scope_helper_start = source.index("static NSString *securityScopeDiagnostic(NSURL *url)")
scope_helper_end = source.index("#pragma mark - Security-Scoped URL Storage", scope_helper_start)
scope_helper = source[scope_helper_start:scope_helper_end]
assert scope_helper.count("startAccessingSecurityScopedResource") == 1, \
    "Security-scope diagnostic must start access exactly once"
assert scope_helper.count("stopAccessingSecurityScopedResource") == 1, \
    "Security-scope diagnostic must stop access exactly once"
assert "if (scopeStarted)" in scope_helper, \
    "Security-scope diagnostic must only stop access after a successful start"
PY
grep -Fq 'delegate.ghostWindow = originWindow;' "$native_files"
grep -Fq '[self deliverResultURL:url];' "$native_files"
grep -Fq 'window->needsDisplayUpdate();' "$native_files"
grep -Fq 'BlenderFiles.log' "$native_files"
grep -Fq 'GHOST_ios_logFileEvent' "$native_files_header"
grep -Fq 'FILES_LIFECYCLE_V2' "$input_system"
grep -Fq 's_pendingOpenURLs' "$input_system"
grep -Fq 'UIApplicationLaunchOptionsURLKey' "$input_system"
grep -Fq 'applicationDidBecomeActive' "$input_system"
grep -Fq 'UIApplicationStateActive' "$input_system"
grep -Fq 'system->notifyExternalEventProcessed();' "$input_system"
grep -Fq 'window->needsDisplayUpdate();' "$input_system"
grep -Fq 'shouldReceiveTouch:(UITouch *)touch' "$input_window"
grep -Fq 'rootWindow.windowLevel = UIWindowLevelNormal;' "$input_window"
grep -Fq 'delegate.window = rootWindow;' "$input_window"
if grep -Fq 'rootWindow.windowLevel = UIWindowLevelAlert;' "$input_window"; then
  echo "The Blender content window would still run at alert level." >&2
  exit 1
fi
grep -Fq '&config.executable, BKE_appdir_program_path()' "$python_interface"
if grep -Fxq '    [url startAccessingSecurityScopedResource];' "$native_files"; then
  echo "The native Files delegate would leak an unbalanced security-scope access." >&2
  exit 1
fi
if grep -Fq 'ghost_ios_window_scene_bounds' "$input_window"; then
  echo "Fullscreen window sizing still depends on scene effective geometry." >&2
  exit 1
fi
if grep -Fq '[m_uiview_controller viewDidLoad];' "$input_window"; then
  echo "The UIKit view lifecycle is still being invoked manually before attachment." >&2
  exit 1
fi
grep -Fq '  add_bundled_libraries(osl/lib)' "$platform_apple"
grep -Fxq 'add_bundled_libraries(openjph/lib)' "$platform_apple"
if grep -Eq '^add_bundled_libraries\(osl/lib\)' "$platform_apple"; then
  echo "Disabled OSL runtime libraries would still be copied into the iOS bundle." >&2
  exit 1
fi
if grep -Eq '^  add_bundled_libraries\(openjph/lib\)' "$platform_apple"; then
  echo "OpenJPH would disappear from the native cross-tools runtime when OSL is disabled." >&2
  exit 1
fi
test "$(grep -Fc -- '- (void)pushIndirectPointerCursorEvent' "$input_window")" -eq 1
method_line="$(grep -n -m1 -- '- (void)pushIndirectPointerCursorEvent' "$input_window" | cut -d: -f1)"
generator_line="$(grep -n -m1 -- '- (void)generateUserInputEvents' "$input_window" | cut -d: -f1)"
test "$method_line" -lt "$generator_line"
grep -Fq 'physical_memory >= (12ull * 1024ull * 1024ull * 1024ull)' \
  "$SOURCE_DIR/source/blender/gpu/metal/mtl_backend.mm"
test -d "$SOURCE_DIR/scripts/addons_core/bl_pkg"
if grep -Fq 'Removing bl_pkg addon' "$bundle_script"; then
  echo "The extension manager would be removed from the app bundle." >&2
  exit 1
fi
grep -Fq 'CFBundlePackageType -string FMWK' "$bundle_script"
grep -Fq 'lib${bridge_name}.fwork' "$bundle_script"
grep -Fq -- '-DBUILD_SHARED_LIBS=OFF' "$draco_dependency_cmake"
grep -Fq -- '-DMESHOPT_BUILD_SHARED_LIBS=OFF' "$meshopt_dependency_cmake"
grep -Fq 'add_library(bf_intern_draco_bridge SHARED' "$draco_bridge_cmake"
grep -Fq 'add_library(bf_intern_meshopt_bridge SHARED' "$meshopt_bridge_cmake"
grep -Fq "'ios': 'lib{}.fwork'.format(lib_name)" "$gltf_library"
grep -Fq 'framework marker is not inside an app bundle' "$gltf_library"
/usr/libexec/PlistBuddy -c 'Print :UIApplicationSupportsIndirectInputEvents' "$info_plist" | \
  grep -Fxq true
/usr/libexec/PlistBuddy -c 'Print :UILaunchStoryboardName' "$info_plist" | grep -Fxq Main
if /usr/libexec/PlistBuddy -c 'Print :UIMainStoryboardFile' "$info_plist" >/dev/null 2>&1; then
  echo "Info.plist would still create a second storyboard-owned application window." >&2
  exit 1
fi
if /usr/libexec/PlistBuddy -c 'Print :UISupportsDocumentBrowser' "$info_plist" >/dev/null 2>&1; then
  echo "Info.plist would still falsely claim a UIDocumentBrowserViewController root." >&2
  exit 1
fi
/usr/libexec/PlistBuddy -c 'Print :UIFileSharingEnabled' "$info_plist" | grep -Fxq true
/usr/libexec/PlistBuddy -c 'Print :LSSupportsOpeningDocumentsInPlace' "$info_plist" | \
  grep -Fxq true

if /usr/libexec/PlistBuddy -c 'Print :CFBundleDisplayName' "$info_plist" >/dev/null 2>&1; then
  /usr/libexec/PlistBuddy -c 'Set :CFBundleDisplayName Blender 5.2 iPad' "$info_plist"
else
  /usr/libexec/PlistBuddy -c 'Add :CFBundleDisplayName string Blender 5.2 iPad' "$info_plist"
fi

# Preserve the increased-memory entitlement in source. A separately packaged Signulous fallback
# strips it only from that copy; the M4/full-memory artifact remains unthrottled.
/usr/libexec/PlistBuddy \
  -c 'Print :com.apple.developer.kernel.increased-memory-limit' \
  "$SOURCE_DIR/release/ios/entitlements.plist" | grep -Fxq true

cat <<EOF
Prepared Blender 5.2 LTS iPad source.
Blender revision: $actual_blender_ref
iOS libraries: $actual_ios_lib_ref
macOS host libraries: $actual_macos_lib_ref
Bundle identifier: $BUNDLE_ID
Pass cache diagnostic patch SHA-256: $(shasum -a 256 "$PASS_CACHE_DIAGNOSTICS_PATCH" | awk '{print $1}')
Demand-driven MTKView diagnostic patch SHA-256: $(shasum -a 256 "$MTKVIEW_DIAGNOSTICS_PATCH" | awk '{print $1}')
Compatibility patch SHA-256: $(shasum -a 256 "$IOS_PATCH" | awk '{print $1}')
Live-view geometry patch SHA-256: $(shasum -a 256 "$GEOMETRY_PATCH" | awk '{print $1}')
Native Files patch SHA-256: $(shasum -a 256 "$NATIVE_FILES_PATCH" | awk '{print $1}')
iOS runtime linkage patch SHA-256: $(shasum -a 256 "$RUNTIME_LINKAGE_PATCH" | awk '{print $1}')
iOS Files lifecycle patch SHA-256: $(shasum -a 256 "$FILES_LIFECYCLE_PATCH" | awk '{print $1}')
Codec framework transform SHA-256: $(shasum -a 256 "$CODEC_TRANSFORM" | awk '{print $1}')
EOF