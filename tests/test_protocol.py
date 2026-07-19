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
import time
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
    # Must match the key config.toml uses, or codex registers a SECOND server, both
    # normalize to agent_bridge, and it disambiguates with per-launch hash suffixes.
    check("injects under the config.toml key", f"mcp_servers.{ab.BRIDGE_MCP_NAME}." in joined)
    check("does not inject a second underscore key",
          f"mcp_servers.{ab.CODEX_BRIDGE_MCP_NAME}." not in joined,
          "a differing key duplicates the server and produces hashed tool names")
    check("name is not quoted in the -c path", 'mcp_servers."' not in joined,
          "quoting embeds the quote chars in the server name")
    check("approval mode is 'approve'", 'default_tools_approval_mode="approve"' in joined,
          "'auto' defers to --ask-for-approval never and still denies")
    check("tool timeout exceeds ask_parent default", ab.CODEX_BRIDGE_TOOL_TIMEOUT_SEC > 600)
    check("job id is passed through env", "JOB-1" in joined)
    check("args value is TOML not JSON", '.args=["' in joined)
    # Codex forwards no parent env, so anything the child's bridge needs must be listed.
    # A missing ancestry fails quietly: escalated questions report depth=1 and can't route.
    check("ancestry forwarded through -c env", "AGENT_BRIDGE_ANCESTRY" in joined)


def test_escalation() -> None:
    print("\nescalation")
    import os
    os.environ["AGENT_BRIDGE_JOB_ID"] = "JOB-CHILD"
    os.environ.pop("AGENT_BRIDGE_ANCESTRY", None)
    try:
        record = {
            "question_id": "Q-ESC", "job_id": "JOB-CHILD", "question": "which one?",
            "context": "", "status": "pending", "asked_at": time.time(), "answer": None,
            "answered_at": None, "on_timeout": "proceed", "ancestry": ["JOB-TOP"],
            "depth": 2, "escalated": False, "escalation_notes": [],
        }
        ab._write_question(record)

        payload = json.loads(ab.escalate_question(
            {"question_id": "Q-ESC", "note": "no basis to answer"})["content"][0]["text"])
        check("marks escalated", payload["escalated"] is True)
        check("question stays pending", payload["status"] == "pending")
        # The flaw a live run exposed: an escalating agent waited for the answer to come
        # back to it, saw none, and reported the correct result as fabricated.
        check("tells escalator NOT to relay", "do_not_relay" in payload)
        check("tells escalator how to check", "pending_questions" in payload.get("how_to_check", ""))

        stored = ab._read_question("Q-ESC")
        check("note recorded with provenance", stored["escalation_notes"][0]["from_job"] == "JOB-CHILD")

        listed = json.loads(ab.pending_questions({"escalated_only": True})["content"][0]["text"])
        check("escalated_only filter works", any(q["question_id"] == "Q-ESC" for q in listed["questions"]))
        check("escalation raises action_required", "action_required" in listed)

        try:
            ab.escalate_question({"question_id": "Q-NOPE", "note": ""})
            check("escalate rejects unknown id", False, "no exception")
        except ValueError:
            check("escalate rejects unknown id", True)
    finally:
        os.environ.pop("AGENT_BRIDGE_JOB_ID", None)
        (ab.QUESTIONS_DIR / "Q-ESC.json").unlink(missing_ok=True)


def test_supervision_notes() -> None:
    print("\nsupervision notes")
    import os
    job_id = "JOB-NOTES"
    os.environ["AGENT_BRIDGE_JOB_ID"] = job_id
    try:
        ab._write_notes(job_id, [])
        # A note to a finished job would never be read - fail loudly instead of silently
        # queueing it, and point at the tool that does work on a finished job.
        try:
            ab.send_note({"job_id": "no-such-job", "note": "x"})
            check("send_note rejects unknown job", False, "no exception")
        except ValueError as exc:
            check("send_note rejects unknown job", "unknown job_id" in str(exc))

        ab._write_notes(job_id, [{"note_id": "n1", "note": "switch to weather",
                                  "sent_at": time.time(), "read_at": None}])
        first = json.loads(ab.check_notes({})["content"][0]["text"])
        check("check_notes returns unread", first["count"] == 1)
        check("note text delivered", first["notes"][0]["note"] == "switch to weather")
        check("delivery carries precedence instruction", "supersedes" in first.get("instruction", ""))

        second = json.loads(ab.check_notes({})["content"][0]["text"])
        check("notes are marked read once delivered", second["count"] == 0,
              "re-delivering would make an agent apply the same correction repeatedly")

        saved = os.environ.pop("AGENT_BRIDGE_JOB_ID")
        try:
            ab.check_notes({})
            check("check_notes needs a job id", False, "no exception")
        except ValueError:
            check("check_notes needs a job id", True)
        finally:
            os.environ["AGENT_BRIDGE_JOB_ID"] = saved

        # The template carries a {notes_tool} placeholder, so assert on the FORMATTED
        # preamble - what the subagent actually receives.
        rendered = ab.with_ask_parent_preamble("task")
        check("preamble teaches check_notes", ab.CHECK_NOTES_TOOL in rendered)
        check("preamble warns against polling", "loop" in rendered)
        cmd, _, *_ = ab.build_claude_command(
            {"prompt": "t", "cwd": "/tmp", "allowed_tools": ["Bash"]}, background=True)
        check("check_notes force-added to allowlist", ab.CHECK_NOTES_TOOL in " ".join(cmd))
    finally:
        os.environ.pop("AGENT_BRIDGE_JOB_ID", None)
        (ab.NOTES_DIR / f"{job_id}.json").unlink(missing_ok=True)


