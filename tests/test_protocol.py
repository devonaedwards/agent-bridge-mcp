#!/usr/bin/env python3
"""Fast tests: no subagents spawned, no tokens spent. Run these before every commit.

    python3 tests/test_protocol.py

Covers the MCP handshake, tool registration parity, command construction, and the
guard paths that are easy to break and annoying to debug live (the codex -c syntax
rules in particular, where a wrong table name fails SILENTLY).
"""
from __future__ import annotations

import json
import os
import re
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
    print("\ndelegation: down, sideways, and up to specialists")
    import os
    REASON = "need a stronger model to settle the export-control question"

    # A controlled cache, not ~/.codex: the real one varies by install and once had no
    # sol at all, which made this test fail on one machine and pass on another.
    import tempfile
    saved_home = os.environ.get("CODEX_HOME")
    with tempfile.TemporaryDirectory() as home:
        (Path(home) / "models_cache.json").write_text(json.dumps({"models": [
            {"slug": "gpt-reserve"}, {"slug": "gpt-5.6-sol"}, {"slug": "codex-auto-review"},
            {"slug": "gpt-5.6-terra"}, {"slug": "gpt-5.6-luna"}]}))
        os.environ["CODEX_HOME"] = home
        check("codex ladder is most-capable-first",
              ab.codex_model_tiers() == ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"],
              "cache order IS the capability order; gpt-reserve and codex-* are not rungs")
    if saved_home is None:
        os.environ.pop("CODEX_HOME", None)
    else:
        os.environ["CODEX_HOME"] = saved_home
    check("claude ladder ranks opus above haiku",
          ab.model_rank("claude", "claude-opus-4-8") < ab.model_rank("claude", "claude-haiku-4-5"))
    check("context-window suffix is stripped",
          ab.model_rank("claude", "claude-opus-4-8[1m]") == ab.model_rank("claude", "claude-opus-4-8"))
    # Prefix matching: the longer id must win, or fable-5-1 ranks as fable-5.
    check("fable 5.1 outranks fable 5",
          ab.model_rank("claude", "claude-fable-5-1") < ab.model_rank("claude", "claude-fable-5"))
    check("opus 5 is on the ladder", ab.model_rank("claude", "claude-opus-5[1m]") is not None,
          "an unranked own-model silently disables the direction check")
    check("dated snapshot ids rank as the base model",
          ab.model_rank("claude", "claude-haiku-4-5-20251001")
          == ab.model_rank("claude", "claude-haiku-4-5"))
    check("bare family alias is accepted and means the latest",
          ab.model_rank("claude", "opus") < ab.model_rank("claude", "claude-opus-5"),
          "`--model opus` is a real thing the CLI takes")

    # The point of ranking claude structurally: models that do not exist yet must rank
    # themselves, with nothing in this file to update.
    for newer, older in (
        ("claude-opus-5-2", "claude-opus-5"),
        ("claude-opus-6", "claude-opus-5-2"),
        ("claude-opus-8-33", "claude-opus-6"),
        ("claude-opus-4-10", "claude-opus-4-8"),   # numeric, not lexical
        ("claude-fable-7", "claude-opus-9"),       # family beats version across tiers
        ("claude-sonnet-9", "claude-haiku-9"),
    ):
        check(f"{newer} outranks {older} with no list to edit",
              ab.model_rank("claude", newer) < ab.model_rank("claude", older))
    check("a future opus still bands as strong general-purpose",
          ab.capability_band("claude", "claude-opus-8-33")
          == ab.capability_band("claude", "claude-opus-5"))
    check("a future opus hiring a future fable reads as upward",
          ab.capability_delta("claude", "claude-opus-8-33", "claude", "claude-fable-9") > 0)
    check("a future opus and sol are peers",
          ab.capability_delta("claude", "claude-opus-8-33", "codex", "gpt-5.6-sol") == 0)
    check("an unknown family stays unranked rather than guessed",
          ab.model_rank("claude", "claude-quill-6") is None,
          "permissive by design - the launch is allowed and logged, not silently mis-tiered")
    check("a malformed id is still rejected",
          ab.model_rank("claude", "claude-opuss-5") is None
          and ab.model_rank("claude", "claude-opus-abc") is None)

    # Cross-vendor comparisons go through the shared bands, not two unrelated indices.
    check("opus 5 -> fable reads as upward",
          ab.capability_delta("claude", "claude-opus-5", "claude", "claude-fable-5-1") > 0)
    check("opus 5 <-> sol read as sideways in both directions",
          ab.capability_delta("claude", "claude-opus-5", "codex", "gpt-5.6-sol") == 0
          and ab.capability_delta("codex", "gpt-5.6-sol", "claude", "claude-opus-5[1m]") == 0)
    check("terra -> sol reads as upward",
          ab.capability_delta("codex", "gpt-5.6-terra", "codex", "gpt-5.6-sol") > 0)
    check("terra -> fable reads as upward",
          ab.capability_delta("codex", "gpt-5.6-terra", "claude", "claude-fable-5-1") > 0)
    check("opus 5 -> haiku still reads as downward",
          ab.capability_delta("claude", "claude-opus-5", "claude", "claude-haiku-4-5") < 0)
    check("opus 5 -> gpt-5.4-mini reads as downward across vendors",
          ab.capability_delta("claude", "claude-opus-5", "codex", "gpt-5.4-mini") < 0,
          "the old per-ladder index comparison called this upward")
    check("within one vendor the fine ladder still separates same-band models",
          ab.capability_delta("claude", "claude-opus-5", "claude", "claude-opus-4-7") < 0)
    check("codex bands come from the model name, aligned with claude families",
          ab.capability_band("codex", "gpt-5.6-sol") == ab.CLAUDE_FAMILY_BANDS["opus"]
          and ab.capability_band("codex", "gpt-5.6-terra") == ab.CLAUDE_FAMILY_BANDS["sonnet"]
          and ab.capability_band("codex", "gpt-5.4-mini") == ab.CHEAPEST_BAND,
          "gpt-5.4-mini must not inherit gpt-5.4's band")
    check("only claude and codex are ranked",
          ab.model_rank("grok", "grok-4.5") is None and ab.capability_band("kimi", "kimi-code/k3") is None)
    check("unrankable model yields no direction",
          ab.capability_delta("claude", "who-knows", "claude", "claude-haiku-4-5") is None)

    saved = {k: os.environ.get(k) for k in
             ("AGENT_BRIDGE_JOB_ID", "AGENT_BRIDGE_MODEL", "AGENT_BRIDGE_PARENT",
              "AGENT_BRIDGE_MAX_ESCALATIONS")}
    try:
        os.environ.pop("AGENT_BRIDGE_JOB_ID", None)
        ab._helpers_launched = ab._escalations_launched = 0
        ab.enforce_delegation("claude", "claude-opus-4-8")
        check("top-level launches are unrestricted", True)

        os.environ.update({"AGENT_BRIDGE_JOB_ID": "J", "AGENT_BRIDGE_MODEL": "claude-opus-5[1m]",
                           "AGENT_BRIDGE_PARENT": "claude"})
        os.environ.pop("AGENT_BRIDGE_MAX_ESCALATIONS", None)

        # Sideways and downward spend the helper budget and need no reason.
        for label, kind, model in (("sideways to Sol", "codex", "gpt-5.6-sol"),
                                   ("sideways to a peer opus", "claude", "claude-opus-4-8"),
                                   ("downward to sonnet", "claude", "claude-sonnet-5"),
                                   ("downward to haiku", "claude", "claude-haiku-4-5")):
            ab._helpers_launched = ab._escalations_launched = 0
            try:
                ab.enforce_delegation(kind, model)
                check(f"opus may delegate {label}",
                      ab._helpers_launched == 1 and ab._escalations_launched == 0)
            except (RuntimeError, ValueError) as exc:
                check(f"opus may delegate {label}", False, str(exc))

        # And the reverse pairing: Sol may hand work to Opus.
        os.environ.update({"AGENT_BRIDGE_MODEL": "gpt-5.6-sol", "AGENT_BRIDGE_PARENT": "codex"})
        ab._helpers_launched = ab._escalations_launched = 0
        try:
            ab.enforce_delegation("claude", "claude-opus-5")
            check("Sol may delegate sideways to Opus", ab._helpers_launched == 1)
        except (RuntimeError, ValueError) as exc:
            check("Sol may delegate sideways to Opus", False, str(exc))

        # Upward is allowed, but only deliberately: it needs a real reason.
        for own_kind, own, label, kind, model in (
            ("claude", "claude-opus-5[1m]", "opus hiring fable", "claude", "claude-fable-5-1"),
            ("codex", "gpt-5.6-sol", "sol hiring fable", "claude", "claude-fable-5-1"),
        ):
            os.environ.update({"AGENT_BRIDGE_MODEL": own, "AGENT_BRIDGE_PARENT": own_kind})
            ab._helpers_launched = ab._escalations_launched = 0
            try:
                ab.enforce_delegation(kind, model)
                check(f"{label} needs a reason", False, "allowed with no reason")
            except ValueError as exc:
                check(f"{label} needs a reason", "specialist_reason" in str(exc))
            try:
                ab.enforce_delegation(kind, model, "why")
                check(f"{label} rejects a token reason", False, "allowed")
            except ValueError:
                check(f"{label} rejects a token reason", True)
            ab.enforce_delegation(kind, model, REASON)
            check(f"{label} allowed with a reason",
                  ab._escalations_launched == 1 and ab._helpers_launched == 0,
                  "upward spends the specialist budget, not the helper budget")
        # Terra hiring Sol and Fable - the same affordance from the codex side.
        os.environ.update({"AGENT_BRIDGE_MODEL": "gpt-5.6-terra", "AGENT_BRIDGE_PARENT": "codex"})
        os.environ["AGENT_BRIDGE_MAX_ESCALATIONS"] = "2"
        ab._helpers_launched = ab._escalations_launched = 0
        ab.enforce_delegation("codex", "gpt-5.6-sol", REASON)
        ab.enforce_delegation("claude", "claude-fable-5-1", REASON)
        check("terra may hire sol and fable", ab._escalations_launched == 2)
        try:
            ab.enforce_delegation("codex", "gpt-5.6-sol", REASON)
            check("specialist budget is enforced", False, "allowed")
        except RuntimeError as exc:
            check("specialist budget is enforced", "specialist limit reached" in str(exc))

        os.environ["AGENT_BRIDGE_MAX_ESCALATIONS"] = "0"
        ab._escalations_launched = 0
        try:
            ab.enforce_delegation("codex", "gpt-5.6-sol", REASON)
            check("escalations can be switched off", False, "allowed")
        except RuntimeError as exc:
            check("escalations can be switched off", "disabled" in str(exc))
        os.environ.pop("AGENT_BRIDGE_MAX_ESCALATIONS")

        os.environ.update({"AGENT_BRIDGE_MODEL": "claude-opus-5", "AGENT_BRIDGE_PARENT": "claude"})
        try:
            ab.enforce_delegation("claude", None)
            check("subagent must name a model", False, "allowed")
        except ValueError:
            check("subagent must name a model", True)
        try:
            ab.enforce_delegation("claude", "claude-imaginary-9")
            check("unknown model rejected", False, "allowed")
        except ValueError as exc:
            check("unknown model rejected", "unknown model" in str(exc))
        try:
            ab.enforce_delegation("claude", "definitely-not-a-model")
            check("unknown non-claude id refused", False, "allowed")
        except ValueError as exc:
            check("unknown non-claude id refused", "unknown model" in str(exc))

        ab._helpers_launched = ab._escalations_launched = 0
        ab.enforce_delegation("claude", "claude-haiku-4-5")
        ab.enforce_delegation("claude", "claude-haiku-4-5")
        check("two helpers allowed", ab._helpers_launched == 2)
        try:
            ab.enforce_delegation("claude", "claude-haiku-4-5")
            check("third helper blocked", False, "allowed")
        except RuntimeError as exc:
            check("third helper blocked", "limit reached" in str(exc))
        check("helper budget does not spend the specialist budget",
              ab._escalations_launched == 0)

        rendered = ab.with_ask_parent_preamble("task")
        check("preamble offers cheaper delegation", "cheaper model" in rendered.lower())
        check("preamble offers sideways delegation", "SIDEWAYS" in rendered)
        check("preamble offers hiring upward", "specialist_reason" in rendered)
        check("preamble keeps accountability", "responsible for the work" in rendered)
        check("preamble keeps accountability when hiring up",
              "accountable for the result" in rendered)
    finally:
        ab._helpers_launched = ab._escalations_launched = 0
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_report_channel_always_allowed() -> None:
    print("\nreport channel is never gated")
    import os
    args = {"prompt": "p", "cwd": str(REPO)}
    cmd, *_ = ab.build_claude_command(dict(args), background=True)
    joined = " ".join(cmd)
    for tool in ab.REPORT_CHANNEL_TOOLS:
        check(f"{tool.split('__')[-1]} allowed with no caller allowlist", tool in joined,
              "a sandboxed child must be able to report instead of timing out silently")

    cmd, *_ = ab.build_claude_command(
        dict(args, allowed_tools=["Read"]), background=True)
    joined = " ".join(cmd)
    check("caller allowlist is preserved", "Read" in joined)
    check("report channel added to caller allowlist", ab.ASK_PARENT_TOOL in joined)

    # A denylist beats an allowlist, so the denied entry has to be dropped outright.
    cmd, *_ = ab.build_claude_command(
        dict(args, disallowed_tools=["Bash", ab.ASK_PARENT_TOOL]), background=True)
    flags = dict(zip(cmd, cmd[1:]))
    check("caller denylist is otherwise honored", "Bash" in flags.get("--disallowedTools", ""))
    check("report channel stripped from denylist",
          ab.ASK_PARENT_TOOL not in flags.get("--disallowedTools", ""))
    check("report channel still allowed despite denylist",
          ab.ASK_PARENT_TOOL in flags.get("--allowedTools", ""))

    # Synchronous run_* jobs get no job id, so ask_parent is inert there by design.
    cmd, *_ = ab.build_claude_command(dict(args), background=False)
    check("foreground runs are left alone", ab.ASK_PARENT_TOOL not in " ".join(cmd),
          "a blocking question to a parent that is itself blocked would deadlock")

    if Path(ab.build_grok_command({"prompt": "p", "cwd": str(REPO)})[0][0]).exists():
        cmd, *_ = ab.build_grok_command({"prompt": "p", "cwd": str(REPO)}, background=True)
        check("grok background launch allows ask_parent",
              "ask_parent" in " ".join(cmd) and "--allow" in cmd)


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
        check("ceiling child loses specialist", "specialist" not in ceiling)
        check("ceiling child keeps the question channel", "core" in ceiling)
        check("structural gate overrides an explicit caller list",
              "delegate" not in ab.select_preamble_sections(["core", "delegate"]),
              "a caller must not be able to advertise a capability the child lacks")

        os.environ["AGENT_BRIDGE_DEPTH"] = "0"
        os.environ["AGENT_BRIDGE_MAX_HELPERS"] = "0"
        check("helpers disabled drops delegate", "delegate" not in ab.select_preamble_sections())
        check("helpers disabled keeps specialist", "specialist" in ab.select_preamble_sections(),
              "the two budgets are independent")
        os.environ.pop("AGENT_BRIDGE_MAX_HELPERS")

        os.environ["AGENT_BRIDGE_MAX_ESCALATIONS"] = "0"
        check("escalations disabled drops specialist",
              "specialist" not in ab.select_preamble_sections())
        os.environ.pop("AGENT_BRIDGE_MAX_ESCALATIONS")

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
                tool="T", notes_tool="N", concern_tool="C", warm_tool="W", max_helpers=2,
                max_escalations=1)
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


