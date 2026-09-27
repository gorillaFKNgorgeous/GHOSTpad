# Repository policy

GHOSTpad is **one public repository** during development. The relay, the Blender
runtime, the native transport, GHOSTroom and the build scripts all live here.
Do not create a separate service repository.

Because everything is public, deployment secrets and deployment-identifying
data never enter the tree. They live only in the relay's `.env` and `/data`
volume, on the iPad, and in the ChatGPT/Claude connector settings.

## Enforced by CI

`.github/workflows/secret-scan.yml` runs on every push and pull request, with no
path filter. `.github/scripts/scan_secrets.py` scans every tracked text file
and fails on:

| rule | what it catches |
| --- | --- |
| `capability-url` | `http(s)://…/mcp/<token>` and `/mcp/p/<token>`: the path segment is the credential |
| `bearer-token`, `token-field`, `jwt` | `Bearer <token>`, `access_token`/`refresh_token`/`client_secret`/`api_key`/`password` values, JWTs |
| provider rules | OpenAI, Anthropic, GitHub, AWS, Google API/OAuth, Slack keys; PEM private keys; service-account key ids |
| `named-secret` | random-looking values assigned to `DEVICE_TOKEN`, `AGENT_TOKEN`, `OAUTH_CLIENT_SECRET`, `OWNER_KEY`, `*_API_KEY`, `*SECRET*`, `*_TOKEN` |
| `public-ip` | globally routable IPv4/IPv6 addresses and CIDRs |
| `ip-hostname` | hostnames embedding a public IP (`a-b-c-d.sslip.io`, `a.b.c.d.nip.io`, xip.io, …) |

Values are only reported when they look random (mixed character classes and
high entropy). Names, `$VARIABLES`, `YOUR_…` placeholders and repeated-character
test values such as `'a' * 40` pass. Findings are printed redacted.

## Writing examples

Use reserved, obviously synthetic values:

- hosts: `relay.example`, `relay.example.invalid`, `YOUR-RELAY-DOMAIN`
- addresses: `192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24` (RFC 5737), private ranges
- IP hostnames: `203-0-113-10.sslip.io`
- tokens: repeated characters (`'d' * 40`), `…_FROM_YOUR_SECRET_STORE`, or values
  assembled at runtime in tests (see `.github/scripts/test_scan_secrets.py`)

## Allowlist

`.github/secret-scan-allowlist.json` is only for values that are public and
harmless, such as Google's published IAP source range in `deploy-gce.sh`. Each
entry records `path`, `rule`, the **sha256** of the matched value (never the
value) and a `reason`. The scanner prints the sha256 to use.
