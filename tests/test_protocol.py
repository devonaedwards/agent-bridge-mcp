#!/usr/bin/env python3
"""Fast tests: no subagents spawned, no tokens spent. Run these before every commit.

    python3 tests/test_protocol.py

Covers the MCP handshake, tool registration parity, command construction, and the
guard paths that are easy to break and annoying to debug live (the codex -c syntax
rules in particular, where a wrong table name fails SILENTLY).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import agent_bridge_mcp as ab  # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{(' - ' + detail) if detail else ''}")
        failures.append(name)


def test_handshake() -> None:
    print("\nMCP handshake")
    proc = subprocess.Popen(
        [sys.executable, str(REPO / "agent_bridge_mcp.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "nonexistent/method"},
    ]
    out, _ = proc.communicate("\n".join(json.dumps(m) for m in msgs) + "\n", timeout=30)
    replies = {json.loads(line)["id"]: json.loads(line) for line in out.strip().splitlines()}

    check("initialize returns serverInfo", replies[1]["result"]["serverInfo"]["name"] == ab.SERVER_NAME)
    check("initialize echoes protocolVersion", "protocolVersion" in replies[1]["result"])
    check("tools/list returns tools", len(replies[2]["result"]["tools"]) > 0)
    check("unknown method -> -32601", replies[3].get("error", {}).get("code") == -32601)


def test_registration_parity() -> None:
    print("\nTool registration")
    schema_names = {t["name"] for t in ab.tool_schema()}
    check("every handler has a schema", set(ab.TOOL_HANDLERS) == schema_names,
          f"handlers-only={set(ab.TOOL_HANDLERS) - schema_names} schema-only={schema_names - set(ab.TOOL_HANDLERS)}")
    for tool in ab.tool_schema():
        check(f"{tool['name']} has a description", bool(tool.get("description")))
    for name in ("peek_agent", "ask_parent", "pending_questions", "answer_agent", "continue_claude_agent"):
        check(f"{name} is registered", name in ab.TOOL_HANDLERS)


def test_ask_parent_guards() -> None:
    print("\nask_parent guards")
    # No AGENT_BRIDGE_JOB_ID -> must fail fast rather than block for the full timeout.
    import os
    saved = os.environ.pop("AGENT_BRIDGE_JOB_ID", None)
    try:
        ab.ask_parent({"question": "anything"})
        check("ask_parent without job id raises", False, "no exception raised")
    except ValueError as exc:
        check("ask_parent without job id raises", "background" in str(exc).lower())
    finally:
        if saved:
            os.environ["AGENT_BRIDGE_JOB_ID"] = saved

    try:
        ab.answer_agent({"question_id": "does-not-exist", "answer": "x"})
        check("answer_agent rejects unknown id", False, "no exception raised")
    except ValueError as exc:
        check("answer_agent rejects unknown id", "unknown question_id" in str(exc))

    try:
        ab.get_job("does-not-exist")
        check("peek_agent rejects unknown job", False, "no exception raised")
    except ValueError as exc:
        check("peek_agent rejects unknown job", "unknown job_id" in str(exc))


def test_preamble_gating() -> None:
    print("\nask_parent preamble gating")
    _, sync_prompt, *_ = ab.build_claude_command({"prompt": "t", "cwd": "/tmp"}, background=False)
    check("run_* claude prompt has NO preamble", ab.ASK_PARENT_TOOL not in sync_prompt)

    _, bg_prompt, *_ = ab.build_claude_command({"prompt": "t", "cwd": "/tmp"}, background=True)
    check("launch_* claude prompt HAS preamble", ab.ASK_PARENT_TOOL in bg_prompt)

    _, codex_prompt, *_ = ab.build_codex_command({"prompt": "t", "cwd": "/tmp"}, background=True, job_id="J")
    check("codex prompt names the UNDERSCORE tool", ab.CODEX_ASK_PARENT_TOOL in codex_prompt)
    check("codex prompt does NOT name the hyphen tool", ab.ASK_PARENT_TOOL not in codex_prompt,
          "codex renames the server, so the hyphenated name would send it hunting")

    # An exhaustive allowlist that omits ask_parent would contradict the preamble.
    cmd, _, *_ = ab.build_claude_command(
        {"prompt": "t", "cwd": "/tmp", "allowed_tools": ["Bash"]}, background=True)
    joined = " ".join(cmd)
    check("ask_parent force-added to a caller allowlist", ab.ASK_PARENT_TOOL in joined)


def test_codex_overrides() -> None:
    print("\ncodex -c injection syntax")
    flags = ab.codex_bridge_overrides("JOB-1")
    joined = " ".join(flags)
    check("uses snake_case mcp_servers", "mcp_servers." in joined)
    check("never emits camelCase mcpServers", "mcpServers" not in joined,
          "codex IGNORES mcpServers silently - no error, server just never appears")
    check("server name has no hyphen", f"mcp_servers.{ab.CODEX_BRIDGE_MCP_NAME}." in joined)
    check("name is not quoted in the -c path", f'mcp_servers."' not in joined,
          "quoting embeds the quote chars in the server name")
    check("approval mode is 'approve'", 'default_tools_approval_mode="approve"' in joined,
          "'auto' defers to --ask-for-approval never and still denies")
    check("tool timeout exceeds ask_parent default", ab.CODEX_BRIDGE_TOOL_TIMEOUT_SEC > 600)
    check("job id is passed through env", "JOB-1" in joined)
    check("args value is TOML not JSON", '.args=["' in joined)


def test_transcript_parsers() -> None:
    print("\ntranscript event parsing")
    claude_entry = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "hello"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
    ]}}
    events = ab._claude_events(claude_entry, include_tool_calls=True)
    check("claude text parsed", any(e["kind"] == "message" and e["text"] == "hello" for e in events))
    check("claude tool_use parsed", any(e["kind"] == "tool_call" and e["tool"] == "Bash" for e in events))
    check("claude tool calls suppressible",
          all(e["kind"] != "tool_call" for e in ab._claude_events(claude_entry, include_tool_calls=False)))

    # payload.message, not payload.text - this bit me.
    codex_msg = {"type": "event_msg", "payload": {"type": "agent_message", "message": "step 1"}}
    check("codex agent_message parsed",
          ab._codex_events(codex_msg, True)[0]["text"] == "step 1")
    codex_call = {"type": "response_item", "payload": {
        "type": "custom_tool_call", "name": "exec", "input": "cmd"}}
    check("codex custom_tool_call parsed",
          ab._codex_events(codex_call, True)[0]["tool"] == "exec")
    # function_call_output is a LIST of blocks; custom_tool_call_output is a plain string.
    codex_out = {"type": "response_item", "payload": {
        "type": "function_call_output", "output": [{"type": "input_text", "text": "DONE"}]}}
    check("codex list-shaped output flattened", "DONE" in ab._codex_events(codex_out, True)[0]["summary"])


if __name__ == "__main__":
    test_handshake()
    test_registration_parity()
    test_ask_parent_guards()
    test_preamble_gating()
    test_codex_overrides()
    test_transcript_parsers()

    print()
    if failures:
        print(f"FAILED ({len(failures)}): {', '.join(failures)}")
        raise SystemExit(1)
    print("all passing")
