#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""One-time ChatGPT account sign-in for the relay's embedded Codex worker."""

import os
import sys

from chat import prepare_codex_environment


def main():
    port = int(os.environ.get("PORT", "8080"))
    prepare_codex_environment(f"http://127.0.0.1:{port}/mcp")

    from openai_codex import Codex

    with Codex() as codex:
        try:
            account = codex.account()
        except Exception:
            account = None
        if account is not None and getattr(account, "account", None) is not None:
            print("Codex account session is already available.", flush=True)
            return 0

        login = codex.login_chatgpt_device_code()
        print("Open this URL in a browser:", login.verification_url, flush=True)
        print("Enter this one-time code:", login.user_code, flush=True)
        completed = login.wait()
        if not getattr(completed, "success", False):
            print("Codex sign-in did not complete successfully.", file=sys.stderr)
            return 1
        print("Codex account sign-in complete.", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