def test_delegation() -> None:
    print("\ndelegation to cheaper models")
    import os
    check("codex ladder is most-capable-first", ab.codex_model_tiers()[0].endswith("sol"),
          "cache order IS the capability order: sol > terra > luna")
    check("claude ladder ranks opus above haiku",
          ab.model_rank("claude", "claude-opus-4-8") < ab.model_rank("claude", "claude-haiku-4-5"))
    check("context-window suffix is stripped",
          ab.model_rank("claude", "claude-opus-4-8[1m]") == ab.model_rank("claude", "claude-opus-4-8"))

    saved = {k: os.environ.get(k) for k in
             ("AGENT_BRIDGE_JOB_ID", "AGENT_BRIDGE_MODEL", "AGENT_BRIDGE_PARENT")}
    try:
        os.environ.pop("AGENT_BRIDGE_JOB_ID", None)
        ab._helpers_launched = 0
        ab.enforce_delegation("claude", "claude-opus-4-8")
        check("top-level launches are unrestricted", True)

        os.environ.update({"AGENT_BRIDGE_JOB_ID": "J", "AGENT_BRIDGE_MODEL": "claude-sonnet-5",
                           "AGENT_BRIDGE_PARENT": "claude"})
        ab._helpers_launched = 0
        for label, model in (("upward", "claude-opus-4-8"), ("sideways", "claude-sonnet-5")):
            try:
                ab.enforce_delegation("claude", model)
                check(f"subagent blocked from delegating {label}", False, "allowed")
            except RuntimeError as exc:
                check(f"subagent blocked from delegating {label}", "DOWNWARD" in str(exc))
        try:
            ab.enforce_delegation("claude", None)
            check("subagent must name a model", False, "allowed")
        except ValueError:
            check("subagent must name a model", True)

        ab._helpers_launched = 0
        ab.enforce_delegation("claude", "claude-haiku-4-5")
        ab.enforce_delegation("claude", "claude-haiku-4-5")
        check("two helpers allowed", ab._helpers_launched == 2)
        try:
            ab.enforce_delegation("claude", "claude-haiku-4-5")
            check("third helper blocked", False, "allowed")
        except RuntimeError as exc:
            check("third helper blocked", "limit reached" in str(exc))

        rendered = ab.with_ask_parent_preamble("task")
        check("preamble offers delegation", "cheaper model" in rendered.lower())
        check("preamble keeps accountability", "responsible for the work" in rendered)
    finally:
        ab._helpers_launched = 0
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_concerns() -> None:
    print("\nconcerns (see something, say something)")
    import os
    saved = os.environ.get("AGENT_BRIDGE_JOB_ID")
    os.environ["AGENT_BRIDGE_JOB_ID"] = "JOB-CONCERN"
    try:
        payload = json.loads(ab.raise_concern({
            "concern": "rmtree on / in maintenance.py",
            "evidence": "maintenance.py:11",
            "severity": "critical",
        })["content"][0]["text"])
        check("records the concern", payload["recorded"] is True)
        # The whole point is that it does NOT block - a blocking need is a question.
        check("tells the agent it is not blocked", "NOT blocked" in payload.get("note", ""))
        check("points blocking needs at ask_parent", "ask_parent" in payload.get("note", ""))

        listed = json.loads(ab.list_concerns({"job_id": "JOB-CONCERN"})["content"][0]["text"])
        check("parent can read it", listed["count"] == 1)
        check("critical counted", listed["critical_count"] == 1)
        filtered = json.loads(ab.list_concerns(
            {"job_id": "JOB-CONCERN", "min_severity": "critical"})["content"][0]["text"])
        check("severity filter works", filtered["count"] == 1)

        os.environ.pop("AGENT_BRIDGE_JOB_ID")
        try:
            ab.raise_concern({"concern": "x"})
            check("needs a job id", False, "no exception")
        except ValueError:
            check("needs a job id", True)

        rendered = ab.with_ask_parent_preamble("task")
        check("preamble empowers speaking up", "nobody asked me" in rendered)
        check("preamble names the concern tool", ab.RAISE_CONCERN_TOOL in rendered)
    finally:
        if saved is None:
            os.environ.pop("AGENT_BRIDGE_JOB_ID", None)
        else:
            os.environ["AGENT_BRIDGE_JOB_ID"] = saved
        for f in ab.CONCERNS_DIR.glob("*.json"):
            try:
                if json.loads(f.read_text())["job_id"] == "JOB-CONCERN":
                    f.unlink()
            except (OSError, json.JSONDecodeError, KeyError):
                pass