def test_stderr_warnings() -> None:
    """The stderr warning scanner should find permission/rejection patterns."""
    print("\nstderr warning scanner")
    # Basic detection
    stderr = "auto-rejecting: external_directory (/tmp/secret)"
    warnings = ab._scan_stderr_warnings(stderr)
    check("finds auto-reject", len(warnings) > 0)
    check("includes line text", "auto-reject" in warnings[0]["line"])

    # Dedup: repeated lines should get a count
    stderr_dup = "permission requested: x\npermission requested: x"
    dedup = ab._scan_stderr_warnings(stderr_dup)
    check("dedup by pattern group and normalized line", dedup[0]["count"] == 2)

    # Clean stderr should produce no warnings
    clean = "Compiled successfully.\nAll tests passed."
    check("clean stderr has no warnings", len(ab._scan_stderr_warnings(clean)) == 0)

    # Empty stderr
    check("empty stderr has no warnings", len(ab._scan_stderr_warnings("")) == 0)

    # Multiple pattern groups
    multi = "permission requested\ndenied: access to /foo\nsandbox violation"
    multi_warns = ab._scan_stderr_warnings(multi)
    check("finds multiple distinct patterns", len(multi_warns) >= 3)


def test_path_scanner() -> None:
    """The out-of-sandbox path scanner catches paths outside cwd."""
    print("\nout-of-sandbox path scanner")
    cwd = str(Path(__file__).resolve().parent)

    # Absolute path outside cwd (only works if the path exists)
    import os
    python_bin = os.path.abspath(sys.executable)
    prompt_with_path = f"Please read {python_bin} and analyze it"
    to_add, skipped = ab._scan_prompt_for_outside_paths(prompt_with_path, cwd, [])
    check("detects absolute outside path", len(to_add) > 0,
          f"got: {to_add}")

    # ~/ path
    prompt_tilde = "Check ~/.bashrc"
    to_add2, _ = ab._scan_prompt_for_outside_paths(prompt_tilde, cwd, [])
    # ~/.bashrc may not exist, which is fine — the scanner only flags existing paths
    check("tilde path handled without error", isinstance(to_add2, list))

    # False positive: version strings
    prompt_version = "Use version 2.0.1 or later"
    to_add3, _ = ab._scan_prompt_for_outside_paths(prompt_version, cwd, [])
    check("version strings are not paths", len(to_add3) == 0)

    # False positive: URLs
    prompt_url = "See https://github.com/foo/bar for docs"
    to_add4, _ = ab._scan_prompt_for_outside_paths(prompt_url, cwd, [])
    check("URLs are not paths", len(to_add4) == 0)

    # Empty prompt
    check("empty prompt safe", len(ab._scan_prompt_for_outside_paths("", cwd, [])[0]) == 0)


