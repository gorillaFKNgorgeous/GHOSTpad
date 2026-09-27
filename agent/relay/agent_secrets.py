# SPDX-License-Identifier: GPL-2.0-or-later
"""Provider credentials entered from GHOSTroom, kept only on the relay's private volume.

Values are never logged, never echoed back to any client and never written to
the ledger. Only the fact that a credential is set (and when) is observable.
"""
import json
import os
from pathlib import Path
import threading
import time


class AgentSecrets:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self):
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def get(self, key):
        with self._lock:
            value = self._read().get(key)
        return value.get('value') if isinstance(value, dict) else None

    def is_set(self, key):
        return bool(self.get(key))

    def set(self, key, value):
        with self._lock:
            data = self._read()
            if value:
                data[key] = {'value': value, 'set_at': time.time()}
            else:
                data.pop(key, None)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix('.tmp')
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as handle:
                json.dump(data, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