def test_preamble_gating_sections() -> None:
    print("\npreamble section gating")
    import os
    saved = {k: os.environ.get(k) for k in ("AGENT_BRIDGE_DEPTH", "AGENT_BRIDGE_MAX_HELPERS")}
    try:
        os.environ["AGENT_BRIDGE_DEPTH"] = "0"
        os.environ.pop("AGENT_BRIDGE_MAX_HELPERS", None)
        full = ab.select_preamble_sections()
        check("child below the ceiling gets everything", set(full) == set(ab.PREAMBLE_ORDER))
        check("order is stable regardless of selection",
              full == [s for s in ab.PREAMBLE_ORDER if s in set(full)])

        # A one-shot job finishes before it would ever reach a phase boundary.
        check("single-phase drops notes", "notes" not in ab.select_preamble_sections(multi_phase=False))

        # Structural: a child at the recursion ceiling cannot launch anything, so telling
        # it how to delegate or escalate advertises a door that is locked.
        os.environ["AGENT_BRIDGE_DEPTH"] = str(ab.max_depth() - 1)
        ceiling = ab.select_preamble_sections()
        check("ceiling child loses escalate", "escalate" not in ceiling)
        check("ceiling child loses delegate", "delegate" not in ceiling)
        check("ceiling child keeps the question channel", "core" in ceiling)
        check("structural gate overrides an explicit caller list",
              "delegate" not in ab.select_preamble_sections(["core", "delegate"]),
              "a caller must not be able to advertise a capability the child lacks")

        os.environ["AGENT_BRIDGE_DEPTH"] = "0"
        os.environ["AGENT_BRIDGE_MAX_HELPERS"] = "0"
        check("helpers disabled drops delegate", "delegate" not in ab.select_preamble_sections())
        os.environ.pop("AGENT_BRIDGE_MAX_HELPERS")

        try:
            ab.select_preamble_sections(["core", "not-a-section"])
            check("unknown section rejected", False, "no exception")
        except ValueError as exc:
            check("unknown section rejected", "not-a-section" in str(exc))

        # Gating must shrink the prompt, and every section must still render.
        big = ab.with_ask_parent_preamble("")
        small = ab.with_ask_parent_preamble("", sections=ab.MINIMAL_SECTIONS)
        check("minimal is materially smaller", len(small) < len(big) * 0.7)
        for name in ab.PREAMBLE_ORDER:
            rendered = ab.PREAMBLE_SECTIONS[name].format(
                tool="T", notes_tool="N", concern_tool="C", max_helpers=2)
            check(f"section '{name}' renders with no stray placeholder", "{" not in rendered)

        cmd_args = {"prompt": "t", "cwd": "/tmp", "multi_phase": False}
        _, trimmed, *_ = ab.build_claude_command(cmd_args, background=True)
        check("launch_claude_agent honors multi_phase", ab.CHECK_NOTES_TOOL not in trimmed)
        _, codex_trimmed, *_ = ab.build_codex_command(cmd_args, background=True, job_id="J")
        check("launch_codex_agent honors multi_phase", ab.CODEX_CHECK_NOTES_TOOL not in codex_trimmed)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_on_timeout_schema() -> None:
    print("\non_timeout")
    schema = next(t for t in ab.tool_schema() if t["name"] == "ask_parent")
    prop = schema["inputSchema"]["properties"].get("on_timeout", {})
    check("on_timeout exposed", set(prop.get("enum", [])) == {"proceed", "abort"})
    check("defaults to proceed", prop.get("default") == "proceed")
    # Assert on the RENDERED preamble - sections are assembled per launch now.
    rendered = ab.with_ask_parent_preamble("task")
    check("preamble teaches abort", "abort" in rendered)
    check("preamble teaches escalation", "escalate_question" in ab.PREAMBLE_SECTIONS["escalate"])


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
    test_escalation()
    test_supervision_notes()
    test_delegation()
    test_concerns()
    test_preamble_gating_sections()
    test_on_timeout_schema()
    test_transcript_parsers()

    print()
    if failures:
        print(f"FAILED ({len(failures)}): {', '.join(failures)}")
        raise SystemExit(1)
    print("all passing")
