#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Private single-owner GhostBlender MCP mode.

ChatGPT connects through a capability URL terminated by Caddy. Caddy hides the
real /mcp endpoint behind the long AGENT_TOKEN path. The relay therefore does
not run an interactive OAuth flow for MCP calls. Device authentication remains
unchanged: /device/exchange still requires DEVICE_TOKEN.

This is intentionally a private-development mode, not a multi-user auth system.
"""
import json
import os
from pathlib import Path

import server
from chat import ChatWorker


# Developer-mode apps configured as "No Authentication" must not advertise an
# OAuth requirement on individual tools. The capability URL is the access gate.
for tool in server.TOOLS:
    tool.pop('securitySchemes', None)
    tool.pop('_meta', None)


_original_authorized = server.Handler.authorized


def _simple_authorized(self, device=False):
    # Keep iPad -> relay pairing authenticated exactly as before. ChatGPT -> MCP
    # has already passed Caddy's unguessable capability path before it reaches
    # this internal-only container endpoint.
    if device:
        return _original_authorized(self, device=True)
    return True


server.Handler.authorized = _simple_authorized


if __name__ == '__main__':
    os.umask(0o077)
    db_path = os.environ.get('DATABASE_PATH', '/data/ghostblender.sqlite3')
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    port = int(os.environ.get('PORT', '8080'))
    app = server.App(
        db_path,
        os.environ['PUBLIC_ORIGIN'],
        os.environ.get('DEVICE_ID', 'ipad'),
        os.environ['DEVICE_TOKEN'],
        os.environ['AGENT_TOKEN'],
        os.environ.get('OAUTH_CLIENT_ID', 'ghostblender'),
        os.environ['OAUTH_CLIENT_SECRET'],
        os.environ['OWNER_KEY'],
        json.loads(os.environ['OAUTH_REDIRECT_URIS']),
    )
    httpd = server.Server(
        (os.environ.get('BIND_HOST', '127.0.0.1'), port),
        app,
    )
    app.chat = ChatWorker(
        app.store,
        app.device_id,
        f'http://127.0.0.1:{port}/mcp',
    )
    app.chat.start()
    try:
        httpd.serve_forever()
    finally:
        app.chat.close()
        httpd.server_close()
