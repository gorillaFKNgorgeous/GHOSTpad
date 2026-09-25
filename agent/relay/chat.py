# SPDX-License-Identifier: GPL-2.0-or-later
"""Durable single-owner Codex worker for GhostBlender's embedded chat."""

import os
from pathlib import Path
import threading
import time


DEVELOPER_INSTRUCTIONS = """You are the embedded GhostBlender assistant running for one trusted owner.
For Blender work, use the ghostblender MCP server and its live scene evidence rather than guessing.
Start with status and inspect_scene when the task depends on scene state. Keep edits in small steps,
poll job_result before dependent work, capture actual images when visual verification matters, and
never claim an edit succeeded without checking the result. Do not use shell commands or inspect the
relay filesystem. The final reply is displayed in a narrow iPad sidebar, so keep it concise and
state what changed or what the user must do next.
"""


def prepare_codex_environment(mcp_url):
    """Create the dedicated Codex home without placing relay credentials in it."""
    codex_home = Path(os.environ.get("CODEX_HOME", "/data/codex"))
    os.environ["CODEX_HOME"] = str(codex_home)
    os.environ.setdefault("HOME", "/tmp/ghostbridge-home")
    Path(os.environ["HOME"]).mkdir(parents=True, exist_ok=True)
    codex_home.mkdir(parents=True, exist_ok=True)
    workdir = Path("/tmp/ghostblender-chat")
    workdir.mkdir(parents=True, exist_ok=True)

    config = f'''approval_policy = "never"
sandbox_mode = "read-only"
web_search = "disabled"

[mcp_servers.ghostblender]
url = "{mcp_url}"
required = true
default_tools_approval_mode = "approve"
'''
    config_path = codex_home / "config.toml"
    if not config_path.exists() or config_path.read_text() != config:
        config_path.write_text(config)
        config_path.chmod(0o600)
    return codex_home, workdir


class ChatWorker:
    """Process queued user messages exactly once, serially, in one Codex thread."""

    def __init__(self, store, device_id, mcp_url, poll_interval=0.25):
        self.store = store
        self.device_id = device_id
        self.mcp_url = mcp_url
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._worker = None
        self._codex = None
        self._thread = None
        self._sdk = None
        self._workdir = None

    def start(self):
        if self._worker and self._worker.is_alive():
            return
        _, self._workdir = prepare_codex_environment(self.mcp_url)
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._run,
            name="ghostblender-codex",
            daemon=True,
        )
        self._worker.start()

    def close(self):
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=2.0)
        self._close_codex()

    def _close_codex(self):
        if self._codex is not None:
            try:
                self._codex.close()
            except Exception:
                pass
        self._codex = None
        self._thread = None
        self._sdk = None

    def _open_codex(self):
        if self._codex is not None:
            return self._codex
        from openai_codex import ApprovalMode, Codex, Sandbox

        codex = Codex()
        account = codex.account()
        if getattr(account, "account", None) is None:
            codex.close()
            raise RuntimeError("codex_not_signed_in")
        self._codex = codex
        self._sdk = (ApprovalMode, Sandbox)
        return codex

    def _thread_options(self):
        approval_mode, sandbox = self._sdk
        options = {
            "approval_mode": approval_mode.deny_all,
            "sandbox": sandbox.read_only,
            "cwd": str(self._workdir),
            "developer_instructions": DEVELOPER_INSTRUCTIONS,
        }
        effort = os.environ.get("CODEX_REASONING_EFFORT", "medium").strip()
        if effort:
            options["config"] = {"model_reasoning_effort": effort}
        model = os.environ.get("CODEX_MODEL", "").strip()
        if model:
            options["model"] = model
        return options

    def _ensure_thread(self):
        if self._thread is not None:
            return self._thread
        codex = self._open_codex()
        options = self._thread_options()
        thread_id = self.store.chat_thread_id(self.device_id)
        if thread_id:
            try:
                self._thread = codex.thread_resume(thread_id, **options)
                return self._thread
            except Exception:
                # Resuming has not started the user's new turn, so falling back to
                # a new conversation cannot duplicate Blender work.
                self.store.chat_set_thread_id(self.device_id, None)
                self._close_codex()
                codex = self._open_codex()
                options = self._thread_options()
        self._thread = codex.thread_start(**options)
        self.store.chat_set_thread_id(self.device_id, self._thread.id)
        return self._thread

    def _run_turn(self, text):
        thread = self._ensure_thread()
        approval_mode, sandbox = self._sdk
        result = thread.run(
            text,
            approval_mode=approval_mode.deny_all,
            sandbox=sandbox.read_only,
            cwd=str(self._workdir),
        )
        final = getattr(result, "final_response", None)
        if not isinstance(final, str) or not final.strip():
            raise RuntimeError("codex_returned_no_final_response")
        return final.strip()[:2000]

    def _run(self):
        while not self._stop.is_set():
            message = self.store.chat_claim(self.device_id)
            if message is None:
                self._stop.wait(self.poll_interval)
                continue

            message_id = message["id"]
            self.store.chat_event(
                self.device_id,
                message_id,
                "status",
                "AI working",
            )
            try:
                final = self._run_turn(message["text"])
            except RuntimeError as exc:
                self._close_codex()
                if str(exc) == "codex_not_signed_in":
                    text = "AI sign-in required on the relay before embedded chat can run."
                else:
                    text = (
                        "AI turn failed. It may have changed Blender before the failure; "
                        "inspect the scene before retrying."
                    )
                self.store.chat_fail(self.device_id, message_id, text)
            except Exception as exc:
                self._close_codex()
                print(
                    "ghostblender_chat_error=" + type(exc).__name__,
                    flush=True,
                )
                self.store.chat_fail(
                    self.device_id,
                    message_id,
                    "AI backend error. Inspect the scene before retrying, then check relay logs.",
                )
            else:
                self.store.chat_complete(self.device_id, message_id, final)
