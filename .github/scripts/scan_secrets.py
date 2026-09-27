#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Fail if tracked files contain secrets or deployment-identifying network data.

GHOSTpad is one public repository. This scan covers every file `git ls-files`
reports and looks for:

- capability URLs (the unguessable path segment is the credential)
- bearer/access tokens and JWTs
- provider credentials (OpenAI, Anthropic, GitHub, AWS, Google, Slack, private keys)
- device, agent and OAuth secrets assigned to their configuration names
- public (globally routable) IPv4/IPv6 addresses
- hostnames that embed an IP address (sslip.io, nip.io and similar)

Findings are printed redacted, so the CI log never repeats a secret. A finding
that is known to be public and harmless is allowed by an entry in
.github/secret-scan-allowlist.json. Entries hold the sha256 of the matched value,
never the value itself. Documentation and tests should use reserved synthetic
values instead: example.invalid hosts, 192.0.2.0/24, 198.51.100.0/24 or
203.0.113.0/24 addresses, and repeated-character or clearly fake tokens.
"""
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST = ROOT / '.github/secret-scan-allowlist.json'
MAX_BYTES = 2_000_000

TOKEN = r'[A-Za-z0-9_\-]'

PROVIDER_PATTERNS = [
    ('private-key', re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')),
    ('anthropic-key', re.compile(r'\bsk-ant-' + TOKEN + r'{20,}')),
    ('openai-key', re.compile(r'\bsk-(?!ant-)(?:proj-|svcacct-|admin-)?' + TOKEN + r'{20,}')),
    ('github-token', re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})')),
    ('aws-access-key', re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b')),
    ('google-api-key', re.compile(r'\bAIza' + TOKEN + r'{35}\b')),
    ('google-oauth-secret', re.compile(r'\bGOCSPX-' + TOKEN + r'{20,}')),
    ('slack-token', re.compile(r'\bxox[abposr]-[A-Za-z0-9-]{10,}')),
    ('service-account-key', re.compile(r'"private_key_id"\s*:\s*"[a-f0-9]{40}"')),
    ('jwt', re.compile(r'\beyJ' + TOKEN + r'{10,}\.eyJ' + TOKEN + r'{10,}\.' + TOKEN + r'{10,}')),
]

# Values are checked for randomness so that names, placeholders and
# repeated-character test values do not trip the scan.
BEARER = re.compile(r'(?i)\bbearer\s+([A-Za-z0-9._~+/\-]{20,}=*)')
TOKEN_FIELD = re.compile(
    r'(?i)["\']?\b(access_token|refresh_token|id_token|auth_token|api_key|apikey|'
    r'client_secret|secret_key|password)\b["\']?\s*[:=]\s*["\']?([A-Za-z0-9._~+/\-]{16,}=*)')
NAMED_SECRET = re.compile(
    r'\b(DEVICE_TOKEN|AGENT_TOKEN|OAUTH_CLIENT_SECRET|OWNER_KEY|[A-Z0-9_]*API_KEY|'
    r'[A-Z0-9_]*SECRET[A-Z0-9_]*|[A-Z0-9_]*_TOKEN)\s*[:=]\s*["\']?([A-Za-z0-9._~+/\-]{16,}=*)')
CAPABILITY_URL = re.compile(r'\bhttps?://[^\s"\'<>()]+?/mcp/(?:p/)?(' + TOKEN + r'{16,})')
IPV4 = re.compile(r'(?<![\w.])((?:\d{1,3}\.){3}\d{1,3})(?:/\d{1,2})?(?![\w]|\.\d)')
IPV6 = re.compile(r'(?<![\w:.])([0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7})(?![\w:])')
IP_HOST = re.compile(
    r'\b((?:\d{1,3}[-.]){3}\d{1,3})\.(sslip\.io|nip\.io|xip\.io|traefik\.me|localtest\.me)\b',
    re.IGNORECASE)


def entropy(value):
    counts = {c: value.count(c) for c in set(value)}
    return -sum(n / len(value) * math.log2(n / len(value)) for n in counts.values())


def looks_random(value):
    """True for credential-like strings; false for words, placeholders and fakes."""
    core = value.rstrip('=')
    if len(core) < 16 or any(ch in core for ch in '$<>{}%'):
        return False
    if re.fullmatch(r'[A-Za-z]+(?:[_\-.][A-Za-z]+)*', core):
        return False  # identifiers and placeholder phrases such as YOUR_TOKEN_HERE
    classes = sum(bool(re.search(p, core)) for p in (r'[a-z]', r'[A-Z]', r'[0-9]'))
    return classes >= 2 and entropy(core) >= 3.5


def is_public(address):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_global and not ip.is_multicast


def redact(value):
    return f'{value[:4]}…({len(value)} chars, sha256 {sha(value)[:12]})'


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def scan_text(text):
    """Yield (line_number, rule, value) for every finding in text."""
    for number, line in enumerate(text.splitlines(), 1):
        for rule, pattern in PROVIDER_PATTERNS:
            for match in pattern.finditer(line):
                yield number, rule, match.group(0)
        for match in BEARER.finditer(line):
            if looks_random(match.group(1)):
                yield number, 'bearer-token', match.group(1)
        for match in TOKEN_FIELD.finditer(line):
            if looks_random(match.group(2)):
                yield number, 'token-field', match.group(2)
        for match in NAMED_SECRET.finditer(line):
            if looks_random(match.group(2)):
                yield number, 'named-secret', match.group(2)
        for match in CAPABILITY_URL.finditer(line):
            if looks_random(match.group(1)):
                yield number, 'capability-url', match.group(0)
        for match in IP_HOST.finditer(line):
            address = re.sub(r'-', '.', match.group(1))
            if is_public(address):
                yield number, 'ip-hostname', match.group(0)
        for match in IPV4.finditer(line):
            if is_public(match.group(1)):
                yield number, 'public-ip', match.group(0)
        for match in IPV6.finditer(line):
            candidate = match.group(1)
            if candidate.count(':') >= 2 and is_public(candidate):
                yield number, 'public-ip', candidate


def tracked_files():
    output = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, check=True,
                            capture_output=True).stdout
    return [ROOT / name for name in output.decode().split('\0') if name]


def load_allowlist():
    if not ALLOWLIST.exists():
        return set()
    entries = json.loads(ALLOWLIST.read_text(encoding='utf-8'))
    allowed = set()
    for entry in entries:
        if set(entry) != {'path', 'rule', 'sha256', 'reason'} or not entry['reason'].strip():
            raise SystemExit(f'Invalid allowlist entry (needs path, rule, sha256, reason): {entry}')
        allowed.add((entry['path'], entry['rule'], entry['sha256']))
    return allowed


def main():
    allowed = load_allowlist()
    findings = []
    for path in tracked_files():
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            continue
        data = path.read_bytes()
        if b'\0' in data[:8192]:
            continue  # binary
        relative = path.relative_to(ROOT).as_posix()
        for number, rule, value in scan_text(data.decode('utf-8', errors='replace')):
            if (relative, rule, sha(value)) not in allowed:
                findings.append((relative, number, rule, value))
    for relative, number, rule, value in findings:
        print(f'{relative}:{number}: {rule}: {redact(value)}')
    if findings:
        print(f'\n{len(findings)} possible secret(s) or deployment identifier(s) found.')
        print('Remove them, replace with reserved synthetic examples, or (only for public, '
              'harmless values) add an allowlist entry with the sha256 shown.')
        return 1
    print('Secret scan passed.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
