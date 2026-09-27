#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Boot an iPad simulator, run the GHOSTroom harness app and collect its results."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

BUNDLE = 'org.ghostpad.ghostroom-harness'


def simctl(*args, check=True, timeout=300):
    return subprocess.run(['xcrun', 'simctl', *args], check=check, capture_output=True, text=True,
                          timeout=timeout)


def pick_ipad():
    listing = json.loads(simctl('list', 'devices', 'available', '-j').stdout)
    best = None
    for runtime, devices in listing['devices'].items():
        if 'iOS' not in runtime:
            continue
        version = tuple(int(p) for p in runtime.rsplit('iOS-', 1)[-1].split('-') if p.isdigit())
        for device in devices:
            if 'iPad' in device['name']:
                score = (version, 'Pro' in device['name'], '13-inch' in device['name'])
                if best is None or score > best[0]:
                    best = (score, device['udid'], device['name'], runtime)
    if best is None:
        sys.exit('No available iPad simulator')
    print(f'Using {best[2]} ({best[3]})')
    return best[1]


def main(out):
    out = Path(out)
    app = out / 'GHOSTroomHarness.app'
    udid = pick_ipad()
    simctl('boot', udid, check=False)
    simctl('bootstatus', udid, '-b', timeout=600)
    simctl('install', udid, str(app))
    try:
        run = simctl('launch', '--console-pty', '--terminate-running-process', udid, BUNDLE, check=False,
                     timeout=180)
        output = run.stdout + run.stderr
    except subprocess.TimeoutExpired as exc:
        parts = [exc.stdout or b'', exc.stderr or b'']
        output = ''.join(p.decode(errors='replace') if isinstance(p, bytes) else p for p in parts)
        output += '\nHARNESS LAUNCH TIMEOUT'
    print(output)
    shots = out / 'screenshots'
    shots.mkdir(exist_ok=True)
    container = simctl('get_app_container', udid, BUNDLE, 'data', check=False).stdout.strip()
    if container:
        for png in (Path(container) / 'Documents').glob('*.png'):
            shutil.copy(png, shots / png.name)
    simctl('io', udid, 'screenshot', str(shots / '99-final-device.png'), check=False)
    conflicts = output.count('Unable to simultaneously satisfy constraints')
    print(f'Auto Layout conflicts: {conflicts}')
    passed = 'HARNESS RESULT failures=0' in output
    if not passed or conflicts:
        sys.exit('GHOSTroom simulator harness failed')
    print('GHOSTroom simulator harness passed')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'build/harness')
