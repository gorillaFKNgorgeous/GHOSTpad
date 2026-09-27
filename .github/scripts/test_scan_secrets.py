"""Tests for scan_secrets.py.

Positive cases are assembled at runtime from harmless pieces, so this file never
contains a credential-shaped literal of its own.
"""
import random
import string
import unittest

import scan_secrets as scan


def rules(text):
    return {rule for _, rule, _ in scan.scan_text(text)}


def fake_token(length=43, seed=7):
    rng = random.Random(seed)
    return ''.join(rng.choice(string.ascii_letters + string.digits + '_-') for _ in range(length))


# Routable addresses are joined from parts at runtime so the repository itself
# never contains one.
def public_ipv4():
    return '.'.join(['8', '34', '120', '17'])


def public_ipv6():
    return ':'.join(['2001', '4860', '4860', '', '8888'])


class PositiveTests(unittest.TestCase):
    def test_capability_urls(self):
        host = 'relay.example.invalid'
        self.assertIn('capability-url', rules(f'https://{host}/mcp/{fake_token()}'))
        self.assertIn('capability-url', rules(f'url = "http://127.0.0.1:8080/mcp/p/{fake_token()}"'))

    def test_bearer_and_token_fields(self):
        self.assertIn('bearer-token', rules('Authorization: Bearer ' + fake_token()))
        self.assertIn('token-field', rules('{"access_token": "' + fake_token() + '"}'))
        self.assertIn('token-field', rules("refresh_token='" + fake_token() + "'"))
        jwt = 'eyJ' + fake_token(20) + '.eyJ' + fake_token(24, 2) + '.' + fake_token(30, 3)
        self.assertIn('jwt', rules(jwt))

    def test_provider_credentials(self):
        self.assertIn('openai-key', rules('sk-' + 'proj-' + fake_token(48)))
        self.assertIn('anthropic-key', rules('sk-' + 'ant-' + 'api03-' + fake_token(80)))
        self.assertIn('github-token', rules('gh' + 'p_' + fake_token(36).replace('-', 'a').replace('_', 'b')))
        self.assertIn('aws-access-key', rules('AK' + 'IA' + 'Q3EGRZ7YH2LMN5VT'))
        self.assertIn('google-api-key', rules('AI' + 'za' + fake_token(35)))
        self.assertIn('private-key', rules('-----BEGIN ' + 'RSA PRIVATE KEY-----'))

    def test_device_agent_and_oauth_secrets(self):
        for name in ('DEVICE_TOKEN', 'AGENT_TOKEN', 'OAUTH_CLIENT_SECRET', 'OWNER_KEY', 'OPENAI_API_KEY'):
            with self.subTest(name=name):
                self.assertIn('named-secret', rules(f'{name}={fake_token()}'))
        self.assertIn('token-field', rules('client_secret: ' + fake_token()))

    def test_public_ips_and_ip_hostnames(self):
        address = public_ipv4()
        self.assertIn('public-ip', rules(f'relay at {address} is up'))
        self.assertIn('public-ip', rules(f'--source-ranges {address}/20'))
        self.assertIn('public-ip', rules(f'host {public_ipv6()} answered'))
        dashed = address.replace('.', '-')
        self.assertIn('ip-hostname', rules(f'PUBLIC_ORIGIN=https://{dashed}.sslip.io'))
        self.assertIn('ip-hostname', rules(f'https://{address}.nip.io/health'))


class NegativeTests(unittest.TestCase):
    """Examples the repository legitimately contains must not fail the build."""

    def test_reserved_and_private_addresses(self):
        for text in ('10.42.0.0/24', '192.168.1.20', '127.0.0.1:8080', '0.0.0.0', '172.16.5.4',
                     '192.0.2.10', '198.51.100.7', '203.0.113.99', '::1', 'fe80::1',
                     'https://203-0-113-10.sslip.io', 'https://192-168-0-2.nip.io'):
            with self.subTest(text=text):
                self.assertEqual(rules(text), set())

    def test_versions_times_and_hashes(self):
        for text in ('Blender 5.2.0 LTS', 'openai-codex==0.156.1', '2026-09-27 02:23:21 +0000',
                     'commit 4a2c008d424b13972b060ce7f6dbd430234fe33f',
                     'https://github.com/gorillaFKNgorgeous/-blender-ipad-M4/actions/runs/34229150516',
                     'sha256 9ca249850d7869b564ba311147e395ace73552cd34790363aca212cf00667571'):
            with self.subTest(text=text):
                self.assertEqual(rules(text), set())

    def test_placeholders_and_test_values(self):
        for text in ('"authorization": "AGENT_TOKEN_FROM_YOUR_SECRET_STORE"',
                     'DEVICE_TOKEN=$DEVICE_TOKEN', "AGENT=$(sed -n 's/^AGENT_TOKEN=//p' .env)",
                     "printf 'MCP_URL=%s/mcp/%s' \"$ORIGIN\" \"$AGENT\"",
                     "'Authorization': 'Bearer ' + 'a' * 40", 'OWNER_KEY=' + 'o' * 40,
                     'https://YOUR-RELAY-DOMAIN/mcp', 'https://example.invalid/secret-token/shot.png',
                     'os.environ["OAUTH_CLIENT_SECRET"]', 'token=AGENT_TOKEN_FROM_YOUR_SECRET_STORE'):
            with self.subTest(text=text):
                self.assertEqual(rules(text), set())

    def test_output_is_redacted(self):
        value = fake_token()
        self.assertNotIn(value[8:], scan.redact(value))


class AllowlistTests(unittest.TestCase):
    def test_allowlist_entries_are_hashes_with_reasons(self):
        allowed = scan.load_allowlist()
        for path, rule, digest in allowed:
            self.assertRegex(digest, r'^[a-f0-9]{64}$')


if __name__ == '__main__':
    unittest.main()
