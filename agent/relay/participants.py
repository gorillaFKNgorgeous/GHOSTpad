#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Manage relay participants. Run inside the relay container:

    docker-compose exec relay python participants.py add claude --kind agent --provider claude --name "Claude"
    docker-compose exec relay python participants.py list
    docker-compose exec relay python participants.py rotate claude
    docker-compose exec relay python participants.py revoke claude

`add` and `rotate` print the participant's MCP path exactly once. Append it to the
relay origin to form the connector URL. The capability in it is the participant's
credential: only its sha256 is stored, so a lost capability can only be rotated,
never recovered. Never paste it into GitHub, logs or chat.

Identity comes only from the capability. MCP clientInfo is shown in `list` as an
unverified label and never grants identity or authorization.
"""
import argparse
import os

from store import Store

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
commands = parser.add_subparsers(dest='command', required=True)
add = commands.add_parser('add')
add.add_argument('participant_id')
add.add_argument('--kind', default='agent', choices=('user', 'agent', 'ghostblender', 'relay', 'system'))
add.add_argument('--provider')
add.add_argument('--name', required=True)
for name in ('rotate', 'revoke'):
    commands.add_parser(name).add_argument('participant_id')
commands.add_parser('list')
args = parser.parse_args()

os.umask(0o077)
store = Store(os.environ.get('DATABASE_PATH', '/data/ghostblender.sqlite3'))
if args.command == 'add':
    capability = store.register_participant(args.participant_id, args.kind, args.name, args.provider)
    print('MCP path (shown once): /mcp/p/' + capability)
elif args.command == 'rotate':
    capability = store.rotate_capability(args.participant_id)
    print('New MCP path (shown once; the old one stops working now): /mcp/p/' + capability)
elif args.command == 'revoke':
    store.revoke_participant(args.participant_id)
    print('Revoked', args.participant_id)
else:
    for row in store.participants():
        state = 'revoked' if row['revoked'] else 'active'
        label = row['client_label_unverified'] or '-'
        print(f"{row['participant_id']:24} {row['kind']:12} {state:8} {row['display_name']}  "
              f"[client says, unverified: {label}]")