def test_compact_status() -> None:
    """Bare agent_status must never inline stdout/stderr."""
    print("\ncompact agent_status shape")
    cmd = ["echo", "hello"]
    proc = subprocess.Popen(
        cmd, cwd=str(REPO),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    import time as _time
    import agent_bridge_mcp as _ab
    job = _ab.Job(
        id="test-compact-status", kind="test", command=cmd, cwd=str(REPO),
        timeout_seconds=10, started_at=_time.time(), process=proc,
    )
    proc.communicate(timeout=5)
    job.returncode = proc.returncode
    job.finished_at = _time.time()
    job.stdout = "hello world"
    job.stderr = ""
    _ab.jobs[job.id] = job

    compact = _ab._compact_job_summary(job)
    check("compact omits stdout", "stdout" not in compact)
    check("compact omits stderr", "stderr" not in compact)
    check("compact has job_id", "job_id" in compact)
    check("compact has status", "status" in compact)
    check("compact has elapsed", "elapsed_seconds" in compact)
    # files_changed is now a dict (Correction 3)
    fc = compact.get("files_changed")
    if fc is not None:
        check("files_changed is dict", isinstance(fc, dict))
        check("files_changed has files key", "files" in fc)


def test_sandbox_widening_reaches_command() -> None:
    """The sandbox scan must run BEFORE build_* so widened dirs reach the command."""
    print("\nsandbox widening in command")
    import os as _os
    state_dir = str(ab.STATE_DIR.resolve())

    # Claude: STATE_DIR should appear in the --add-dir flags
    args = {"prompt": "test", "cwd": str(REPO)}
    widened, _note = ab._prepare_subagent_sandbox(args, ab.resolve_cwd(args), "test")
    cmd, _, _, _, _ = ab.build_claude_command(args, background=True, add_dirs_override=widened)
    joined = " ".join(cmd)
    check("claude --add-dir has STATE_DIR", state_dir in joined,
          f"STATE_DIR not found in command")

    # Codex: STATE_DIR should appear in --add-dir flags
    job_id = str(ab.uuid.uuid4())
    cmd2, _, _, _, _ = ab.build_codex_command(args, background=True, job_id=job_id, add_dirs_override=widened)
    joined2 = " ".join(cmd2)
    check("codex --add-dir has STATE_DIR", state_dir in joined2,
          f"STATE_DIR not found in codex command")

    # Kimi: STATE_DIR should appear in --add-dir flags
    cmd3, _, _, _, _ = ab.build_kimi_command(args, background=True, add_dirs_override=widened)
    joined3 = " ".join(cmd3)
    check("kimi --add-dir has STATE_DIR", state_dir in joined3,
          f"STATE_DIR not found in kimi command")

    # Opencode: no --add-dir flag, but sandbox_note says so
    cmd4, _, _, _, _ = ab.build_opencode_command(args, background=True, add_dirs_override=widened)
    # _prepare_subagent_sandbox still widens but opencode build ignores per the docs
    check("opencode build does not crash with add_dirs_override", True)


def test_buffer_cap() -> None:
    """The buffer cap must preserve head + tail and never exceed the max."""
    print("\nbuffer cap")
    import agent_bridge_mcp as _ab
    head = "HEAD: " + "x" * (_ab.STREAM_BUFFER_HEAD_CHARS - 6)
    body = "BODY: " + "z" * (_ab.STREAM_BUFFER_MAX_CHARS * 2)
    tail = "TAIL: error message at end"
    huge = head + "\n" + body + "\n" + tail

    capped, dropped = _ab._cap_stream_buffer(huge)
    check("capped fits within max", len(capped) <= _ab.STREAM_BUFFER_MAX_CHARS,
          f"got {len(capped)} > {_ab.STREAM_BUFFER_MAX_CHARS}")
    check("head preserved", "HEAD:" in capped[:200])
    check("tail preserved", "TAIL:" in capped[-200:])

    # Should not cap a short string
    short = "short"
    check("short string unchanged", _ab._cap_stream_buffer(short)[0] == short)

    # The dropped-count must be CUMULATIVE across calls. Line-at-a-time is how the reader
    # thread actually uses this, and the per-call figure understates the true loss by
    # orders of magnitude - a marker claiming "221 chars truncated" on a job that really
    # lost 905,000 reads as "you have basically everything" when you have 18%.
    buf, dropped = "", 0
    line = "y" * 220 + "\n"
    n_lines = 5000
    for _ in range(n_lines):
        buf, dropped = _ab._cap_stream_buffer(buf + line, dropped)
    fed = n_lines * len(line)
    actually_dropped = fed - len(buf)
    check("cumulative dropped count is accurate",
          abs(dropped - actually_dropped) <= 100,
          f"reported {dropped}, actually dropped {actually_dropped}")
    marker = re.search(r"\[\.\.\. (\d+) chars truncated", buf)
    check("marker reports the cumulative figure",
          marker is not None and abs(int(marker.group(1)) - actually_dropped) <= 100,
          f"marker says {marker.group(1) if marker else 'NONE'}, actual {actually_dropped}")


def test_opencode_permission_env() -> None:
    """Opencode's STATE_DIR grant rides on an env var, since it has no --add-dir flag."""
    print("\nopencode sandbox permission")
    import agent_bridge_mcp as _ab

    raw = _ab.opencode_permission_env([str(_ab.STATE_DIR)])
    check("returns a config payload", raw is not None)
    cfg = json.loads(raw)
    rules = cfg["permission"]["external_directory"]
    expected = f"{Path(str(_ab.STATE_DIR)).expanduser().resolve()}/**"
    check("STATE_DIR is allowed", rules.get(expected) == "allow",
          f"rules were {rules}")
    check("glob is recursive", all(k.endswith("/**") for k in rules),
          "nested paths under STATE_DIR (questions/, notes/) are rejected by a /* glob")
    check("no dirs means no injection", _ab.opencode_permission_env([]) is None)

    # The variable is a general config channel; clobbering an inherited value would
    # silently drop whatever the parent put there.
    prior = os.environ.get("OPENCODE_CONFIG_CONTENT")
    os.environ["OPENCODE_CONFIG_CONTENT"] = json.dumps({"model": "someone/else"})
    try:
        merged = json.loads(_ab.opencode_permission_env([str(_ab.STATE_DIR)]))
        check("inherited config keys survive", merged.get("model") == "someone/else")
        check("permission still applied", "external_directory" in merged.get("permission", {}))
        os.environ["OPENCODE_CONFIG_CONTENT"] = "{not json"
        check("malformed inherited value does not raise",
              _ab.opencode_permission_env([str(_ab.STATE_DIR)]) is not None)
    finally:
        if prior is None:
            os.environ.pop("OPENCODE_CONFIG_CONTENT", None)
        else:
            os.environ["OPENCODE_CONFIG_CONTENT"] = prior

    # Only opencode gets the env var; every other client takes a CLI flag.
    env = _ab.child_env("opencode", job_id="j1", add_dirs=[str(_ab.STATE_DIR)])
    check("child_env injects for opencode", "OPENCODE_CONFIG_CONTENT" in env)
    env_claude = _ab.child_env("claude", job_id="j1", add_dirs=[str(_ab.STATE_DIR)])
    check("child_env does not inject for claude",
          env_claude.get("OPENCODE_CONFIG_CONTENT") == prior
          or "OPENCODE_CONFIG_CONTENT" not in env_claude)


def test_called_functions_exist() -> None:
    """Every bare function call resolves. 3a5d215 deleted run_command while all five
    run_* handlers still called it, and nothing failed until a run_* tool was used."""
    print("\ncalled functions exist")
    import ast
    import builtins
    tree = ast.parse(Path(ab.__file__).read_text())
    known = set(dir(builtins)) | set(vars(ab))
    missing = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        local = {a.arg for a in ast.walk(fn.args) if isinstance(a, ast.arg)}
        local |= {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        local |= {n.name for n in ast.walk(fn) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        local |= {(a.asname or a.name).split(".")[0] for n in ast.walk(fn) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        for call in ast.walk(fn):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                if call.func.id not in known and call.func.id not in local:
                    missing.add(call.func.id)
    check("no call to an undefined function", not missing, f"undefined: {sorted(missing)}")


if __name__ == "__main__":
    test_called_functions_exist()
    test_opencode_permission_env()
    test_handshake()
    test_registration_parity()
    test_ask_parent_guards()
    test_preamble_gating()
    test_codex_overrides()
    test_escalation()
    test_supervision_notes()
    test_delegation()
    test_report_channel_always_allowed()
    test_concerns()
    test_preamble_gating_sections()
    test_on_timeout_schema()
    test_transcript_parsers()
    test_stderr_warnings()
    test_path_scanner()
    test_compact_status()
    test_buffer_cap()
    test_sandbox_widening_reaches_command()

    print()
    if failures:
        print(f"FAILED ({len(failures)}): {', '.join(failures)}")
        raise SystemExit(1)
    print("all passing")
