#!/usr/bin/env python3
"""Live end-to-end tests. SPAWNS REAL SUBAGENTS AND SPENDS TOKENS.

    python3 tests/test_live.py claude    # ~1 min
    python3 tests/test_live.py codex     # ~3 min
    python3 tests/test_live.py all

Speaks JSON-RPC to a freshly spawned server rather than going through a client, so
it exercises the code on disk right now - not whatever version your editor's MCP
client happens to have loaded. That distinction matters: an MCP client keeps its
server process alive across edits, so a client-side test can silently pass against
stale code.

Each scenario forces the agent into a genuine unknown so it MUST call ask_parent;
an agent that could guess would make the test vacuous.
"""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CODENAME = "ALBATROSS"


class Server:
    """Minimal JSON-RPC client for one agent_bridge_mcp.py subprocess."""

    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(REPO / "agent_bridge_mcp.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, cwd=str(REPO),
        )
        self.replies: queue.Queue = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()
        self._next_id = 0
        self.rpc("initialize", {})

    def _reader(self) -> None:
        for line in self.proc.stdout:
            try:
                self.replies.put(json.loads(line))
            except json.JSONDecodeError:
                pass

    def rpc(self, method: str, params: dict | None = None, timeout: int = 900) -> dict:
        self._next_id += 1
        rid = self._next_id
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                reply = self.replies.get(timeout=5)
            except queue.Empty:
                continue
            if reply.get("id") == rid:
                return reply
        raise TimeoutError(f"no reply to {method} within {timeout}s")

    def call(self, tool: str, **args) -> dict:
        reply = self.rpc("tools/call", {"name": tool, "arguments": args})
        result = reply["result"]
        payload = result["content"][0]["text"]
        if result.get("isError"):
            raise RuntimeError(payload)
        return json.loads(payload)

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass


def run_scenario(kind: str) -> bool:
    """Launch an agent that cannot succeed without asking, then answer it."""
    print(f"\n=== {kind}: ask_parent + peek_agent round trip ===")
    server = Server()
    try:
        launch = f"launch_{kind}_agent"
        job = server.call(
            launch,
            prompt=(
                "Task: report the project codename. You do NOT know it and it is not "
                "discoverable anywhere on disk - do not search for it. Ask the parent "
                "for it, then reply with only that codename."
            ),
            cwd="/tmp",
        )
        job_id = job["job_id"]
        print(f"launched {kind} job {job_id}")

        question_id = None
        cursor = 0
        deadline = time.time() + 420
        while time.time() < deadline:
            time.sleep(6)

            peek = server.call("peek_agent", job_id=job_id, since=cursor, limit=15)
            cursor = peek.get("cursor", cursor)
            for event in peek.get("events", []):
                label = event.get("tool") or event.get("role") or ""
                text = (event.get("text") or event.get("summary") or "")[:80]
                print(f"   [{event.get('kind')}] {label} :: {text}")

            if question_id is None:
                pending = server.call("pending_questions", job_id=job_id)
                if pending["count"]:
                    question = pending["questions"][0]
                    question_id = question["question_id"]
                    print(f"\n>>> {kind} ASKED: {question['question'][:160]}")
                    server.call("answer_agent", question_id=question_id,
                                answer=f"The project codename is {CODENAME}.")
                    print(">>> answered\n")

            if peek.get("status") != "running":
                break

        result = server.call("agent_result", job_id=job_id)
        stdout = result.get("stdout") or ""

        asked = question_id is not None
        answered = CODENAME in stdout
        print(f"\nasked the parent : {asked}")
        print(f"used the answer  : {answered}")
        print(f"final output     : {stdout.strip()[-120:]!r}")

        if not asked:
            print(f"FAIL: {kind} never called ask_parent")
        elif not answered:
            print(f"FAIL: {kind} asked but did not use the answer")
        else:
            print(f"PASS: {kind}")
        return asked and answered
    finally:
        server.close()


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    kinds = ["claude", "codex"] if which == "all" else [which]
    if any(k not in ("claude", "codex") for k in kinds):
        raise SystemExit("usage: test_live.py [claude|codex|all]")

    results = {kind: run_scenario(kind) for kind in kinds}
    print("\n" + "=" * 40)
    for kind, passed in results.items():
        print(f"{kind:8} {'PASS' if passed else 'FAIL'}")
    raise SystemExit(0 if all(results.values()) else 1)
