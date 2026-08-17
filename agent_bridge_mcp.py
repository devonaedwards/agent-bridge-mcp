#!/usr/bin/env python3
"""MCP bridge for launching Claude Code and Codex as subagents.

The server intentionally has no third-party dependencies. It implements the
small MCP stdio surface needed by Claude Code and Codex: initialize,
tools/list, and tools/call.
"""

from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

try:
    import pty
    import select as _select
except ImportError:  # pragma: no cover - non-POSIX platforms
    pty = None
    _select = None


SERVER_NAME = "agent-bridge-mcp"
SERVER_VERSION = "0.5.0-grok-kimi-opencode"

DEFAULT_TIMEOUT_SECONDS = 900
DEFAULT_MAX_OUTPUT_CHARS = 30000
DEFAULT_MAX_DEPTH = 2

# Streaming buffer caps: a chatty agent must not grow stdout/stderr without bound.
# When a buffer exceeds this ceiling, the MIDDLE is dropped (head + tail preserved)
# so the tail — where error messages live — is never silently discarded.
STREAM_BUFFER_MAX_CHARS = 200_000
STREAM_BUFFER_HEAD_CHARS = 40_000   # kept from the start
# tail = STREAM_BUFFER_MAX_CHARS - STREAM_BUFFER_HEAD_CHARS  (kept from the end)

# files_changed TTL: how long git-status porcelain results are cached on the Job object
# to avoid spawning a git process on every rapid agent_status poll.
FILES_CHANGED_TTL_SECONDS = 5.0

# Stderr patterns that signal a sandbox or permission rejection. These are what the
# streaming stderr watcher and the warnings[] scanner look for. Each tuple is a group
# of patterns; a match on any in the group triggers the same warning dedup key.
STDERR_WARNING_PATTERNS: list[tuple[str, ...]] = [
    ("permission requested", "auto-reject", "auto-rejecting"),
    ("permission denied", "denied", "not permitted", "permission error"),
    ("external_directory", "external directory", "outside the workspace"),
    ("sandbox", "sandbox violation", "sandbox policy"),
]

# Codex only auto-loads AGENTS.md as its project doc, not CLAUDE.md. Every codex
# prompt gets this preamble so the subagent reads the repo's CLAUDE.md guidance
# without the prompt author having to remember to ask for it.
CODEX_CLAUDE_MD_PREAMBLE = (
    "Orientation: this project's agent instructions live in CLAUDE.md, not AGENTS.md. "
    "If the working directory or its git root contains CLAUDE.md, read it first and obey it; "
    "also read any scoped CLAUDE-*.md files for subsystems you touch. "
    "Then proceed with the task below.\n\n"
)


# A subagent that doesn't know it can ask will just guess. Background-launched agents get
# this prepended so the channel is advertised at the point of work, not buried in a tool list.
# MCP server name as each client namespaces it. Both expose tools as mcp__<name>__<tool>,
# but codex NORMALIZES hyphens to underscores in the server name - registering it as
# "agent-bridge" there yields mcp__agent_bridge__ask_parent, so a preamble naming the
# hyphenated form sends the subagent hunting through ALL_TOOLS. Register codex under the
# underscore form directly and the name we advertise is the name it sees.
BRIDGE_MCP_NAME = "agent-bridge"
CODEX_BRIDGE_MCP_NAME = "agent_bridge"
ASK_PARENT_TOOL = f"mcp__{BRIDGE_MCP_NAME}__ask_parent"
CODEX_ASK_PARENT_TOOL = f"mcp__{CODEX_BRIDGE_MCP_NAME}__ask_parent"
CHECK_NOTES_TOOL = f"mcp__{BRIDGE_MCP_NAME}__check_notes"
CODEX_CHECK_NOTES_TOOL = f"mcp__{CODEX_BRIDGE_MCP_NAME}__check_notes"
RAISE_CONCERN_TOOL = f"mcp__{BRIDGE_MCP_NAME}__raise_concern"
CODEX_RAISE_CONCERN_TOOL = f"mcp__{CODEX_BRIDGE_MCP_NAME}__raise_concern"
# The child's channel back to whoever launched it: ask a blocking question, pick up
# a mid-flight note, flag something wrong. These are ALWAYS permitted, never gated
# on a caller's allowlist - see always_allowed_report_tools().
REPORT_CHANNEL_TOOLS = (ASK_PARENT_TOOL, CHECK_NOTES_TOOL, RAISE_CONCERN_TOOL)
# Where a subagent looks before spending tokens on a fresh helper.
WARM_AGENTS_TOOL = f"mcp__{BRIDGE_MCP_NAME}__warm_agents"
CODEX_WARM_AGENTS_TOOL = f"mcp__{CODEX_BRIDGE_MCP_NAME}__warm_agents"
KIMI_WARM_AGENTS_TOOL = WARM_AGENTS_TOOL
OPENCODE_WARM_AGENTS_TOOL = "warm_agents"
GROK_WARM_AGENTS_TOOL = "warm_agents"
# Opencode exposes MCP tools directly without mcp__ prefix (observed: `ask_parent` not `mcp__agent-bridge__ask_parent`)
OPENCODE_ASK_PARENT_TOOL = "ask_parent"
OPENCODE_CHECK_NOTES_TOOL = "check_notes"
OPENCODE_RAISE_CONCERN_TOOL = "raise_concern"
# Kimi Code uses mcp__<server>__<tool> similar to Claude (preserves hyphen)
KIMI_ASK_PARENT_TOOL = f"mcp__{BRIDGE_MCP_NAME}__ask_parent"
KIMI_CHECK_NOTES_TOOL = f"mcp__{BRIDGE_MCP_NAME}__check_notes"
KIMI_RAISE_CONCERN_TOOL = f"mcp__{BRIDGE_MCP_NAME}__raise_concern"
# Grok: docs say permissions use MCPTool(server__tool) without mcp__ prefix, but actual tool names discovered
# are direct like run_codex_agent (from logs: tools list shows direct names, no server prefix)
# However grok doctor says tool_count 28 with direct names. So use direct names like opencode.
# But per docs, MCPTool pattern is server__tool, not mcp__server__tool. Let's use direct for preamble
# to be safe, and also define both forms as fallback.
GROK_ASK_PARENT_TOOL = "ask_parent"
GROK_CHECK_NOTES_TOOL = "check_notes"
GROK_RAISE_CONCERN_TOOL = "raise_concern"
# Alternative with server prefix if needed
GROK_ASK_PARENT_TOOL_ALT = f"{BRIDGE_MCP_NAME}__ask_parent"
GROK_ASK_PARENT_TOOL_MCP = f"mcp__{BRIDGE_MCP_NAME}__ask_parent"
# Same guarantee as always_allowed_report_tools() gives claude, spelled for grok's
# `--allow <RULE>` flag. Because grok's tool naming is still unsettled (see above),
# every plausible form is allowed rather than betting on one: grok accepts unmatched
# rules without erroring (verified against the installed CLI), so the redundant
# entries cost nothing and the child keeps its channel home whichever form is live.
GROK_REPORT_CHANNEL_ALLOW_RULES = tuple(
    rule
    for tool in ("ask_parent", "check_notes", "raise_concern")
    for rule in (
        tool,
        f"{BRIDGE_MCP_NAME}__{tool}",
        f"mcp__{BRIDGE_MCP_NAME}__{tool}",
        f"MCPTool({BRIDGE_MCP_NAME}__{tool})",
    )
)

# The preamble is assembled from sections rather than sent whole. Every section here
# earned its place from an observed failure, but sending all of them on every launch
# dilutes the ones that matter for the task at hand - and worse, several describe
# capabilities a subagent may provably NOT have (a subagent at max depth cannot launch
# anything, so telling it how to delegate or escalate is the same "advertised a door
# that's locked" bug the codex registration gate fixed).
#
# Gating is structural where it can be - derived from depth and configuration, never
# guessed from the prompt text - and caller-controlled otherwise.
PREAMBLE_SECTIONS: dict[str, str] = {
    # Always present: the channel itself, and what it's for.
    "core": (
        "You are running as a background subagent, and the parent agent that launched you is "
        "reachable the whole time you work. If you hit a real fork in the road - an ambiguous "
        "requirement, a missing path or credential, a destructive or irreversible step you aren't "
        "sure is wanted, or a design decision that would be costly to undo - call the "
        "`{tool}` tool with a specific question and wait for the reply instead of "
        "guessing (if your tools are deferred, load it by that exact name first). Asking is "
        "expected, not a failure; a wrong assumption carried to completion costs far more than a "
        "question. Do not use it for anything you can settle yourself by reading the repo. If no "
        "answer arrives before the timeout, proceed on your best judgement and state plainly in "
        "your final report what you assumed and why.\n"
    ),
    # Only earns its place when guessing wrong could actually hurt.
    "abort": (
        "If guessing wrong would be destructive, irreversible, or expensive to undo, pass "
        "on_timeout='abort' - then an unanswered question stops that part of the work instead of "
        "resolving into a guess. Prefer this over inventing a safe-looking default for something "
        "the user would want to decide.\n"
    ),
    # Both of these presuppose the agent can launch children. At max depth it cannot.
    "escalate": (
        "If you launch subagents of your own and one asks YOU something you have no basis to "
        "answer, call `escalate_question` rather than making something up - a fabricated answer "
        "from you is worse than its own guess, because it carries your authority.\n"
    ),
    "delegate": (
        "You do not have to do all your own drudgery. You may launch up to {max_helpers} helper "
        "agent(s) of your own, and you must name the `model` explicitly when you do. Two "
        "directions are open to you. DOWNWARD, to a cheaper model, for the toil in your task - "
        "bulk mechanical edits, scanning long logs, reformatting, repetitive lookups. SIDEWAYS, "
        "to a peer at your own capability level - usually a different vendor's frontier model, "
        "such as Codex/Sol and Claude/Opus, which are peers of each other in both directions - "
        "when a genuinely independent look is worth more than another pass of your own: a second "
        "opinion on a design call, an adversarial read of a conclusion you are not sure of, or a "
        "check on work where your own blind spot is the risk. What you may NOT do is delegate "
        "upward to a more capable class than your own; that is escalation wearing the costume of "
        "offloading, and `escalate_question`/`ask_parent` is the honest way to do it. Keep the "
        "judgement, the design decisions, and the final report for yourself: you remain fully "
        "responsible for the work, including anything a helper got wrong, so check what comes "
        "back rather than passing it through unread - a peer's disagreement is evidence to weigh, "
        "not a verdict that overrides you. Delegating is a way to spend your attention where it "
        "matters, not a way to hand off accountability.\n"
        "Before launching anything fresh, call `{warm_tool}`. If an agent has already worked "
        "this problem it holds context you would otherwise pay to rebuild - the files it read, "
        "the layout it learned, the corrections it absorbed - and resuming it with "
        "`continue_*_agent` costs one turn instead of an entire re-education. Reuse the one "
        "whose task line and directory match yours, and re-brief rather than assume when the "
        "entry says 'stale' or 'crowded'. This has a limit: an agent carrying a lot of context, "
        "or pointed at a different problem than yours, is worse than a fresh one, because its "
        "old framing comes along with it. Warm and relevant beats fresh; warm and irrelevant "
        "does not.\n"
    ),
    # Useless on a one-shot task: there is no phase boundary at which to check.
    "notes": (
        "Your parent can watch your progress and leave you notes. Call `{notes_tool}` at the "
        "points where a correction would still be worth having: before any irreversible or "
        "destructive action, and when you finish one major phase and start the next. It is "
        "cheap and returns immediately when there is nothing waiting. Do NOT poll it in a "
        "loop or between every small step - checking constantly wastes your turns without "
        "learning anything.\n"
    ),
    "concerns": (
        "If you notice something wrong that is OUTSIDE what you were asked to do - a bug in code "
        "you were only reading past, a security or data-loss risk, a premise in your instructions "
        "you believe is mistaken, or output from one of your own helpers you do not trust - say so "
        "with `{concern_tool}`. It records the observation and does NOT block you; keep working. "
        "Noticing is not off-task, and 'nobody asked me' is not a reason to stay quiet - you may "
        "be the only one positioned to see it. Raise it as 'critical' only when acting on it "
        "matters more than finishing your task.\n"
    ),
    "standing": (
        "You are entitled to enough context to make good decisions. If you were handed a task "
        "without the purpose behind it, without knowing what your output feeds into, or without "
        "the judgement calls you're allowed to make on your own, that lack IS worth asking about - "
        "it is not a failing on your part to need it. Your report will be read by the agent that "
        "launched you and may reach the human; write it for someone who wasn't watching. Say what "
        "you are confident in, what you assumed, and what you could not determine, in your own "
        "words rather than a template.\n"
    ),
}

# Order is fixed regardless of which sections are selected, so the prompt prefix stays
# stable across launches that share a section set.
PREAMBLE_ORDER = ["core", "abort", "escalate", "delegate", "notes", "concerns", "standing"]
# What a caller gets when it asks for the smallest useful preamble.
MINIMAL_SECTIONS = ["core", "concerns", "standing"]


def child_can_spawn() -> bool:
    """Would a subagent launched from here be allowed to launch agents of its own?

    Its bridge server runs one level deeper than ours, and enforce_depth refuses once
    depth reaches the max - so a child at the ceiling cannot spawn, and must not be
    told it can.
    """
    return (current_depth() + 1) < max_depth()


def select_preamble_sections(
    sections: list[str] | None = None, multi_phase: bool = True
) -> list[str]:
    """Choose preamble sections. Structural facts win; callers refine the rest."""
    if sections is not None:
        unknown = [s for s in sections if s not in PREAMBLE_SECTIONS]
        if unknown:
            raise ValueError(
                f"unknown preamble section(s): {', '.join(unknown)}. "
                f"Valid: {', '.join(PREAMBLE_ORDER)}"
            )
        chosen = set(sections)
    else:
        chosen = set(PREAMBLE_ORDER)
        if not multi_phase:
            # No phase boundary to check at; the note would arrive after the job ended.
            chosen.discard("notes")

    # Structural gates apply even to an explicit caller list - never advertise a
    # capability the subagent provably does not have.
    if not child_can_spawn():
        chosen.discard("escalate")
        chosen.discard("delegate")
    if max_helpers() <= 0:
        chosen.discard("delegate")
    return [name for name in PREAMBLE_ORDER if name in chosen]


BRIDGE_SCRIPT = str(Path(__file__).resolve())
# Must exceed ask_parent's own timeout (default 600s) or codex kills the blocked call.
CODEX_BRIDGE_TOOL_TIMEOUT_SEC = 3600


def codex_bridge_overrides(job_id: str, model: str | None = None) -> list[str]:
    """`-c` flags that register THIS server into a codex subagent, so it can ask_parent.

    Codex only sees MCP servers declared in config.toml, and this repo's may not be there.
    Rather than require a global edit, each background launch injects the bridge for that
    invocation. `-c` merges with the user's existing servers rather than replacing them.

    Syntax rules verified against codex-cli 0.144.6:
      - table is `mcp_servers` (snake_case); `mcpServers` is SILENTLY IGNORED
      - hyphenated names must be BARE in a -c path - quoting embeds the quote characters
        in the server name rather than escaping it
      - values are TOML, not JSON; strings get explicit double quotes
    Env is passed explicitly because codex does not forward the parent's environment to
    MCP servers wholesale, and ask_parent addresses its question by AGENT_BRIDGE_JOB_ID.
    """
    # Codex does not forward the parent's environment to MCP servers, so every var the
    # child's bridge needs must be listed here. Ancestry is easy to forget and fails
    # quietly: questions still work, they just report depth=1 with an empty chain, so an
    # escalated question looks top-level and cannot be routed.
    ancestry = [j for j in (os.environ.get("AGENT_BRIDGE_ANCESTRY") or "").split(",") if j]
    here = os.environ.get("AGENT_BRIDGE_JOB_ID")
    if here:
        ancestry.append(here)
    env_pairs = ", ".join([
        f'AGENT_BRIDGE_JOB_ID="{job_id}"',
        f'AGENT_BRIDGE_DEPTH="{current_depth() + 1}"',
        'AGENT_BRIDGE_PARENT="codex"',
        f'AGENT_BRIDGE_MAX_DEPTH="{max_depth()}"',
        f'AGENT_BRIDGE_ANCESTRY="{",".join(ancestry)}"',
        *([f'AGENT_BRIDGE_MODEL="{model}"'] if model else []),
    ])
    # Inject under the SAME key config.toml uses (hyphenated). Codex merges -c overrides
    # into a matching entry but treats a different key as a SECOND server - and since it
    # normalizes both to `agent_bridge`, the collision makes it disambiguate with hash
    # suffixes (mcp__agent_bridge_529cc70a97db__...). Tool names then differ per launch
    # and the subagent has to grep ALL_TOOLS to find anything. Matching keys avoids it,
    # and codex's own normalization still yields the mcp__agent_bridge__* tool names.
    name = BRIDGE_MCP_NAME
    return [
        "-c", f'mcp_servers.{name}.command="{sys.executable}"',
        "-c", f'mcp_servers.{name}.args=["{BRIDGE_SCRIPT}"]',
        "-c", f"mcp_servers.{name}.env={{{env_pairs}}}",
        # We launch codex with --ask-for-approval never, under which an MCP tool call that
        # would need approval is DENIED outright ("user cancelled" in 0.0s) rather than
        # prompting. Without this the subagent can see ask_parent and never call it.
        # Valid modes are auto|prompt|writes|approve; "auto" defers to the approval policy
        # (so it still denies under never) - "approve" pre-approves this server's tools.
        "-c", f'mcp_servers.{name}.default_tools_approval_mode="approve"',
        # ask_parent blocks until the parent answers; codex's default tool_timeout_sec is
        # 60, which would kill the call long before a human-paced answer arrives.
        "-c", f"mcp_servers.{name}.tool_timeout_sec={CODEX_BRIDGE_TOOL_TIMEOUT_SEC}",
        "-c", f"mcp_servers.{name}.startup_timeout_sec=30",
    ]


def codex_has_bridge() -> bool:
    """Is agent-bridge registered as an MCP server for codex?

    Claude gets the bridge automatically from ~/.claude.json (user scope), but codex only
    sees it if ~/.codex/config.toml declares it. Telling a codex subagent it can call
    ask_parent when the tool isn't there just wastes its turns hunting for it, so the
    preamble is gated on this. Cheap substring check - no TOML parser in the stdlib for
    writing, and we only need to know whether the block exists.
    """
    config = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "config.toml"
    try:
        text = config.read_text(encoding="utf-8")
    except OSError:
        return False
    return "mcp_servers" in text and "agent_bridge_mcp.py" in text


def opencode_has_bridge() -> bool:
    """Is agent-bridge registered for opencode?

    Opencode stores MCP servers in ~/.config/opencode/opencode.json under mcp.
    We check that file for agent-bridge. If it's there, opencode subagents will
    see ask_parent etc. (opencode tool names are direct, not prefixed).
    """
    # Allow override via env
    config_path = Path(os.environ.get("OPENCODE_CONFIG", "~/.config/opencode/opencode.json")).expanduser()
    try:
        text = config_path.read_text(encoding="utf-8")
        # cheap check - look for agent-bridge anywhere and mcp block
        return "agent-bridge" in text and "agent_bridge_mcp.py" in text
    except OSError:
        return False


def kimi_has_bridge() -> bool:
    """Is agent-bridge registered for Kimi Code?

    Kimi stores MCP servers in ~/.kimi-code/mcp.json under mcpServers (per docs).
    """
    config_path = Path(os.environ.get("KIMI_CODE_HOME", "~/.kimi-code")).expanduser() / "mcp.json"
    try:
        text = config_path.read_text(encoding="utf-8")
        return "agent-bridge" in text and "agent_bridge_mcp.py" in text
    except OSError:
        return False


def grok_has_bridge() -> bool:
    """Is agent-bridge registered for Grok Code?

    Grok stores MCP servers in ~/.grok/config.toml as [mcp_servers.agent-bridge]
    plus inherits from ~/.claude.json. We check both.
    """
    # Check grok's own config.toml
    grok_config = Path(os.environ.get("GROK_HOME", "~/.grok")).expanduser() / "config.toml"
    try:
        text = grok_config.read_text(encoding="utf-8")
        if "agent-bridge" in text and "agent_bridge_mcp.py" in text:
            return True
    except OSError:
        pass
    # Check claude.json inheritance (grok reads it)
    claude_config = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser() / ".claude.json"
    # Actually Claude stores in ~/.claude.json, not in CLAUDE_CONFIG_DIR? Try both
    for p in [
        Path("~/.claude.json").expanduser(),
        Path("~/.claude/projects").expanduser().parent / ".." / ".claude.json",  # fallback
    ]:
        try:
            text = p.read_text(encoding="utf-8")
            if "agent-bridge" in text and "agent_bridge_mcp.py" in text:
                return True
        except OSError:
            continue
    # Also check ~/.claude.json directly via env
    try:
        text = Path(os.environ.get("CLAUDE_CONFIG", "~/.claude.json")).expanduser().read_text(encoding="utf-8")
        if "agent-bridge" in text and "agent_bridge_mcp.py" in text:
            return True
    except OSError:
        return False
    return False


def with_ask_parent_preamble(prompt: str, tool_name: str = ASK_PARENT_TOOL,
                             notes_tool: str = CHECK_NOTES_TOOL,
                             concern_tool: str = RAISE_CONCERN_TOOL,
                             warm_tool: str = WARM_AGENTS_TOOL,
                             sections: list[str] | None = None,
                             multi_phase: bool = True) -> str:
    """Advertise the parent channels. Background launches only, gated by section."""
    chosen = select_preamble_sections(sections, multi_phase)
    preamble = "".join(
        PREAMBLE_SECTIONS[name].format(
            tool=tool_name, notes_tool=notes_tool, max_helpers=max_helpers(),
            concern_tool=concern_tool, warm_tool=warm_tool)
        for name in chosen
    ) + "\n"
    if preamble.strip() in prompt:
        return prompt
    return preamble + prompt


def with_claude_md_preamble(prompt: str) -> str:
    """Prepend the CLAUDE.md orientation preamble unless it is already present."""
    if CODEX_CLAUDE_MD_PREAMBLE.strip() in prompt:
        return prompt
    return CODEX_CLAUDE_MD_PREAMBLE + prompt


@dataclass
class Job:
    id: str
    kind: str
    command: list[str]
    cwd: str
    timeout_seconds: int
    started_at: float
    # None for a job rehydrated from the on-disk roster after a server restart: the
    # process is long gone, but its SESSION still exists and is what continue_* needs.
    process: subprocess.Popen[str] | None = None
    stdout: str = ""
    stderr: str = ""
    # Cumulative chars discarded by the middle-truncation cap. Tracked here rather than
    # recomputed, because after the first truncation the buffer no longer knows how much
    # of the stream it has thrown away.
    stdout_dropped_chars: int = 0
    stderr_dropped_chars: int = 0
    returncode: int | None = None
    finished_at: float | None = None
    error: str | None = None
    commit_paths: list[str] = field(default_factory=list)
    commit_message: str | None = None
    commit: dict[str, Any] | None = None
    # Session/token visibility (additive; populated once known).
    session_id: str | None = None          # codex thread id / claude session id, for resume
    model: str | None = None               # model the subagent ran under, if known
    tokens: dict[str, Any] | None = None    # per-job token usage (input/output/total/...)
    resume: dict[str, Any] | None = None    # config needed to resume this job's session
    enriched: bool = False                 # token/session enrichment already attempted
    transcript_path: str | None = None     # cached live transcript/rollout, for peek_agent
    # Reuse bookkeeping: what this agent was put to work on, and how warm it is.
    task: str | None = None                # opening prompt, so a warm agent is identifiable
    turns: int = 1                         # 1 at launch, +1 per continue_*
    last_used_at: float | None = None      # last launch or continue, for staleness
    retired: bool = False                  # human said stop reusing this one
    retired_reason: str | None = None
    rehydrated: bool = False               # came back from the roster, not this process
    lock: threading.Lock = field(default_factory=threading.Lock)
    # Baseline git numstat captured at launch — so files_changed can report
    # insertions_since_launch rather than absolute numbers from an already-dirty repo.
    _launch_numstat: str | None = None     # raw output of `git diff --numstat` at launch

    @property
    def status(self) -> str:
        if self.returncode is None:
            return "running"
        if self.returncode == 0:
            return "succeeded"
        if self.returncode < 0:
            return "cancelled"
        return "failed"


jobs: dict[str, Job] = {}
jobs_lock = threading.Lock()


def log(message: str) -> None:
    print(f"[{SERVER_NAME}] {message}", file=sys.stderr, flush=True)


def json_rpc_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def json_rpc_error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def text_content(text: str) -> list[dict[str, str]]:
    return [{"type": "text", "text": text}]


def tool_response(payload: Any) -> dict[str, Any]:
    return {"content": text_content(json.dumps(payload, indent=2, sort_keys=True))}


def tool_error(message: str, payload: Any | None = None) -> dict[str, Any]:
    text = message
    if payload is not None:
        text = f"{message}\n{json.dumps(payload, indent=2, sort_keys=True)}"
    return {"isError": True, "content": text_content(text)}


def truncate_text(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    omitted = len(value) - max_chars
    return value[:max_chars] + f"\n\n[truncated {omitted} chars]"


def _cap_stream_buffer(text: str, already_dropped: int = 0) -> tuple[str, int]:
    """Truncate the MIDDLE of a stream buffer, preserving head + tail.

    The tail is where errors, warnings, and exit messages land — silently dropping it
    would defeat the purpose of streaming visibility. Head context is kept so the
    beginning of the output (model, session id, early messages) is still readable.

    Returns (capped_text, total_dropped). `already_dropped` carries the running total in,
    because the count CANNOT be recovered from the text itself: this is called once per
    line, so by the second call `text` is already truncated and `len(text) - MAX` measures
    only the newest line rather than everything lost so far. Reporting that per-call figure
    understates reality by orders of magnitude - a real 1.1MB job showed "221 chars
    truncated" against 905,000 actually dropped, which reads as "you have essentially all
    the output" when 82% of it is gone. A caller who trusts that number stops looking.
    """
    if len(text) <= STREAM_BUFFER_MAX_CHARS:
        return text, already_dropped
    tail_chars = STREAM_BUFFER_MAX_CHARS - STREAM_BUFFER_HEAD_CHARS
    total_dropped = already_dropped + (len(text) - STREAM_BUFFER_MAX_CHARS)
    marker = f"\n\n[... {total_dropped} chars truncated from middle ...]\n\n"
    # Account for the marker itself so the result fits within the cap
    usable_tail = tail_chars - len(marker)
    if usable_tail < 0:
        usable_tail = 0
    return text[:STREAM_BUFFER_HEAD_CHARS] + marker + text[-usable_tail:], total_dropped


def _scan_stderr_warnings(stderr: str) -> list[dict[str, Any]]:
    """Scan accumulated stderr for permission/rejection patterns.

    Returns a compact deduped list of warnings: the matched line (trimmed/truncated to
    200 chars), a count if repeated, and the pattern group that fired. Designed to help
    a parent notice sandbox auto-rejections without reading raw stderr.
    """
    if not stderr:
        return []
    lines = stderr.splitlines()
    seen: dict[str, dict[str, Any]] = {}  # keyed by (pattern_index, normalized_line)
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        for pattern_idx, pattern_group in enumerate(STDERR_WARNING_PATTERNS):
            matched = False
            for pat in pattern_group:
                if pat in stripped.lower():
                    matched = True
                    break
            if not matched:
                continue
            # Normalize: collapse whitespace, trim to 200 chars
            normalized = " ".join(stripped.split())[:200]
            key = (pattern_idx, normalized)
            if key in seen:
                seen[key]["count"] = seen[key].get("count", 1) + 1
            else:
                seen[key] = {
                    "line": normalized,
                    "count": 1,
                    "pattern_group_index": pattern_idx,
                }
    # Sort by pattern group (so related warnings cluster), then by first occurrence
    return sorted(seen.values(), key=lambda w: (w["pattern_group_index"], -w["count"]))


def _scan_stderr_auto_rejections(stderr: str) -> list[str]:
    """Extract distinct paths that were auto-rejected from stderr lines.

    Looks for lines matching patterns like 'auto-rejecting external_directory: /foo/bar'
    and returns the distinct rejected paths. Used by the streaming watcher to raise
    parent-side questions.
    """
    if not stderr:
        return []
    rejected: set[str] = set()
    for line in stderr.splitlines():
        stripped = line.strip()
        lower = stripped.lower()
        if not ("external_directory" in lower or "outside the workspace" in lower
                or ("permission" in lower and ("reject" in lower or "denied" in lower))):
            continue
        # Match paths: /absolute/paths, ~/relative, ../foo/bar
        for match in re.finditer(
            r'(?:/[\w./-]+|~[\w./-]*|\.\.[\w./-]*)', stripped
        ):
            path = match.group(0)
            if len(path) > 2:  # skip bare "/" or "./"
                rejected.add(path)
    return sorted(rejected)


def require_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"`{key}` is required and must be a non-empty string")
    return value


def optional_str(args: dict[str, Any], key: str, default: str | None = None) -> str | None:
    value = args.get(key, default)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"`{key}` must be a string")
    return value


def optional_bool(args: dict[str, Any], key: str, default: bool) -> bool:
    value = args.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"`{key}` must be a boolean")
    return value


def optional_int(args: dict[str, Any], key: str, default: int, minimum: int, maximum: int) -> int:
    value = args.get(key, default)
    if not isinstance(value, int):
        raise ValueError(f"`{key}` must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"`{key}` must be between {minimum} and {maximum}")
    return value


def optional_string_list(args: dict[str, Any], key: str) -> list[str]:
    value = args.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"`{key}` must be an array of strings")
    return value


def enum_value(args: dict[str, Any], key: str, default: str, allowed: set[str]) -> str:
    value = args.get(key, default)
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"`{key}` must be one of: {', '.join(sorted(allowed))}")
    return value


def _prepare_subagent_sandbox(
    args: dict[str, Any], cwd: str, prompt: str | None
) -> tuple[list[str], dict[str, Any]]:
    """Gather add_dirs, scan the prompt for out-of-sandbox paths, and auto-add STATE_DIR.

    Returns (final_add_dirs, sandbox_note_dict). The note dict has:
      - `auto_added_dirs`: dirs added by the prompt scan (empty if none)
      - `state_dir_added`: whether STATE_DIR was auto-added
      - `total_dirs`: final add_dirs count
      - `warning`: non-empty if limitations exist

    Called from each launch_* BEFORE build_*, so the widened add_dirs reach the command.
    """
    add_dirs = list(optional_string_list(args, "add_dirs"))
    cwd_resolved = str(Path(cwd).resolve())
    state_dir_resolved = str(STATE_DIR.resolve())

    # ---- STATE_DIR is not optional plumbing ----
    # The report channel (check_notes / ask_parent / raise_concern) lives on disk under
    # STATE_DIR. A subagent whose sandbox blocks that directory cannot ask, cannot read
    # notes, and cannot raise concerns, and none of that failure is visible to anyone.
    # So STATE_DIR is always added to the accessible set, without the caller asking.
    state_dir_already = any(
        str(Path(d).expanduser().resolve()) == state_dir_resolved
        for d in add_dirs
    )
    if not state_dir_already and cwd_resolved != state_dir_resolved:
        add_dirs.append(str(STATE_DIR))

    # ---- Out-of-sandbox path scan (item 5) ----
    to_add: list[str] = []
    if prompt:
        to_add, _skipped = _scan_prompt_for_outside_paths(prompt, cwd, add_dirs)
    for d in to_add:
        if d not in add_dirs:
            add_dirs.append(d)

    note: dict[str, Any] = {
        "auto_added_dirs": to_add,
        "state_dir_added": not state_dir_already and cwd_resolved != state_dir_resolved,
        "total_dirs": len(add_dirs),
    }
    if to_add:
        note["warning"] = (
            f"Prompt references {len(to_add)} path(s) outside cwd. "
            f"These were added to the subagent's accessible directories "
            f"to prevent sandbox auto-rejection errors: {', '.join(to_add)}."
        )
    return add_dirs, note


def resolve_cwd(args: dict[str, Any]) -> str:
    raw_cwd = optional_str(args, "cwd", os.getcwd())
    assert raw_cwd is not None
    cwd = str(Path(raw_cwd).expanduser().resolve())
    if not Path(cwd).is_dir():
        raise ValueError(f"`cwd` does not exist or is not a directory: {cwd}")
    return cwd


# ---------------------------------------------------------------------------
# Out-of-sandbox path scanner
#
# Scans a prompt text for path-like tokens that resolve outside cwd and are not
# covered by add_dirs. Catches the "permission requested: external_directory"
# error before it costs a launch. Auto-adds the directory to add_dirs when
# detected, with a loud note in the response — a silent add that widens the
# sandbox beyond what the caller intended is its own hazard.
# ---------------------------------------------------------------------------

_OUT_OF_SANDBOX_PATH_RE = re.compile(
    r'(?:^|\s|["\'(])((?:~|\.\.(?:/\.\.)*)/[\w.\-/]*|/[\w.\-/]+)(?:[/:\s"\'.;!?)]|$)'
)

_BAD_PATH_RE = re.compile(
    r'(?:'
        r'https?://|'       # URLs
        r'\d+\.\d+|'        # version numbers like 2.0.1
        r'\.{1,2}$|'        # bare . or ..
        r'^/dev/|'          # /dev/null, /dev/stdin, etc.
        r'^/proc/|'         # /proc/...
        r'^/sys/|'          # /sys/...
        r'^/tmp/'           # /tmp/... is fair to auto-add
    r')'
)


def _scan_prompt_for_outside_paths(
    prompt: str, cwd: str, add_dirs: list[str]
) -> tuple[list[str], list[str]]:
    """Return (paths_to_add, false_positives_skipped).

    Scans the prompt for path-like tokens that resolve outside cwd. Returns a list of
    directories that should be added to add_dirs. The false_positives_skipped list is
    informational only — it shows what we deliberately ignored.
    """
    if not prompt:
        return [], []
    resolved_cwd = Path(cwd).resolve()
    resolved_add_dirs = {str(Path(d).expanduser().resolve()) for d in add_dirs}
    resolved_add_dirs.add(str(resolved_cwd))

    to_add: list[str] = []
    skipped: list[str] = []

    for match in _OUT_OF_SANDBOX_PATH_RE.finditer(prompt):
        raw_path = match.group(1)
        if not raw_path or len(raw_path) < 2:
            continue
        if _BAD_PATH_RE.search(raw_path):
            continue
        # Skip paths that look like they're inside cwd (e.g., "see src/foo.py")
        if not raw_path.startswith("/") and not raw_path.startswith("~") and not raw_path.startswith("."):
            continue
        try:
            resolved = Path(raw_path).expanduser().resolve()
        except (OSError, RuntimeError):
            skipped.append(raw_path)
            continue
        if not resolved.exists():
            continue  # only flag paths that actually exist
        resolved_str = str(resolved)
        # Check if this path (or its parent chain) is already covered
        parent = resolved
        already_covered = False
        while parent != parent.parent:
            if str(parent) in resolved_add_dirs:
                already_covered = True
                break
            parent = parent.parent
        if already_covered:
            continue
        # Find the closest existing parent directory to add
        # (we add the containing directory, not the file itself)
        dir_to_add = resolved.parent if resolved.is_file() else resolved
        dir_str = str(dir_to_add)
        if dir_str not in resolved_add_dirs and dir_str not in to_add:
            to_add.append(dir_str)
    return to_add, skipped


def opencode_permission_env(add_dirs: list[str], cwd: str | None = None) -> str | None:
    """Build OPENCODE_CONFIG_CONTENT granting opencode access to `add_dirs`.

    Opencode is the one client with no `--add-dir` flag: `opencode run --dir` sets the
    working directory and nothing else, so every path outside it is refused by opencode's
    own permission layer with

        permission requested: external_directory (<path>/*); auto-rejecting

    which lands in stderr, is not an error, and does not stop the run. A subagent hitting
    that on STATE_DIR loses check_notes, ask_parent and raise_concern in one go - the
    report channel goes silently dead while the job looks perfectly healthy. That is not
    hypothetical: it is how a mid-flight correction to this very file was lost.

    Opencode's config does expose the knob the CLI does not - `permission.external_directory`
    maps a glob to allow/ask/deny - and OPENCODE_CONFIG_CONTENT is MERGED over the resolved
    config rather than replacing it (verified: `opencode debug config` with the variable set
    still shows the user's providers, model and mcp servers, with permission added). So the
    grant can be injected per-child, scoped to exactly the directories this job was given,
    without touching the user's own config file.

    Deliberately narrow: only the listed directories are allowed, never a blanket rule.
    `--auto` would also silence the rejection, but by auto-approving EVERYTHING the agent
    asks for, which trades a visible failure for an invisible one.
    """
    if not add_dirs:
        return None
    # Preserve anything the parent already set rather than clobbering it - the value is a
    # general config channel and may legitimately carry unrelated keys.
    existing: dict[str, Any] = {}
    raw = os.environ.get("OPENCODE_CONFIG_CONTENT")
    if raw:
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                existing = loaded
        except (ValueError, TypeError):
            # Malformed inherited value: ignore it rather than propagate a broken config.
            existing = {}

    permission = dict(existing.get("permission") or {})
    external = permission.get("external_directory")
    # The schema allows a bare "allow"/"ask"/"deny" string here as well as a glob map.
    # A pre-existing string is a broader policy than anything we would add, so leave it.
    if isinstance(external, str):
        return json.dumps(existing) if raw else None
    external = dict(external or {})
    # SPACED-PATH TRAP (2026-08-17): opencode derives the permission SUBJECT from
    # shell tokens in the child's own commands, so a path typed as
    # `Astro\ Backups` arrives with a literal backslash and never matches the
    # unescaped glob. Worse, a spaced CWD makes the child's own repo look
    # external the moment it shells `cd` with an escaped path: two lanes died in
    # seconds this way while their jobs reported success. Grant BOTH spellings
    # of every directory, and always include the cwd when it contains a space so
    # a job can never be locked out of its own tree.
    grant_dirs = list(add_dirs)
    if cwd and " " in cwd and cwd not in grant_dirs:
        grant_dirs.append(cwd)
    for directory in grant_dirs:
        resolved = str(Path(directory).expanduser().resolve())
        # `/**` rather than `/*`: the rejection is raised for nested paths too
        # (questions/, notes/, concerns/, roster/ all live under STATE_DIR).
        external[f"{resolved}/**"] = "allow"
        if " " in resolved:
            external[f"{resolved.replace(' ', chr(92) + ' ')}/**"] = "allow"
    permission["external_directory"] = external
    existing["permission"] = permission
    return json.dumps(existing)


def child_env(kind: str, job_id: str | None = None, model: str | None = None,
              add_dirs: list[str] | None = None, cwd: str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    depth = current_depth()
    env["AGENT_BRIDGE_DEPTH"] = str(depth + 1)
    env["AGENT_BRIDGE_PARENT"] = kind
    # Record the launcher's own job id (empty at top level) BEFORE overwriting it, so a
    # question carries the chain it came from. Without this an escalated question from a
    # depth-2 agent is unroutable: you can see it, but not who is supposed to answer it.
    ancestry = [j for j in (os.environ.get("AGENT_BRIDGE_ANCESTRY") or "").split(",") if j]
    here = os.environ.get("AGENT_BRIDGE_JOB_ID")
    if here:
        ancestry.append(here)
    env["AGENT_BRIDGE_ANCESTRY"] = ",".join(ancestry)

    # Only BACKGROUND jobs get a job id, and it is what gates ask_parent: a synchronous
    # run_* agent must not be able to block on a question its parent cannot answer.
    if job_id:
        env["AGENT_BRIDGE_JOB_ID"] = job_id
    else:
        env.pop("AGENT_BRIDGE_JOB_ID", None)
    # The child needs its own model to know which tiers are BELOW it when delegating.
    if model:
        env["AGENT_BRIDGE_MODEL"] = model
    else:
        env.pop("AGENT_BRIDGE_MODEL", None)

    # Every other client takes its accessible directories as a CLI flag, wired up in the
    # matching build_* function. Opencode has no such flag, so its grant is an env var -
    # see opencode_permission_env for why this is the only route.
    if kind == "opencode":
        config_content = opencode_permission_env(add_dirs or [], cwd=cwd)
        if config_content:
            env["OPENCODE_CONFIG_CONTENT"] = config_content
    return env


def current_depth() -> int:
    raw = os.environ.get("AGENT_BRIDGE_DEPTH", "0")
    try:
        return int(raw)
    except ValueError:
        return 0


def max_depth() -> int:
    raw = os.environ.get("AGENT_BRIDGE_MAX_DEPTH", str(DEFAULT_MAX_DEPTH))
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_MAX_DEPTH


def enforce_depth() -> None:
    depth = current_depth()
    allowed = max_depth()
    if depth >= allowed:
        raise RuntimeError(
            f"agent bridge recursion limit reached: depth={depth}, max={allowed}. "
            "Increase AGENT_BRIDGE_MAX_DEPTH only if you intentionally want deeper handoffs."
        )


# ---------------------------------------------------------------------------
# Parent <-> child question channel
#
# A subagent runs in its OWN agent-bridge server process (claude/codex each spawn
# their own stdio server), so the parent's in-memory `jobs` dict is invisible to it.
# The channel therefore has to be on disk. One JSON file per question, written
# atomically via os.replace, polled by the asker.
#
# Inspired by dvcrn/mcp-server-subagent, which pioneered subagent->parent questions.
# ---------------------------------------------------------------------------

STATE_DIR = Path(os.environ.get("AGENT_BRIDGE_STATE_DIR", "~/.agent-bridge")).expanduser()
QUESTIONS_DIR = STATE_DIR / "questions"
QUESTION_POLL_SECONDS = 1.5


def _questions_dir() -> Path:
    QUESTIONS_DIR.mkdir(parents=True, exist_ok=True)
    return QUESTIONS_DIR


def _write_question(record: dict[str, Any]) -> None:
    """Atomically write a question record: temp file in the same dir, then os.replace."""
    target = _questions_dir() / f"{record['question_id']}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, target)


def _read_question(question_id: str) -> dict[str, Any] | None:
    path = _questions_dir() / f"{question_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _iter_questions() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        for path in sorted(_questions_dir().glob("*.json")):
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
    except OSError:
        return out
    return out


def pending_questions_for(job_id: str) -> list[dict[str, Any]]:
    return [q for q in _iter_questions() if q.get("job_id") == job_id and q.get("status") == "pending"]


def ask_parent(args: dict[str, Any]) -> dict[str, Any]:
    """Child-side: ask the launching parent a question and BLOCK until it answers.

    Only meaningful for agents started with launch_* (background). A run_* agent runs
    synchronously, so the parent is blocked inside run_command and physically cannot
    answer - that would deadlock. Those jobs get no AGENT_BRIDGE_JOB_ID, so we fail
    fast with an explanation instead of hanging until timeout.
    """
    question = require_str(args, "question")
    context = optional_str(args, "context") or ""
    timeout_seconds = optional_int(args, "timeout_seconds", 600, 5, 24 * 60 * 60)
    # "proceed" means the question is advisory - guessing is acceptable if nobody answers.
    # "abort" means it is load-bearing: for a destructive or irreversible step, silently
    # picking an interpretation is worse than doing nothing at all.
    on_timeout = enum_value(args, "on_timeout", "proceed", {"proceed", "abort"})

    job_id = os.environ.get("AGENT_BRIDGE_JOB_ID")
    if not job_id:
        raise ValueError(
            "ask_parent is only available to a subagent launched in the BACKGROUND "
            "(launch_claude_agent / launch_codex_agent). This process has no "
            "AGENT_BRIDGE_JOB_ID, meaning it is either a top-level agent or was started "
            "with run_* (synchronous), where the parent is blocked and could never answer."
        )

    question_id = str(uuid.uuid4())
    ancestry = [j for j in (os.environ.get("AGENT_BRIDGE_ANCESTRY") or "").split(",") if j]
    record = {
        "question_id": question_id,
        "job_id": job_id,
        "from_kind": os.environ.get("AGENT_BRIDGE_PARENT", "unknown"),
        "question": question,
        "context": context,
        "status": "pending",
        "asked_at": time.time(),
        "answer": None,
        "answered_at": None,
        "on_timeout": on_timeout,
        # Chain of job ids from the top-level agent down to this one's launcher.
        "ancestry": ancestry,
        "depth": len(ancestry) + 1,
        # Set by escalate_question when an intermediate parent cannot answer either.
        "escalated": False,
        "escalation_notes": [],
    }
    _write_question(record)
    log(f"question {question_id} from job {job_id}: {question[:80]}")

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        time.sleep(QUESTION_POLL_SECONDS)
        current = _read_question(question_id)
        if current and current.get("status") == "answered":
            return tool_response(
                {
                    "question_id": question_id,
                    "status": "answered",
                    "answer": current.get("answer"),
                    "escalated": current.get("escalated", False),
                    "waited_seconds": round(time.time() - record["asked_at"], 1),
                }
            )

    record["status"] = "timeout"
    _write_question(record)
    if on_timeout == "abort":
        return tool_error(
            f"NO ANSWER within {timeout_seconds}s, and you marked this question as "
            "blocking (on_timeout='abort'). Do NOT guess. Stop work on the part of the "
            "task that depends on this and report clearly: what you asked, that nobody "
            "answered, and what remains undone. Complete any independent parts of the "
            "task normally.",
            {"question_id": question_id, "status": "timeout", "on_timeout": "abort"},
        )
    return tool_error(
        f"no answer within {timeout_seconds}s - the parent may not have checked "
        f"pending_questions. Proceed using your best judgement and note the assumption "
        f"in your final report.",
        {"question_id": question_id, "status": "timeout", "on_timeout": "proceed"},
    )


# ---------------------------------------------------------------------------
# Concerns: see something, say something.
#
# Distinct from ask_parent. A question BLOCKS because the agent needs an answer to
# proceed. A concern does not block - the agent noticed something outside its
# assigned task (a bug in code it was only passing through, a security problem, a
# premise it believes is wrong, a helper's output it doesn't trust) and keeps
# working. Without a channel, that observation lands in a final report nobody
# re-reads, or is dropped as "not my task".
# ---------------------------------------------------------------------------

CONCERNS_DIR = STATE_DIR / "concerns"


def _concerns_dir() -> Path:
    CONCERNS_DIR.mkdir(parents=True, exist_ok=True)
    return CONCERNS_DIR


def _iter_concerns() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        for path in sorted(_concerns_dir().glob("*.json")):
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
    except OSError:
        return out
    return out


def concerns_for(job_id: str) -> list[dict[str, Any]]:
    return [c for c in _iter_concerns() if c.get("job_id") == job_id]


def raise_concern(args: dict[str, Any]) -> dict[str, Any]:
    """Subagent-side: flag something worth knowing WITHOUT blocking on a reply."""
    concern = require_str(args, "concern")
    severity = enum_value(args, "severity", "warning", {"info", "warning", "critical"})
    evidence = optional_str(args, "evidence") or ""
    job_id = os.environ.get("AGENT_BRIDGE_JOB_ID")
    if not job_id:
        raise ValueError(
            "raise_concern is only available to a background-launched subagent "
            "(launch_claude_agent / launch_codex_agent)."
        )
    concern_id = str(uuid.uuid4())
    record = {
        "concern_id": concern_id,
        "job_id": job_id,
        "from_kind": os.environ.get("AGENT_BRIDGE_PARENT", "unknown"),
        "from_model": os.environ.get("AGENT_BRIDGE_MODEL"),
        "concern": concern,
        "evidence": evidence,
        "severity": severity,
        "raised_at": time.time(),
        "ancestry": [j for j in (os.environ.get("AGENT_BRIDGE_ANCESTRY") or "").split(",") if j],
    }
    target = _concerns_dir() / f"{concern_id}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, target)
    log(f"concern [{severity}] from job {job_id}: {concern[:80]}")
    return tool_response({
        "concern_id": concern_id,
        "severity": severity,
        "recorded": True,
        "note": (
            "Recorded and surfaced to your parent - you are NOT blocked, carry on with your "
            "task. Mention it in your final report too. If you cannot safely continue until "
            "someone responds, that is a question, not a concern: use ask_parent with "
            "on_timeout='abort' instead."
        ),
    })


def list_concerns(args: dict[str, Any]) -> dict[str, Any]:
    """Parent-side: read concerns raised by subagents."""
    job_id = optional_str(args, "job_id")
    min_severity = enum_value(args, "min_severity", "info", {"info", "warning", "critical"})
    order = {"info": 0, "warning": 1, "critical": 2}
    out = []
    for c in _iter_concerns():
        if job_id and c.get("job_id") != job_id:
            continue
        if order.get(c.get("severity", "info"), 0) < order[min_severity]:
            continue
        out.append(c)
    out.sort(key=lambda c: (-order.get(c.get("severity", "info"), 0), c.get("raised_at") or 0))
    return tool_response({
        "concerns": out,
        "count": len(out),
        "critical_count": sum(1 for c in out if c.get("severity") == "critical"),
    })


def pending_questions(args: dict[str, Any]) -> dict[str, Any]:
    """Parent-side: list questions subagents are currently blocked on."""
    job_id = optional_str(args, "job_id")
    include_answered = optional_bool(args, "include_answered", False)
    escalated_only = optional_bool(args, "escalated_only", False)
    out = []
    for q in _iter_questions():
        if job_id and q.get("job_id") != job_id:
            continue
        if not include_answered and q.get("status") != "pending":
            continue
        if escalated_only and not q.get("escalated"):
            continue
        item = dict(q)
        if item.get("asked_at"):
            item["waiting_seconds"] = round(time.time() - item["asked_at"], 1)
        out.append(item)
    # Escalated questions first: they have already cost one agent its turn, and something
    # above could not answer them, so they are the ones most likely to need a human.
    out.sort(key=lambda q: (not q.get("escalated"), q.get("asked_at") or 0))
    escalated = sum(1 for q in out if q.get("escalated"))
    payload: dict[str, Any] = {"questions": out, "count": len(out)}
    if escalated:
        payload["escalated_count"] = escalated
        payload["action_required"] = (
            f"{escalated} question(s) were escalated - an intermediate agent could not "
            "answer them. If you cannot either, ask the human rather than guessing."
        )
    return tool_response(payload)


def escalate_question(args: dict[str, Any]) -> dict[str, Any]:
    """Pass a subagent's question up the chain when you cannot answer it either.

    The failure this exists to prevent: a mid-chain agent receives a question it has no
    basis to answer, invents a plausible answer to avoid stalling, and the subagent acts
    on it. That is strictly worse than the subagent guessing, because the fabricated
    answer carries the authority of the parent. Escalating keeps the question alive and
    marks it for whoever is actually in a position to decide - ultimately the human.
    """
    question_id = require_str(args, "question_id")
    note = optional_str(args, "note") or ""
    record = _read_question(question_id)
    if record is None:
        raise ValueError(f"unknown question_id: {question_id}")
    if record.get("status") != "pending":
        raise ValueError(
            f"question {question_id} is {record.get('status')}, not pending - nothing to escalate"
        )
    record["escalated"] = True
    notes = list(record.get("escalation_notes") or [])
    notes.append({
        "from_job": os.environ.get("AGENT_BRIDGE_JOB_ID") or "top-level",
        "note": note,
        "at": time.time(),
    })
    record["escalation_notes"] = notes
    _write_question(record)
    return tool_response({
        "question_id": question_id,
        "status": "pending",
        "escalated": True,
        # Learned the hard way: without this, an escalating agent waits for the answer to
        # come back to IT, never sees one (the answer goes straight to the original asker),
        # concludes the chain failed, and reports the eventually-correct result as
        # fabricated. Escalation then makes the outcome worse than not escalating.
        "do_not_relay": (
            "You do NOT relay this answer and you must NOT call answer_agent for it. "
            "Whoever answers unblocks the original subagent DIRECTLY - the answer will "
            "never be handed back to you."
        ),
        "how_to_check": (
            f"To see the resolution, call pending_questions(job_id='{record.get('job_id')}', "
            "include_answered=true) and read the 'answer' field, or agent_status on that "
            "job. Seeing no answer addressed to you is EXPECTED and does not mean the "
            "chain failed - check before reporting failure."
        ),
        "question": record.get("question"),
        "asked_by_job": record.get("job_id"),
        "depth": record.get("depth"),
    })


def answer_agent(args: dict[str, Any]) -> dict[str, Any]:
    """Parent-side: answer a blocked subagent's question, unblocking it."""
    question_id = require_str(args, "question_id")
    answer = require_str(args, "answer")
    record = _read_question(question_id)
    if record is None:
        raise ValueError(f"unknown question_id: {question_id}")
    if record.get("status") == "answered":
        raise ValueError(f"question {question_id} was already answered")
    if record.get("status") == "timeout":
        raise ValueError(
            f"question {question_id} timed out - the subagent stopped waiting and moved on. "
            "Answering now would have no effect."
        )
    record["status"] = "answered"
    record["answer"] = answer
    record["answered_at"] = time.time()
    _write_question(record)
    return tool_response(
        {
            "question_id": question_id,
            "job_id": record.get("job_id"),
            "status": "answered",
            "note": "subagent unblocks within ~2s",
        }
    )


# ---------------------------------------------------------------------------
# Supervision notes: parent -> running subagent, one way.
#
# A launched subagent is a one-shot process with stdin at DEVNULL, so there is no
# channel to interrupt it. The only workable shape is a mailbox the agent chooses to
# read. Timing is the whole design problem: polling on a timer burns calls to learn
# nothing, so the preamble ties checks to the moments a correction is worth having -
# before an irreversible action, and at phase boundaries.
#
# EXPERIMENTAL. Whether agents check at useful moments is an empirical question.
# ---------------------------------------------------------------------------

NOTES_DIR = STATE_DIR / "notes"


def _notes_path(job_id: str) -> Path:
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    return NOTES_DIR / f"{job_id}.json"


def _read_notes(job_id: str) -> list[dict[str, Any]]:
    try:
        return json.loads(_notes_path(job_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def _write_notes(job_id: str, notes: list[dict[str, Any]]) -> None:
    target = _notes_path(job_id)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(notes, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, target)


def send_note(args: dict[str, Any]) -> dict[str, Any]:
    """Parent-side: leave a note for a RUNNING subagent to pick up at its next check."""
    job_id = require_str(args, "job_id")
    note = require_str(args, "note")
    job = get_job(job_id)
    with job.lock:
        status = job.status
    if status != "running":
        raise ValueError(
            f"job {job_id} is {status}, not running - it will never read this note. "
            "Use continue_claude_agent / continue_codex_agent to send a follow-up turn "
            "to a finished job instead."
        )
    notes = _read_notes(job_id)
    notes.append({
        "note_id": str(uuid.uuid4()),
        "note": note,
        "sent_at": time.time(),
        "read_at": None,
    })
    _write_notes(job_id, notes)
    return tool_response({
        "job_id": job_id,
        "queued": True,
        "unread": sum(1 for n in notes if not n.get("read_at")),
        "note": (
            "Delivered when the subagent next calls check_notes - it is told to do that "
            "before irreversible actions and at phase boundaries, NOT continuously. A "
            "note is not an interrupt; if the agent is mid-step it will not see this "
            "until that step ends, and it may never see it if it finishes first."
        ),
    })


def check_notes(args: dict[str, Any]) -> dict[str, Any]:
    """Subagent-side: read any notes the parent has left. Non-blocking."""
    job_id = os.environ.get("AGENT_BRIDGE_JOB_ID")
    if not job_id:
        raise ValueError(
            "check_notes is only available to a background-launched subagent "
            "(launch_claude_agent / launch_codex_agent)."
        )
    notes = _read_notes(job_id)
    unread = [n for n in notes if not n.get("read_at")]
    if not unread:
        return tool_response({"notes": [], "count": 0})
    now = time.time()
    for n in notes:
        if not n.get("read_at"):
            n["read_at"] = now
    _write_notes(job_id, notes)
    return tool_response({
        "notes": [{"note": n["note"], "sent_at": n["sent_at"]} for n in unread],
        "count": len(unread),
        "instruction": (
            "Your parent is watching your progress and sent this. Treat it as a "
            "correction that supersedes your current plan where they conflict - it was "
            "written with visibility into what you have actually been doing. If it "
            "contradicts your original instructions, follow the note and say so in your "
            "final report."
        ),
    })


# ---------------------------------------------------------------------------
# Delegation: a subagent may hand work to a model at its own level or below.
#
# A depth-limited subagent would otherwise have to do all its own toil while
# everything above it delegates freely. It can launch helpers of its own - at an
# equal or lower capability class, and only a couple - so the affordance can't be
# used to route real work UP to a better model or to fan out without bound. The
# delegating agent stays accountable for the result either way.
#
# Sideways delegation (equal class, usually a different vendor) is deliberately
# allowed: a second opinion from a peer is the whole point of a bridge between
# frontier models, and "Sol reviews what Opus wrote" is not an escalation. It is
# what PEER_MODELS below exists to guarantee.
# ---------------------------------------------------------------------------

# Most capable first. Claude's ladder is static (see the claude-api skill for the
# current lineup); codex's is read from its own cache, which already lists models
# in descending capability, so it doesn't rot when the lineup changes.
CLAUDE_MODEL_TIERS = [
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
]
CODEX_MODEL_TIERS_FALLBACK = [
    "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4", "gpt-5.4-mini",
]
# Opencode can run many providers; we order by known capability - meta's spark at top,
# then heavier opencode models, then free lightweight ones. The direct, paid DeepSeek
# Flash endpoint is listed separately from OpenCode's free proxy model so callers can
# select it unambiguously. Unknown models still work because enforce_delegation allows
# unknown own_rank but checks requested.
OPENCODE_MODEL_TIERS = [
    "meta/muse-spark-1.1",
    "opencode/big-pickle",
    "anthropic/claude-opus-4",
    "anthropic/claude-sonnet-4",
    "openai/gpt-5",
    "openai/gpt-4.1",
    "deepseek/deepseek-v4-flash",
    "opencode/deepseek-v4-flash-free",
    "opencode/laguna-s-2.1-free",
    "opencode/ling-3.0-flash-free",
    "opencode/mimo-v2.5-free",
    "opencode/nemotron-3-ultra-free",
    "opencode/north-mini-code-free",
    "anthropic/claude-haiku-4",
]
# Kimi Code models (k3 is flagship, then k2.5, kimi-for-coding family, etc)
KIMI_MODEL_TIERS = [
    "kimi-code/k3",
    "kimi-code/kimi-for-coding",
    "kimi-code/kimi-for-coding-highspeed",
    "kimi-code/kimi-k2.5",
    "kimi/k2",
    "kimi/k2-thinking",
    "moonshot/kimi-k2",
    "anthropic/claude-opus-4",
    "anthropic/claude-sonnet-4",
    "openai/gpt-5",
    "openai/gpt-4.1",
    "anthropic/claude-haiku-4",
]
# Grok models (grok-4.5 flagship, plus older)
GROK_MODEL_TIERS = [
    "grok-4.5",
    "grok-4.5-build-free",
    "grok-4",
    "grok-3",
    "grok-3-mini",
    "anthropic/claude-opus-4",
    "anthropic/claude-sonnet-4",
    "openai/gpt-5",
    "openai/gpt-4.1",
    "anthropic/claude-haiku-4",
]
DEFAULT_MAX_HELPERS = 2

# ---------------------------------------------------------------------------
# Cross-vendor capability classes, most capable first.
#
# Delegation compares CLASSES, not ladder positions. Two ladders' indices are not
# commensurable - index 0 of the codex ladder is not "the same capability as"
# index 0 of opencode's - so the old rank<=rank check silently blocked every
# cross-vendor peer handoff (claude-opus rank 3 vs gpt-5.6-sol rank 0 reads as an
# escalation) while waving through nonsense in the other direction. Classes are
# coarse on purpose: the question is only "is this an equal or a lesser model",
# which is all the delegation rule needs to decide.
# ---------------------------------------------------------------------------
CAPABILITY_CLASSES = ["apex", "frontier", "workhorse", "light"]
CAPABILITY_CLASS_MEMBERS: dict[str, list[str]] = {
    # Priced and positioned above the frontier tier - not a peer of it.
    "apex": [
        "claude-fable-5",
        "claude-mythos-5",
    ],
    # Each vendor's flagship. These are peers of each other; see PEER_MODELS.
    "frontier": [
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "anthropic/claude-opus-4",
        "gpt-5.6-sol",
        "gpt-5.5",
        "meta/muse-spark-1.1",
        "opencode/big-pickle",
        "kimi-code/k3",
        "grok-4.5",
    ],
    # Everyday work: capable, cheaper, not the flagship.
    "workhorse": [
        "claude-sonnet-5",
        "claude-sonnet-4-6",
        "anthropic/claude-sonnet-4",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.4",
        "openai/gpt-5",
        "kimi-code/kimi-for-coding",
        "kimi-code/kimi-k2.5",
        "kimi/k2",
        "kimi/k2-thinking",
        "moonshot/kimi-k2",
        "grok-4",
        "grok-3",
    ],
    # Drudgery tier: bulk edits, log scanning, reformatting, repetitive lookups.
    "light": [
        "claude-haiku-4-5",
        "anthropic/claude-haiku-4",
        "gpt-5.4-mini",
        "openai/gpt-4.1",
        "kimi-code/kimi-for-coding-highspeed",
        "grok-3-mini",
        "grok-4.5-build-free",
        "deepseek/deepseek-v4-flash",
        "opencode/deepseek-v4-flash-free",
        "opencode/laguna-s-2.1-free",
        "opencode/ling-3.0-flash-free",
        "opencode/mimo-v2.5-free",
        "opencode/nemotron-3-ultra-free",
        "opencode/north-mini-code-free",
    ],
}

# Fallback for a model that isn't in the table - matched against the normalized
# name, first hit wins. Order matters: "light" is checked first so grok-3-mini
# lands there rather than in workhorse on the "grok-3" substring, and
# kimi-for-coding-highspeed doesn't get pulled up by "kimi-for-coding".
CAPABILITY_CLASS_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("light", ("-mini", "-nano", "-free", "haiku", "flash", "highspeed", "gpt-4.")),
    ("apex", ("fable", "mythos")),
    ("frontier", ("opus", "-sol", "muse-spark", "big-pickle", "grok-4.5")),
    ("workhorse", ("sonnet", "-terra", "-luna", "gpt-5", "kimi", "grok-")),
]

# Models the human has declared eligible for bidirectional delegation, so handoffs
# between them are allowed in BOTH directions no matter how the class table ranks
# them. This is a routing permission, not a claim that the models have identical
# capabilities. Keeping it explicit means a lineup change cannot quietly block a
# deliberate cross-vendor review loop.
PEER_MODELS: list[set[str]] = [
    {
        "gpt-5.6-sol",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
    },
    {
        # Direct paid DeepSeek API through OpenCode <-> Claude Code Opus.
        # The free OpenCode-hosted DeepSeek Flash model is deliberately excluded.
        "deepseek/deepseek-v4-flash",
        "opus",  # Claude Code's supported short model selector.
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
    },
]

_helpers_launched = 0
_helpers_lock = threading.Lock()


def codex_model_tiers() -> list[str]:
    """Codex's own model cache, which lists models most-capable-first."""
    cache = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "models_cache.json"
    try:
        models = (json.loads(cache.read_text(encoding="utf-8")) or {}).get("models") or []
        slugs = [m["slug"] for m in models if isinstance(m, dict) and m.get("slug")]
        # Drop non-agent entries like codex-auto-review, which aren't a capability tier.
        slugs = [s for s in slugs if not s.startswith("codex-")]
        return slugs or CODEX_MODEL_TIERS_FALLBACK
    except (OSError, json.JSONDecodeError, KeyError):
        return CODEX_MODEL_TIERS_FALLBACK


def _normalize_model(model: str) -> str:
    """Strip context-window and date suffixes so aliases compare equal."""
    model = model.strip().lower()
    for suffix in ("[1m]", "[200k]"):
        model = model.replace(suffix, "")
    return model.strip()


def _opencode_model_tiers() -> list[str]:
    return OPENCODE_MODEL_TIERS

def _kimi_model_tiers() -> list[str]:
    return KIMI_MODEL_TIERS

def _grok_model_tiers() -> list[str]:
    return GROK_MODEL_TIERS

def model_rank(kind: str, model: str | None) -> int | None:
    """Position in the capability ladder; lower is more capable. None if unknown."""
    if not model:
        return None
    tiers = model_rank_ladder(kind)
    target = _normalize_model(model)
    for index, known in enumerate(tiers):
        known_norm = _normalize_model(known)
        if target == known_norm or target.startswith(known_norm) or known_norm.startswith(target):
            return index
    # For opencode, also try substring match on model family (e.g. "opus", "sonnet", "haiku", "gpt-4", "mini", "flash")
    # so that provider/model strings like "anthropic/claude-opus-4-6" still rank.
    # If still unknown, return None and caller allows with log.
    return None


def capability_class(model: str | None) -> int | None:
    """Index into CAPABILITY_CLASSES; lower is more capable. None if unclassifiable.

    Vendor-agnostic by design - the caller does not have to know which client a
    model belongs to, which is what makes an equal-class comparison across two
    different vendors' ladders meaningful.
    """
    if not model:
        return None
    target = _normalize_model(model)
    # LONGEST match wins, not first. A cheap variant shares its family's prefix -
    # grok-3-mini starts with grok-3, gpt-5.4-mini with gpt-5.4,
    # kimi-for-coding-highspeed with kimi-for-coding - so a first-hit scan in class
    # order would promote every one of them into the tier above the right one, which
    # is the direction that matters: it would let a light helper pass as a workhorse.
    best_index, best_len = None, -1
    for index, name in enumerate(CAPABILITY_CLASSES):
        for known in CAPABILITY_CLASS_MEMBERS[name]:
            known_norm = _normalize_model(known)
            if target.startswith(known_norm) and len(known_norm) > best_len:
                best_index, best_len = index, len(known_norm)
    if best_index is not None:
        return best_index
    for name, needles in CAPABILITY_CLASS_PATTERNS:
        if any(needle in target for needle in needles):
            return CAPABILITY_CLASSES.index(name)
    return None


def are_peers(one: str | None, other: str | None) -> bool:
    """Has the human declared these two models equivalent? (Sol <-> Opus, etc.)"""
    if not one or not other:
        return False
    left, right = _normalize_model(one), _normalize_model(other)

    def in_group(model: str, group: set[str]) -> bool:
        return any(model == _normalize_model(m) or model.startswith(_normalize_model(m))
                   for m in group)

    return any(in_group(left, group) and in_group(right, group) for group in PEER_MODELS)


def sideways_or_cheaper(kind: str, model: str) -> list[str]:
    """Example models a subagent on `model` may hand work to, for error messages."""
    own_class = capability_class(model)
    ladder = model_rank_ladder(kind)
    allowed = [m for m in ladder
               if are_peers(model, m)
               or (own_class is not None and (capability_class(m) or 0) >= own_class)]
    # Prefer the cheap end - that is what delegation is usually for.
    return allowed[-2:] if allowed else ladder[-2:]


def model_rank_ladder(kind: str) -> list[str]:
    """The known model list for one client, most capable first."""
    if kind == "claude":
        return CLAUDE_MODEL_TIERS
    if kind == "codex":
        return codex_model_tiers()
    if kind == "opencode":
        return OPENCODE_MODEL_TIERS
    if kind == "kimi":
        return KIMI_MODEL_TIERS
    if kind == "grok":
        return GROK_MODEL_TIERS
    return (CLAUDE_MODEL_TIERS + codex_model_tiers() + OPENCODE_MODEL_TIERS
            + KIMI_MODEL_TIERS + GROK_MODEL_TIERS)


def max_helpers() -> int:
    try:
        return max(0, int(os.environ.get("AGENT_BRIDGE_MAX_HELPERS", str(DEFAULT_MAX_HELPERS))))
    except ValueError:
        return DEFAULT_MAX_HELPERS


def enforce_delegation(kind: str, requested_model: str | None) -> None:
    """Gate a subagent launching its own helper. No-op for a top-level agent.

    Top-level launches are the human's call and stay unrestricted. A subagent
    (AGENT_BRIDGE_JOB_ID is set) may delegate DOWN a capability class or SIDEWAYS
    within one - including across vendors, which is how Sol hands work to Opus and
    Opus hands work to Sol - and only a bounded number of times. Delegating UP is
    still refused: that is escalation dressed as offloading, and the point of the
    cap is that the delegating agent stays accountable for the result.
    """
    if not os.environ.get("AGENT_BRIDGE_JOB_ID"):
        return

    with _helpers_lock:
        already = _helpers_launched
    allowed = max_helpers()
    if already >= allowed:
        raise RuntimeError(
            f"delegation limit reached: you have already launched {already} helper agent(s), "
            f"the maximum is {allowed}. Do the remaining work yourself, or ask your parent "
            "(ask_parent) if the task genuinely needs more delegation than that."
        )

    own_model = os.environ.get("AGENT_BRIDGE_MODEL")
    if not requested_model:
        raise ValueError(
            "as a subagent you must name the `model` you are delegating to. It may be at "
            "your own level (a peer - e.g. a cross-vendor second opinion) or below it, but "
            "not above. Delegate drudgery (mechanical edits, bulk reads, formatting, log "
            f"scanning) to a cheaper model - for {kind} you could use "
            f"{', '.join(sideways_or_cheaper(kind, own_model or ''))}."
        )

    # A model unknown to BOTH the ladder and the class table can't be reasoned
    # about at all - refuse rather than wave it through on a typo.
    requested_class = capability_class(requested_model)
    if model_rank(kind, requested_model) is None and requested_class is None:
        raise ValueError(
            f"unknown model '{requested_model}' - cannot confirm it is at or below your own "
            f"level. Name a model from the known ladder: "
            f"{', '.join(model_rank_ladder(kind))}"
        )

    own_class = capability_class(own_model)
    if are_peers(own_model, requested_model):
        # Declared equivalent by the human. Sideways by definition, both ways.
        log(f"delegation: {own_model!r} -> {requested_model!r} allowed as declared peers")
    elif own_class is None or requested_class is None:
        # Can't prove the direction. Allow it - the count cap still bounds the
        # blast radius - but leave a trail saying the check was skipped.
        log(f"delegation: cannot class {own_model!r} or {requested_model!r}; "
            "direction check skipped")
    elif requested_class < own_class:
        raise RuntimeError(
            f"you may delegate SIDEWAYS or DOWNWARD, not upward. You are running "
            f"{own_model} ({CAPABILITY_CLASSES[own_class]}); '{requested_model}' is "
            f"{CAPABILITY_CLASSES[requested_class]}, a more capable class, so this would "
            "escalate rather than offload. Pick a peer at your own level or a cheaper model "
            f"({', '.join(sideways_or_cheaper(kind, own_model or ''))}), and keep the "
            "judgement calls yourself - you remain responsible for the result either way."
        )

    with _helpers_lock:
        globals()["_helpers_launched"] = already + 1


def command_preview(command: list[str]) -> list[str]:
    preview = list(command)
    if preview and preview[-1] == "-":
        return preview
    if len(preview) >= 2 and preview[-2] == "--":
        preview[-1] = "<prompt>"
        return preview
    # Opencode: last arg is prompt without marker; also codex may have prompt as last after "-" removed?
    # If last element looks like a long prompt (contains space) and not a flag, hide it.
    if preview and len(preview[-1]) > 20 and " " in preview[-1] and not preview[-1].startswith("-"):
        preview[-1] = "<prompt>"
    return preview


def build_codex_command(
    args: dict[str, Any], background: bool = False, job_id: str | None = None,
    add_dirs_override: list[str] | None = None,
) -> tuple[list[str], str, str, int, dict[str, Any]]:
    prompt = with_claude_md_preamble(require_str(args, "prompt"))
    # Advertise ask_parent only if codex can actually reach the bridge: either we're
    # injecting it for this invocation (job_id present), or it's in the user's config.
    inject_bridge = bool(background and job_id)
    # Needed by the -c injection below, so resolve it before that block.
    model = optional_str(args, "model")
    if background and (inject_bridge or codex_has_bridge()):
        prompt = with_ask_parent_preamble(
            prompt, CODEX_ASK_PARENT_TOOL, CODEX_CHECK_NOTES_TOOL, CODEX_RAISE_CONCERN_TOOL,
            CODEX_WARM_AGENTS_TOOL,
            sections=optional_string_list(args, "preamble_sections") or None,
            multi_phase=optional_bool(args, "multi_phase", True))
    cwd = resolve_cwd(args)
    timeout_seconds = optional_int(args, "timeout_seconds", DEFAULT_TIMEOUT_SECONDS, 1, 24 * 60 * 60)
    codex_bin = os.environ.get("CODEX_BIN", "codex")
    approval_policy = enum_value(args, "approval_policy", "never", {"never", "on-request", "untrusted"})
    sandbox = enum_value(
        args,
        "sandbox",
        "workspace-write",
        {"read-only", "workspace-write", "danger-full-access"},
    )
    # Default to PERSISTED sessions so codex jobs are resumable (continue_codex_agent
    # and `codex resume <id>`). Pass ephemeral=true explicitly to opt out (non-resumable).
    ephemeral = optional_bool(args, "ephemeral", False)
    skip_git_repo_check = optional_bool(args, "skip_git_repo_check", True)
    add_dirs = add_dirs_override if add_dirs_override is not None else optional_string_list(args, "add_dirs")
    extra_args = optional_string_list(args, "extra_args")

    command = [
        codex_bin,
        "--ask-for-approval",
        approval_policy,
        "exec",
        "--color",
        "never",
        "--sandbox",
        sandbox,
        "--cd",
        cwd,
    ]
    if ephemeral:
        command.append("--ephemeral")
    if skip_git_repo_check:
        command.append("--skip-git-repo-check")
    if inject_bridge:
        assert job_id is not None
        command.extend(codex_bridge_overrides(job_id, model))

    if model:
        command.extend(["--model", model])

    profile = optional_str(args, "profile")
    if profile:
        command.extend(["--profile", profile])

    for add_dir in add_dirs:
        command.extend(["--add-dir", str(Path(add_dir).expanduser().resolve())])

    command.extend(extra_args)
    command.append("-")
    meta = {
        "session_persist": not ephemeral,
        "model": model,
        "resume": {"sandbox": sandbox, "approval_policy": approval_policy, "cwd": cwd},
    }
    return command, prompt, cwd, timeout_seconds, meta


def always_allowed_report_tools(
    allowed: list[str], disallowed: list[str]
) -> tuple[list[str], list[str]]:
    """Force the parent-report channel into the permission config, unconditionally.

    A sandboxed child that cannot finish its task must still be able to SAY SO. If
    the only channel back to its parent is itself gated behind a permission prompt,
    the failure arrives as silence: the job burns its timeout and reports nothing,
    which is the single worst outcome the bridge can produce - worse than a refusal,
    because nobody learns why. So these three tools are not a default the caller can
    forget; they are added to every background launch whether or not an allowlist was
    passed, and removed from the denylist if a caller put them there.

    Safe to always add: claude's --allowedTools is an ADDITIVE auto-approve list, not
    an exhaustive whitelist (verified against the installed CLI - a child launched
    with `--allowedTools Read` still used Bash without prompting). Naming three tools
    here therefore costs the caller no other capability. Under --print, an unapproved
    MCP call would otherwise be DENIED rather than prompted, so without this the
    subagent can see ask_parent and never manage to call it - the same failure the
    codex `default_tools_approval_mode="approve"` override already fixes on that side.
    """
    for required in REPORT_CHANNEL_TOOLS:
        if required not in allowed:
            allowed = [*allowed, required]
    # A deny beats an allow, so a caller-supplied denylist would re-gag the child
    # even with the tools allowlisted. Drop those entries and leave a trail.
    gagged = [t for t in disallowed if t in REPORT_CHANNEL_TOOLS]
    if gagged:
        log(f"ignoring disallowed_tools {', '.join(gagged)}: the parent report channel "
            "(ask_parent / check_notes / raise_concern) is always allowed, so a child that "
            "cannot finish can still report instead of timing out silently")
        disallowed = [t for t in disallowed if t not in REPORT_CHANNEL_TOOLS]
    return allowed, disallowed


def build_claude_command(args: dict[str, Any], background: bool = False, add_dirs_override: list[str] | None = None) -> tuple[list[str], str, str, int, dict[str, Any]]:
    prompt = require_str(args, "prompt")
    if background:
        prompt = with_ask_parent_preamble(
            prompt,
            sections=optional_string_list(args, "preamble_sections") or None,
            multi_phase=optional_bool(args, "multi_phase", True),
        )
    cwd = resolve_cwd(args)
    timeout_seconds = optional_int(args, "timeout_seconds", DEFAULT_TIMEOUT_SECONDS, 1, 24 * 60 * 60)
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    permission_mode = enum_value(
        args,
        "permission_mode",
        "auto",
        {"acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"},
    )
    output_format = enum_value(args, "output_format", "text", {"text", "json", "stream-json"})
    # Default to PERSISTED sessions so claude jobs are resumable (`claude --resume <id>`).
    # Pass no_session_persistence=true explicitly to opt out (non-resumable).
    no_session_persistence = optional_bool(args, "no_session_persistence", False)
    # Pre-assign a session id so the resume command is known immediately, without having
    # to parse it out of json output (works even with the default text output format).
    session_id = optional_str(args, "session_id") or str(uuid.uuid4())
    add_dirs = add_dirs_override if add_dirs_override is not None else optional_string_list(args, "add_dirs")
    allowed_tools = optional_string_list(args, "allowed_tools")
    disallowed_tools = optional_string_list(args, "disallowed_tools")
    if background:
        allowed_tools, disallowed_tools = always_allowed_report_tools(
            allowed_tools, disallowed_tools)
    extra_args = optional_string_list(args, "extra_args")

    command = [
        claude_bin,
        "--print",
        "--output-format",
        output_format,
        "--permission-mode",
        permission_mode,
    ]
    if no_session_persistence:
        command.append("--no-session-persistence")
    else:
        command.extend(["--session-id", session_id])

    model = optional_str(args, "model")
    if model:
        command.extend(["--model", model])

    if allowed_tools:
        command.extend(["--allowedTools", ",".join(allowed_tools)])
    if disallowed_tools:
        command.extend(["--disallowedTools", ",".join(disallowed_tools)])
    if add_dirs:
        command.extend(["--add-dir", *[str(Path(item).expanduser().resolve()) for item in add_dirs]])

    command.extend(extra_args)
    command.extend(["--", prompt])
    meta = {
        "session_persist": not no_session_persistence,
        "model": model,
        # Session id is known up-front only when we persist (we passed --session-id).
        "session_id": None if no_session_persistence else session_id,
    }
    return command, prompt, cwd, timeout_seconds, meta


def build_opencode_command(
    args: dict[str, Any], background: bool = False,
    add_dirs_override: list[str] | None = None,
) -> tuple[list[str], str, str, int, dict[str, Any]]:
    """Build an `opencode run` command.

    Opencode's CLI: `opencode run --format json --dir <cwd> -m <model> <prompt>`
    Background launches advertise the parent question channel (ask_parent / check_notes)
    using opencode's direct tool names (no mcp__ prefix).

    Opencode has NO per-directory sandbox flag (--dir sets the working directory only,
    not additional accessible directories). The `add_dirs_override` parameter is accepted
    for interface uniformity with other build_* functions but does not expand the
    subagent's filesystem access. The caller is told this limitation in the launch
    response.
    """
    prompt = require_str(args, "prompt")
    if background and (opencode_has_bridge() or os.environ.get("AGENT_BRIDGE_PARENT") == "opencode"):
        prompt = with_ask_parent_preamble(
            prompt,
            OPENCODE_ASK_PARENT_TOOL,
            OPENCODE_CHECK_NOTES_TOOL,
            OPENCODE_RAISE_CONCERN_TOOL,
            OPENCODE_WARM_AGENTS_TOOL,
            sections=optional_string_list(args, "preamble_sections") or None,
            multi_phase=optional_bool(args, "multi_phase", True),
        )
    cwd = resolve_cwd(args)
    timeout_seconds = optional_int(args, "timeout_seconds", DEFAULT_TIMEOUT_SECONDS, 1, 24 * 60 * 60)
    opencode_bin = os.environ.get("OPENCODE_BIN", "opencode")

    model = optional_str(args, "model")
    variant = optional_str(args, "variant")
    agent_name = optional_str(args, "agent")
    output_format = enum_value(args, "output_format", "json", {"json", "default"})
    if output_format not in ("json", "default"):
        output_format = "json"

    extra_args = optional_string_list(args, "extra_args")
    no_session_persistence = False

    # Opencode has no --add-dir flag; we include add_dirs_override for interface
    # consistency only. The caller is told in the launch response that add_dirs
    # could not be honored for this client.
    _ = add_dirs_override  # accepted, cannot be applied to the command

    command = [
        opencode_bin,
        "run",
        "--format",
        output_format,
        "--dir",
        cwd,
    ]
    if model:
        command.extend(["--model", model])
    if variant:
        command.extend(["--variant", variant])
    if agent_name:
        command.extend(["--agent", agent_name])

    command.extend(extra_args)
    # Opencode takes prompt as positional at end; we already ensured prompt is non-empty
    command.append(prompt)

    meta = {
        "session_persist": not no_session_persistence,
        "model": model,
        "resume": {"cwd": cwd},
    }
    # For run_command compatibility, we return prompt as second element but it's already in command.
    # We will treat opencode specially: feeds_stdin is False, prompt in command.
    # The second return value is still prompt for collect_job threading, but we won't feed it via stdin.
    return command, prompt, cwd, timeout_seconds, meta


def build_kimi_command(
    args: dict[str, Any], background: bool = False,
    add_dirs_override: list[str] | None = None,
) -> tuple[list[str], str, str, int, dict[str, Any]]:
    """Build a `kimi -p` command.

    Kimi Code CLI: `kimi -p <prompt> --output-format stream-json -m <model> --add-dir <dir>`
    Cwd is handled via subprocess cwd, since kimi has no --dir flag.
    `--add-dir` (repeatable) widens accessible directories. STATE_DIR is always added.
    """
    prompt = require_str(args, "prompt")
    if background and (kimi_has_bridge() or os.environ.get("AGENT_BRIDGE_PARENT") == "kimi"):
        prompt = with_ask_parent_preamble(
            prompt,
            KIMI_ASK_PARENT_TOOL,
            KIMI_CHECK_NOTES_TOOL,
            KIMI_RAISE_CONCERN_TOOL,
            KIMI_WARM_AGENTS_TOOL,
            sections=optional_string_list(args, "preamble_sections") or None,
            multi_phase=optional_bool(args, "multi_phase", True),
        )
    cwd = resolve_cwd(args)
    timeout_seconds = optional_int(args, "timeout_seconds", DEFAULT_TIMEOUT_SECONDS, 1, 24 * 60 * 60)
    kimi_bin = os.environ.get("KIMI_BIN") or os.environ.get("KIMI_CODE_BIN")
    if not kimi_bin:
        default_path = Path("~/.kimi-code/bin/kimi").expanduser()
        kimi_bin = str(default_path) if default_path.exists() else "kimi"

    model = optional_str(args, "model")
    output_format = enum_value(args, "output_format", "stream-json", {"stream-json", "text"})
    extra_args = optional_string_list(args, "extra_args")
    yolo = optional_bool(args, "yolo", True)
    add_dirs = add_dirs_override if add_dirs_override is not None else optional_string_list(args, "add_dirs")

    command = [kimi_bin]
    if model:
        command.extend(["-m", model])
    command.extend(["-p", prompt, "--output-format", output_format])
    if yolo:
        pass
    for add_dir in add_dirs:
        command.extend(["--add-dir", str(Path(add_dir).expanduser().resolve())])
    command.extend(extra_args)

    meta = {
        "session_persist": True,  # kimi persists sessions automatically
        "model": model,
        "resume": {"cwd": cwd},
    }
    return command, prompt, cwd, timeout_seconds, meta


def _parse_kimi_stream_json(stdout: str) -> dict[str, Any]:
    """Parse `kimi -p --output-format stream-json` JSONL output.

    Each line is a JSON object with possible `type` or message structure.
    We collect assistant text and try to extract token usage if present.
    """
    reply_parts: list[str] = []
    usage: dict[str, Any] | None = None
    session_id: str | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # if text format, treat whole stdout later
            continue
        if not isinstance(event, dict):
            continue
        # Try to detect session id
        sid = event.get("session_id") or event.get("sessionId") or event.get("id")
        if sid and isinstance(sid, str) and len(sid) > 8 and not session_id:
            # heuristic: session ids often contain hyphens or are long
            if "-" in sid or sid.startswith("session_"):
                session_id = sid
        # Kimi stream-json typical shape: {"role":"assistant","content":...} or {"type":"..."}
        # Assistant message
        if event.get("role") == "assistant":
            content = event.get("content")
            if isinstance(content, str):
                reply_parts.append(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        txt = item.get("text") or item.get("content")
                        if txt:
                            reply_parts.append(txt)
                    elif isinstance(item, str):
                        reply_parts.append(item)
        # tool call result etc
        if event.get("type") == "assistant" and event.get("message"):
            msg = event["message"]
            if isinstance(msg, dict):
                c = msg.get("content")
                if isinstance(c, str):
                    reply_parts.append(c)
        # token usage often in event with usage field
        if "usage" in event and isinstance(event["usage"], dict):
            usage = event["usage"]
        if "tokens" in event and isinstance(event["tokens"], dict):
            usage = event["tokens"]
    reply = "\n".join(reply_parts).strip()
    if not reply:
        # fallback: if stdout is plain text (when --output-format text), use raw
        # but we already have json lines, so try to return whatever non-json lines exist
        non_json = []
        for line in stdout.splitlines():
            try:
                json.loads(line)
            except:
                if line.strip():
                    non_json.append(line)
        if non_json:
            reply = "\n".join(non_json).strip()
        else:
            reply = stdout.strip()
    return {"reply": reply, "usage": usage, "session_id": session_id}


def build_grok_command(
    args: dict[str, Any], background: bool = False,
    add_dirs_override: list[str] | None = None,
) -> tuple[list[str], str, str, int, dict[str, Any]]:
    """Build a `grok -p` command.

    Grok CLI: `grok -p <prompt> --output-format json|plain -m <model> --cwd <cwd>`

    Grok has no per-directory sandbox flag (--sandbox accepts a profile name, not paths,
    and --allow controls tool permissions, not filesystem access). The
    `add_dirs_override` parameter is accepted for interface uniformity but does not
    expand the subagent's filesystem access. The caller is told this limitation in the
    launch response.
    """
    prompt = require_str(args, "prompt")
    if background and (grok_has_bridge() or os.environ.get("AGENT_BRIDGE_PARENT") == "grok"):
        prompt = with_ask_parent_preamble(
            prompt,
            GROK_ASK_PARENT_TOOL,
            GROK_CHECK_NOTES_TOOL,
            GROK_RAISE_CONCERN_TOOL,
            GROK_WARM_AGENTS_TOOL,
            sections=optional_string_list(args, "preamble_sections") or None,
            multi_phase=optional_bool(args, "multi_phase", True),
        )
    cwd = resolve_cwd(args)
    timeout_seconds = optional_int(args, "timeout_seconds", DEFAULT_TIMEOUT_SECONDS, 1, 24 * 60 * 60)
    grok_bin = os.environ.get("GROK_BIN") or "/Users/devonedwards/.local/bin/grok"
    if not Path(grok_bin).exists():
        grok_bin = os.environ.get("GROK_BIN", "grok")

    model = optional_str(args, "model")
    output_format = enum_value(args, "output_format", "json", {"json", "plain", "streaming-json"})
    extra_args = optional_string_list(args, "extra_args")
    _ = add_dirs_override  # accepted, cannot be applied (grok --sandbox is profile-based)

    command = [grok_bin, "-p", prompt, "--output-format", output_format]
    if model:
        command.extend(["-m", model])
    # Grok has --cwd flag per help
    command.extend(["--cwd", cwd])
    if background:
        # The child must always be able to report back - see the comment on
        # GROK_REPORT_CHANNEL_ALLOW_RULES and always_allowed_report_tools().
        for rule in GROK_REPORT_CHANNEL_ALLOW_RULES:
            command.extend(["--allow", rule])
    command.extend(extra_args)

    meta = {
        "session_persist": True,
        "model": model,
        "resume": {"cwd": cwd},
    }
    return command, prompt, cwd, timeout_seconds, meta


def _parse_grok_json(stdout: str) -> dict[str, Any]:
    """Parse `grok -p --output-format json` output.

    Example:
    {
      "text": "Hello",
      "sessionId": "...",
      "usage": {"input_tokens":..., "output_tokens":..., "total_tokens":...},
      ...
    }
    Could also be plain text if format=plain.
    """
    reply_parts: list[str] = []
    usage: dict[str, Any] | None = None
    session_id: str | None = None
    # Try parse entire stdout as json first (single object)
    try:
        obj = json.loads(stdout.strip())
        if isinstance(obj, dict):
            if obj.get("text"):
                reply_parts.append(obj["text"])
            if obj.get("sessionId"):
                session_id = obj["sessionId"]
            if obj.get("usage"):
                usage = obj["usage"]
            # also handle streaming-json lines
            if reply_parts:
                return {"reply": "\n".join(reply_parts).strip(), "usage": usage, "session_id": session_id}
    except json.JSONDecodeError:
        pass
    # Fallback: JSONL streaming-json
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("text"):
            reply_parts.append(event["text"])
        if event.get("sessionId") and not session_id:
            session_id = event["sessionId"]
        if event.get("usage"):
            usage = event["usage"]
        # For streaming-json, sometimes content in different shape
        if event.get("role") == "assistant" and event.get("content"):
            c = event["content"]
            if isinstance(c, str):
                reply_parts.append(c)
            elif isinstance(c, list):
                for item in c:
                    if isinstance(item, dict) and item.get("text"):
                        reply_parts.append(item["text"])
    reply = "\n".join(reply_parts).strip()
    if not reply:
        # plain text
        reply = stdout.strip()
    return {"reply": reply, "usage": usage, "session_id": session_id}


def _parse_opencode_json_events(stdout: str) -> dict[str, Any]:
    """Parse `opencode run --format json` JSONL output.

    Each line is an event with `type` = step_start, text, step_finish, etc.
    We collect text parts and final tokens + session id.
    """
    reply_parts: list[str] = []
    usage: dict[str, Any] | None = None
    session_id: str | None = None
    cost: float | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        # Extract session id if present
        sid = event.get("sessionID") or event.get("session_id") or (event.get("part") or {}).get("sessionID")
        if sid and not session_id:
            session_id = sid
        etype = event.get("type")
        if etype == "text":
            part = event.get("part") or {}
            text = part.get("text") or event.get("text")
            if text:
                reply_parts.append(text)
        elif etype == "step_finish":
            part = event.get("part") or {}
            tokens = part.get("tokens") or event.get("tokens")
            if tokens:
                usage = tokens
            if part.get("cost") is not None:
                cost = part.get("cost")
        elif etype == "error":
            # Include error text as reply
            part = event.get("part") or {}
            err_text = part.get("text") or event.get("error") or ""
            if err_text:
                reply_parts.append(f"[error] {err_text}")
    # Some opencode versions emit message with nested structure; fallback to raw stdout if no text parts found
    reply = "\n".join(reply_parts).strip()
    if not reply:
        # If json parsing yielded nothing, maybe stdout was already plain text (when format=default)
        # Return stdout as reply
        reply = stdout.strip()
    return {"reply": reply, "usage": usage, "session_id": session_id, "cost": cost}


def _git_numstat_parse(raw: str) -> tuple[int, int]:
    """Sum insertions and deletions from git diff --numstat output."""
    ins = 0
    dels = 0
    for line in raw.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2:
            try:
                ins += int(parts[0]) if parts[0] != "-" else 0
                dels += int(parts[1]) if parts[1] != "-" else 0
            except ValueError:
                pass
    return ins, dels


def _files_changed(job: Any) -> dict[str, Any] | None:
    """Count changed files and insertions/deletions from git in job.cwd.

    Returns an object (never a raw int) with:
      - `files` — changed-path count from git status --porcelain
      - `insertions` / `deletions` — summed from git diff --numstat
      - `untracked` — new-file count (not in numstat)
      - `insertions_since_launch` — delta against the launch baseline (the field that
        answers "has this job actually done anything" on an already-dirty repo)

    Returns None when cwd is not a git repo (field omitted from the payload).
    Cached with a short TTL so rapid agent_status polling doesn't spawn git per call.
    """
    now = time.time()
    with job.lock:
        cached = getattr(job, "_files_changed_cache", None)
        baseline = getattr(job, "_launch_numstat", None)
    if cached is not None:
        count, cached_at = cached
        if now - cached_at < FILES_CHANGED_TTL_SECONDS:
            return count

    try:
        proc = subprocess.run(
            ["git", "-C", job.cwd, "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            return None
        lines = [l for l in proc.stdout.splitlines() if l.strip()]
        files = len(lines)
        untracked = sum(1 for l in lines if l.startswith("??"))

        # numstat for insertions/deletions on tracked files
        ins = 0
        dels = 0
        ins_since_launch: int | None = None
        numstat_proc = subprocess.run(
            ["git", "-C", job.cwd, "diff", "--numstat"],
            capture_output=True, text=True, timeout=10,
        )
        if numstat_proc.returncode == 0 and numstat_proc.stdout.strip():
            ins, dels = _git_numstat_parse(numstat_proc.stdout)
            if baseline is not None:
                base_ins, base_dels = _git_numstat_parse(baseline)
                # The current numstat minus the baseline numstat is the net change
                # attributable to this job. Floor at 0 — files can be cleaned.
                ins_since_launch = max(0, ins - base_ins)

        result: dict[str, Any] = {
            "files": files,
            "insertions": ins,
            "deletions": dels,
            "untracked": untracked,
        }
        if ins_since_launch is not None:
            result["insertions_since_launch"] = ins_since_launch
        with job.lock:
            job._files_changed_cache = (result, now)
        return result
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        return None


def _suspicious_check(job: Any) -> dict[str, Any] | None:
    """Flag implausible successes: returncode 0 but the job didn't produce evidence of work.

    Signals checked, in order of confidence (strongest first):
      1. insertions_since_launch == 0 on a mutation job — the repo hasn't changed at all
      2. Finished in <15 seconds with <1000 total tokens
      3. Stderr has warnings but the job claimed success

    Prefer false negatives over noisy false positives: a suspicious flag that fires on
    healthy jobs will be ignored within a day.
    """
    with job.lock:
        returncode = job.returncode
        elapsed = (job.finished_at or time.time()) - job.started_at
        tokens = job.tokens
        stderr_buf = job.stderr if job.stderr else ""
    if returncode != 0:
        return None

    reasons: list[str] = []
    # Signal 1: insertions_since_launch (strongest — a zero-change mutation job is clear)
    fc = _files_changed(job)
    if fc is not None and fc.get("insertions_since_launch") is not None:
        if fc["insertions_since_launch"] == 0 and fc.get("files", 0) == 0:
            reasons.append(
                f"returned 0 in {elapsed:.1f}s with zero git changes (insertions_since_launch=0)"
            )

    # Signal 2: elapsed/output token floor (weaker heuristic)
    output_tokens = (tokens or {}).get("output_tokens", 0) or 0
    total_tokens = (tokens or {}).get("total_tokens", 0) or 0
    if elapsed < 15 and total_tokens < 1000:
        reasons.append(
            f"finished in {elapsed:.1f}s with only {total_tokens} total tokens "
            f"(output: {output_tokens})"
        )

    # Signal 3: stderr warnings with clean exit (weakest)
    if elapsed < 30 and stderr_buf:
        warnings = _scan_stderr_warnings(stderr_buf)
        if warnings:
            reasons.append(
                f"returned 0 in {elapsed:.1f}s but stderr has {len(warnings)} "
                f"warning(s): {warnings[0].get('line', '')[:120]}"
            )

    if not reasons:
        return None
    return {
        "suspicious": True,
        "reason": "; ".join(reasons),
        "elapsed_seconds": round(elapsed, 1),
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
    enforce_depth()
    started_at = time.time()
    feeds_stdin = command[-1] == "-"
    run_kwargs: dict[str, Any] = dict(
        cwd=cwd,
        env=child_env(kind),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    if feeds_stdin:
        run_kwargs["input"] = prompt
    else:
        # DEVNULL, not inherited: keep a stdin-reading subagent off the server's stdin.
        run_kwargs["stdin"] = subprocess.DEVNULL
    completed = subprocess.run(command, **run_kwargs)
    finished_at = time.time()
    return {
        "kind": kind,
        "status": "succeeded" if completed.returncode == 0 else "failed",
        "returncode": completed.returncode,
        "elapsed_seconds": round(finished_at - started_at, 3),
        "cwd": cwd,
        "command": command_preview(command),
        "stdout": truncate_text(completed.stdout, max_output_chars),
        "stderr": truncate_text(completed.stderr, max_output_chars),
    }


def git_commit_paths(cwd: str, paths: list[str], message: str) -> dict[str, Any]:
    """Stage EXPLICIT paths (never `-A`) and commit, on the host outside any agent
    sandbox. Codex's workspace-write sandbox blocks writes to .git, so an agent cannot
    commit its own edits; this lets the bridge land them. Never raises."""
    try:
        for path in paths:
            subprocess.run(["git", "-C", cwd, "add", "--", path],
                           capture_output=True, text=True, timeout=120)
        staged = subprocess.run(["git", "-C", cwd, "diff", "--cached", "--name-only"],
                                capture_output=True, text=True, timeout=60)
        if not staged.stdout.strip():
            return {"committed": False, "hash": None,
                    "detail": "nothing to commit for the given paths"}
        commit = subprocess.run(["git", "-C", cwd, "commit", "-m", message],
                                capture_output=True, text=True, timeout=120)
        if commit.returncode != 0:
            return {"committed": False, "hash": None,
                    "detail": f"git commit failed: {(commit.stderr or commit.stdout).strip()[:500]}"}
        rev = subprocess.run(["git", "-C", cwd, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=30)
        return {"committed": True, "hash": rev.stdout.strip(),
                "staged": staged.stdout.split(), "detail": commit.stdout.strip()[:500]}
    except Exception as exc:  # pragma: no cover - defensive
        return {"committed": False, "hash": None, "detail": f"exception: {exc}"}


def launch_command(kind: str, command: list[str], prompt: str | None, cwd: str, timeout_seconds: int, commit_paths: list[str] | None = None, commit_message: str | None = None, meta: dict[str, Any] | None = None, job_id: str | None = None, task: str | None = None, sandbox_note: dict[str, Any] | None = None, add_dirs: list[str] | None = None) -> dict[str, Any]:
    enforce_depth()
    enforce_delegation(kind, (meta or {}).get("model"))

    # The job id reaches the child so ask_parent can address questions back at this job.
    # Callers that must bake it into the command itself (launch_codex, via -c env
    # overrides) generate it first and pass it in; otherwise make one here.
    job_id = job_id or str(uuid.uuid4())
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=child_env(kind, job_id=job_id, model=(meta or {}).get("model"), add_dirs=add_dirs, cwd=cwd),
        # DEVNULL (never None) when we aren't piping a prompt: None would make the child
        # INHERIT the server's stdin (the JSON-RPC pipe), and a subagent that reads stdin
        # (e.g. `claude --print`) then steals incoming requests, hanging agent_status.
        stdin=subprocess.PIPE if command[-1] == "-" else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    meta = meta or {}
    job = Job(
        id=job_id,
        kind=kind,
        command=command,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        started_at=time.time(),
        process=process,
        commit_paths=commit_paths or [],
        commit_message=commit_message,
        # For claude we pre-assigned the session id, so it's known immediately.
        # For codex the id is discovered from stdout/rollout once the job finishes.
        session_id=meta.get("session_id"),
        model=meta.get("model"),
        resume=meta.get("resume"),
        # Kept so a warm agent is identifiable later by what it was working on -
        # "which of these six sessions knows the auth refactor" is unanswerable from
        # a job id alone. The CALLER's original prompt, deliberately: `prompt` here is
        # None for every client that carries its prompt inside the command (grok,
        # kimi, opencode, and claude unless it's piped), and where it is set it has
        # already been preamble-wrapped, so 400 chars of it would be boilerplate.
        task=task,
        last_used_at=time.time(),
    )

    with jobs_lock:
        jobs[job.id] = job

    # ---- Correction 3: capture git numstat baseline at launch ----
    # An already-dirty repo makes absolute changed-file counts meaningless. The delta
    # against a launch-time baseline is what answers "has this job actually done anything."
    try:
        baseline = subprocess.run(
            ["git", "-C", job.cwd, "diff", "--numstat"],
            capture_output=True, text=True, timeout=10,
        )
        if baseline.returncode == 0:
            job._launch_numstat = baseline.stdout
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        pass

    thread = threading.Thread(target=collect_job, args=(job, prompt), daemon=True)
    thread.start()

    response: dict[str, Any] = {
        "job_id": job.id,
        "kind": kind,
        "pid": process.pid,
        "status": job.status,
        "cwd": cwd,
        "timeout_seconds": timeout_seconds,
        "session_id": job.session_id,
        "command": command_preview(command),
    }
    if sandbox_note:
        response["sandbox"] = sandbox_note
    return response


def _maybe_raise_auto_rejection_question(
    job: Job, stderr_line: str, seen: set[str]
) -> None:
    """When the streaming stderr watcher sees a sandbox auto-rejection, raise it as a
    parent-side pending_question. This fixes "why didn't the agent ask" structurally:
    it no longer depends on the model noticing the rejection in its own stderr.

    The question record is a NOTIFICATION, not a blocked-subagent: the subagent already
    got rejected and moved on, so answer_agent semantics are advisory here. The record
    carries `action: "notify"` so the parent can distinguish it from a genuine blocked
    question where the subagent is waiting.
    """
    lower = stderr_line.strip().lower()
    if not (("external_directory" in lower or "outside the workspace" in lower
             or "auto-reject" in lower or "permission" in lower)):
        return
    # Don't re-raise a path we've already raised for this job
    paths = _scan_stderr_auto_rejections(stderr_line)
    new_paths = [p for p in paths if p not in seen]
    if not new_paths:
        return
    seen.update(new_paths)

    for path in new_paths:
        question_id = str(uuid.uuid4())
        record = {
            "question_id": question_id,
            "job_id": job.id,
            "from_kind": "bridge",
            "question": (
                f"Sandbox auto-rejection detected for job {job.id}: "
                f"access to '{path}' was denied by the sandbox. "
                f"The subagent already received the rejection and moved on — it is NOT "
                f"blocked waiting for this answer. You may want to relaunch with add_dirs "
                f"covering this path, or adjust the sandbox configuration."
            ),
            "context": (
                f"Stderr line: {stderr_line.strip()[:300]}\n"
                f"Rejected path: {path}\n"
                f"CWD: {job.cwd}"
            ),
            "status": "pending",
            "asked_at": time.time(),
            "answer": None,
            "answered_at": None,
            "on_timeout": "proceed",
            "ancestry": [j for j in (os.environ.get("AGENT_BRIDGE_ANCESTRY") or "").split(",") if j],
            "depth": current_depth() + 1,
            "escalated": False,
            "escalation_notes": [],
            # Marks this as a notification, not a blocked-subagent question. The
            # existing blocked-subagent semantics (answer_agent unblocks the subagent,
            # action_required claims the job is BLOCKED) are preserved for genuine
            # ask_parent questions and do NOT apply here.
            "auto_rejection_notification": True,
        }
        _write_question(record)
        log(f"auto-rejection question {question_id} for job {job.id}: {path}")


def collect_job(job: Job, prompt: str | None) -> None:
    """Run the child process, streaming stdout/stderr into job under lock.

    Before this rewrite, collect_job called `process.communicate()`, which blocked
    until the child exited — so job.stdout and job.stderr were empty strings for the
    entire life of a running job. Nothing that read them mid-flight could work. The
    parent caught failures only by noticing anomalies like "10 seconds elapsed on a
    multi-file port" after the fact.

    Now dedicated reader threads append to job.stdout / job.stderr as lines arrive, and
    a stdin writer thread feeds the prompt when `command[-1] == "-"`. Every existing
    behavior is preserved: the timeout path (terminate, then SIGKILL on posix with
    returncode fallback -signal.SIGTERM), the exception path (returncode -1), and the
    post-job sequence (enrich_job, save_to_roster, optional git_commit_paths).
    """
    # Stderr auto-rejection tracker: paths we've already raised parent questions for,
    # so we don't spam one question per repeated rejection.
    auto_rejection_paths_seen: set[str] = set()

    def _reader_thread(stream_name: str) -> None:
        """Read lines from a process stream and append to job.stdout / job.stderr."""
        stream = getattr(job.process, stream_name)
        buf_attr = stream_name
        for line in iter(stream.readline, ""):
            with job.lock:
                current = getattr(job, buf_attr)
                # The running dropped-total lives on the Job because it cannot be derived
                # from the buffer once truncation has started - see _cap_stream_buffer.
                dropped_attr = f"{buf_attr}_dropped_chars"
                capped, dropped = _cap_stream_buffer(
                    current + line, getattr(job, dropped_attr, 0))
                setattr(job, buf_attr, capped)
                setattr(job, dropped_attr, dropped)
            # On each stderr line, check for auto-rejections that warrant a parent question.
            # This runs in the reader thread to catch the signal as it arrives, not after
            # the job finishes — the whole point is early visibility.
            if stream_name == "stderr" and line.strip():
                _maybe_raise_auto_rejection_question(job, line, auto_rejection_paths_seen)

    def _stdin_writer() -> None:
        """Write the prompt to stdin, then close it so the child sees EOF."""
        if prompt is None:
            return
        try:
            job.process.stdin.write(prompt)
            job.process.stdin.flush()
        except (BrokenPipeError, OSError):
            # Child exited before reading all input — not an error worth surfacing.
            pass
        finally:
            try:
                job.process.stdin.close()
            except (BrokenPipeError, OSError):
                pass

    # Start reader threads BEFORE the stdin writer, so no output is ever lost. The
    # process is already running; stdout and stderr may already have data buffered.
    stdout_thread = threading.Thread(target=_reader_thread, args=("stdout",), daemon=True)
    stderr_thread = threading.Thread(target=_reader_thread, args=("stderr",), daemon=True)
    stdin_thread = threading.Thread(target=_stdin_writer, daemon=True)

    stdout_thread.start()
    stderr_thread.start()
    if job.command[-1] == "-":
        stdin_thread.start()

    # Wait for the process to exit, with timeout handling identical to the old path.
    try:
        job.process.wait(timeout=job.timeout_seconds)
        # Process exited; wait for reader threads to drain the remaining output.
        stdout_thread.join(timeout=30)
        stderr_thread.join(timeout=30)
        if job.command[-1] == "-":
            stdin_thread.join(timeout=10)
        with job.lock:
            job.returncode = job.process.returncode
            job.finished_at = time.time()
    except subprocess.TimeoutExpired:
        job.process.terminate()
        try:
            job.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.kill(job.process.pid, signal.SIGKILL)
            else:
                job.process.kill()
            job.process.wait()
        # Drain remaining output after termination.
        stdout_thread.join(timeout=30)
        stderr_thread.join(timeout=30)
        if job.command[-1] == "-":
            stdin_thread.join(timeout=10)
        with job.lock:
            job.error = f"timed out after {job.timeout_seconds} seconds"
            job.returncode = job.process.returncode if job.process.returncode is not None else -signal.SIGTERM
            job.finished_at = time.time()
    except Exception as exc:  # pragma: no cover - defensive runtime path
        with job.lock:
            job.error = str(exc)
            job.returncode = -1
            job.finished_at = time.time()

    # Discover the session id (for resume) and per-job token usage now that the job
    # has finished and its stdout/transcript exist. Never raises.
    try:
        enrich_job(job)
    except Exception:  # pragma: no cover - defensive
        pass

    # Enrichment is what discovers the session id for codex, so the roster entry has to
    # be written after it - otherwise the agent looks unresumable and never gets offered
    # for reuse. Survives a server restart from here on.
    save_to_roster(job)

    # Optional post-agent commit (Codex jobs given commit_paths). Runs on the host,
    # outside the agent sandbox, so it lands even though workspace-write blocks .git.
    # Only on a clean success; explicit paths only, never `git add -A`.
    if job.commit_paths and job.returncode == 0:
        result = git_commit_paths(job.cwd, job.commit_paths, job.commit_message or "agent commit")
        with job.lock:
            job.commit = result


# ---------------------------------------------------------------------------
# The warm-agent roster: reuse an agent that already knows things.
#
# An agent that has been working a problem has paid for its context - it has read
# the files, learned the layout, had its wrong assumptions corrected, and been told
# things it could not have guessed. Throwing that away and launching a fresh agent
# means paying for all of it again, in tokens and in wall-clock, to arrive back
# where the last one already was. continue_* has always been able to resume a
# session; what was missing is that the job roster lived only in this process's
# memory, so every server restart orphaned every warm agent. Their SESSIONS were
# still on disk and still resumable - the bridge just lost the paperwork.
#
# So the roster is written to disk. It is deliberately small: enough to identify an
# agent, judge whether it is still worth resuming, and hand it to continue_*.
#
# "At least to a point" is the other half. Reuse is not free forever: context grows,
# goes stale against a moving repo, and drags an agent toward the shape of its old
# task. So each entry carries a recommendation rather than an invitation, and an
# agent can go stale (by age), get crowded (by turns), or be retired outright.
# ---------------------------------------------------------------------------

ROSTER_DIR = STATE_DIR / "roster"
# Past these, reuse stops being the cheap option: a long-running session costs more
# per turn to re-read than a fresh agent costs to start, and a days-old session's
# picture of the repo may be actively wrong. Both tunable - these are judgement
# calls, not physics.
ROSTER_STALE_AFTER_SECONDS = float(
    os.environ.get("AGENT_BRIDGE_ROSTER_STALE_HOURS", "24")) * 3600
ROSTER_CROWDED_AFTER_TURNS = int(os.environ.get("AGENT_BRIDGE_ROSTER_MAX_TURNS", "12"))
ROSTER_KEEP = int(os.environ.get("AGENT_BRIDGE_ROSTER_KEEP", "200"))


def _roster_dir() -> Path:
    ROSTER_DIR.mkdir(parents=True, exist_ok=True)
    return ROSTER_DIR


def save_to_roster(job: Job) -> None:
    """Record a resumable agent on disk. Never raises - this is bookkeeping.

    Only agents that can actually be resumed earn an entry: without a session id
    there is nothing to continue, so listing one would be an empty promise.
    """
    try:
        with job.lock:
            if not job.session_id or job.returncode is None:
                return
            record = {
                "job_id": job.id,
                "kind": job.kind,
                "model": job.model,
                "cwd": job.cwd,
                "session_id": job.session_id,
                "resume": job.resume,
                "task": (job.task or "")[:400],
                "turns": job.turns,
                "started_at": job.started_at,
                "last_used_at": job.last_used_at or job.finished_at or job.started_at,
                "tokens": job.tokens,
                "status": job.status,
                "retired": job.retired,
                "retired_reason": job.retired_reason,
            }
        target = _roster_dir() / f"{job.id}.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, target)
        _prune_roster()
    except Exception as exc:  # pragma: no cover - defensive
        log(f"roster: could not record job {job.id}: {exc}")


def _iter_roster() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        for path in _roster_dir().glob("*.json"):
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
    except OSError:
        return out
    out.sort(key=lambda r: r.get("last_used_at") or 0, reverse=True)
    return out


def _prune_roster() -> None:
    """Drop the oldest entries past ROSTER_KEEP so the roster can't grow forever."""
    records = _iter_roster()
    for record in records[ROSTER_KEEP:]:
        try:
            (_roster_dir() / f"{record['job_id']}.json").unlink(missing_ok=True)
        except OSError:
            continue


def rehydrate_job(job_id: str) -> Job | None:
    """Rebuild an in-memory Job from the roster so continue_* can resume it.

    This is what makes persistence real rather than decorative: after a restart the
    bridge can hand a warm agent straight back to continue_*, instead of reporting
    'unknown job_id' about a session that is sitting on disk fully intact.
    """
    record = next((r for r in _iter_roster() if r.get("job_id") == job_id), None)
    if not record:
        return None
    job = Job(
        id=record["job_id"],
        kind=record.get("kind") or "claude",
        command=[],
        cwd=record.get("cwd") or str(Path.home()),
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        started_at=record.get("started_at") or time.time(),
        process=None,
        # A rehydrated job is finished by definition - the process died with the old
        # server. Marking it succeeded is what lets continue_* past its running check.
        returncode=0,
        finished_at=record.get("last_used_at"),
        session_id=record.get("session_id"),
        model=record.get("model"),
        tokens=record.get("tokens"),
        resume=record.get("resume"),
        # Enrichment reads a live transcript/rollout; nothing new to learn here, and
        # attempting it would re-scan files for a process that no longer exists.
        enriched=True,
        task=record.get("task"),
        turns=record.get("turns") or 1,
        last_used_at=record.get("last_used_at"),
        retired=bool(record.get("retired")),
        retired_reason=record.get("retired_reason"),
        rehydrated=True,
    )
    with jobs_lock:
        jobs.setdefault(job.id, job)
        return jobs[job.id]


def note_reuse(job: Job) -> None:
    """Record that a warm agent was resumed: one more turn, clock reset."""
    with job.lock:
        job.turns += 1
        job.last_used_at = time.time()
    save_to_roster(job)


def roster_verdict(record: dict[str, Any]) -> tuple[str, str]:
    """Should this agent be reused? Returns (verdict, why) for the human to act on."""
    if record.get("retired"):
        return "retired", record.get("retired_reason") or "retired by the human"
    if record.get("status") not in (None, "succeeded"):
        return "suspect", (
            f"its last run ended {record.get('status')} - read agent_result before "
            "trusting what it thinks it knows")
    age = time.time() - (record.get("last_used_at") or 0)
    turns = record.get("turns") or 1
    if age > ROSTER_STALE_AFTER_SECONDS:
        return "stale", (
            f"idle {round(age / 3600, 1)}h - its picture of the repo may be out of date, "
            "so re-brief it or start fresh")
    if turns >= ROSTER_CROWDED_AFTER_TURNS:
        return "crowded", (
            f"{turns} turns of context - re-reading it may now cost more than a fresh "
            "agent would, so prefer this one only if that context is the point")
    return "reuse", (
        f"warm: {turns} turn(s), last used {round(age / 60)}m ago - it already has the "
        "context, so resuming beats re-teaching a new agent")


def get_job(job_id: str) -> Job:
    with jobs_lock:
        job = jobs.get(job_id)
    if job is None:
        # Not in memory - it may predate a server restart. Its session is likely
        # still on disk, so try the roster before declaring it unknown.
        job = rehydrate_job(job_id)
    if job is None:
        raise ValueError(
            f"unknown job_id: {job_id}. It is not running here and not in the warm-agent "
            "roster; call warm_agents to see which agents are still resumable."
        )
    return job


CODEX_SESSIONS_DIR = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "sessions"
SESSION_ID_RE = re.compile(r"session id:\s*([0-9a-fA-F-]{36})")


def find_codex_rollout(session_id: str) -> Path | None:
    """Locate the rollout JSONL for a codex session id (newest match wins)."""
    try:
        matches = sorted(
            CODEX_SESSIONS_DIR.glob(f"**/*{session_id}*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    return matches[0] if matches else None


def parse_codex_rollout(path: Path) -> dict[str, Any]:
    """Extract token usage, model, and rate limits from a codex rollout JSONL.

    Codex writes `event_msg/token_count` events carrying `info.total_token_usage`
    (input/output/cached/reasoning/total) and a `rate_limits` block. This reads the
    LAST such event (cumulative totals for the session) plus the model from turn_context.
    """
    out: dict[str, Any] = {}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
                ptype = payload.get("type")
                if ptype in ("turn_context", "session_meta"):
                    model = payload.get("model")
                    if model:
                        out["model"] = model
                if ptype == "token_count":
                    info = payload.get("info") or {}
                    total = info.get("total_token_usage")
                    if total:
                        out["tokens"] = dict(total)
                    if info.get("model_context_window"):
                        out["model_context_window"] = info.get("model_context_window")
                    if payload.get("rate_limits"):
                        out["rate_limits"] = payload.get("rate_limits")
    except OSError:
        return out
    return out


def _sum_claude_transcript_tokens(session_id: str) -> dict[str, Any] | None:
    """Sum token usage from a single claude session transcript, by session id."""
    try:
        projects = find_claude_projects_dir()
    except FileNotFoundError:
        return None
    bucket = {f: 0 for f in USAGE_TOKEN_FIELDS} | {"assistant_messages": 0}
    found = False
    model = None
    for jsonl_path in projects.glob(f"*/{session_id}.jsonl"):
        found = True
        try:
            with jsonl_path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("type") != "assistant":
                        continue
                    message = entry.get("message") or {}
                    if message.get("model"):
                        model = message["model"]
                    usage = message.get("usage")
                    if not usage:
                        continue
                    for key in USAGE_TOKEN_FIELDS:
                        bucket[key] += usage.get(key, 0) or 0
                    bucket["assistant_messages"] += 1
        except OSError:
            continue
    if not found:
        return None
    if model:
        bucket["model"] = model
    bucket["total_tokens"] = (
        bucket.get("input_tokens", 0)
        + bucket.get("output_tokens", 0)
        + bucket.get("cache_read_input_tokens", 0)
        + bucket.get("cache_creation_input_tokens", 0)
    )
    return bucket


def enrich_job(job: Job) -> None:
    """Populate job.session_id / job.model / job.tokens from on-disk artifacts.

    For codex: session id is printed to stdout ("session id: <uuid>") and the rollout
    JSONL under ~/.codex/sessions carries token_count events. For claude: the session id
    is pre-assigned (we passed --session-id) and the transcript under ~/.claude/projects
    carries per-turn usage. For opencode: we parse the JSONL stdout that was captured via
    `opencode run --format json`. Safe to call repeatedly; cheap and best-effort.
    """
    with job.lock:
        kind = job.kind
        session_id = job.session_id
        # codex prints the "session id:" and "tokens used" header lines to stderr.
        streams = f"{job.stderr}\n{job.stdout}"
        model = job.model

    if kind == "codex":
        if not session_id and streams:
            m = SESSION_ID_RE.search(streams)
            if m:
                session_id = m.group(1)
        if session_id:
            rollout = find_codex_rollout(session_id)
            info = parse_codex_rollout(rollout) if rollout else {}
            with job.lock:
                job.session_id = session_id
                if info.get("tokens"):
                    tokens = dict(info["tokens"])
                    if info.get("model_context_window"):
                        tokens["model_context_window"] = info["model_context_window"]
                    job.tokens = tokens
                if info.get("model") and not job.model:
                    job.model = info["model"]
                job.enriched = True
        else:
            with job.lock:
                job.enriched = True
    elif kind == "claude":
        if session_id:
            tokens = _sum_claude_transcript_tokens(session_id)
            claude_model = tokens.pop("model", None) if tokens else None
            with job.lock:
                if tokens:
                    job.tokens = tokens
                if claude_model and not job.model:
                    job.model = claude_model
                job.enriched = True
        else:
            with job.lock:
                job.enriched = True
    elif kind == "opencode":
        # Opencode stdout is JSONL events; parse for session_id and tokens
        try:
            parsed = _parse_opencode_json_events(streams or job.stdout or "")
            opencode_tokens = parsed.get("usage")
            opencode_sid = parsed.get("session_id")
            # Normalize opencode token shape to generic {input, output, total}
            norm_tokens: dict[str, Any] | None = None
            if opencode_tokens:
                inp = opencode_tokens.get("input") or opencode_tokens.get("input_tokens") or 0
                out = opencode_tokens.get("output") or opencode_tokens.get("output_tokens") or 0
                tot = opencode_tokens.get("total") or opencode_tokens.get("total_tokens") or (inp + out)
                reasoning = opencode_tokens.get("reasoning") or 0
                cache = opencode_tokens.get("cache") or {}
                norm_tokens = {
                    "input_tokens": inp,
                    "output_tokens": out,
                    "total_tokens": tot,
                    "reasoning_output_tokens": reasoning,
                    "cache_read_input_tokens": cache.get("read", 0),
                    "cache_creation_input_tokens": cache.get("write", 0),
                }
            with job.lock:
                if opencode_sid and not job.session_id:
                    job.session_id = opencode_sid
                if norm_tokens:
                    job.tokens = norm_tokens
                job.enriched = True
        except Exception:
            with job.lock:
                job.enriched = True
    elif kind == "kimi":
        try:
            parsed = _parse_kimi_stream_json(streams or job.stdout or "")
            kimi_tokens = parsed.get("usage")
            kimi_sid = parsed.get("session_id")
            norm_tokens = None
            if kimi_tokens:
                inp = kimi_tokens.get("input") or kimi_tokens.get("input_tokens") or kimi_tokens.get("prompt_tokens") or 0
                out = kimi_tokens.get("output") or kimi_tokens.get("output_tokens") or kimi_tokens.get("completion_tokens") or 0
                tot = kimi_tokens.get("total") or kimi_tokens.get("total_tokens") or (inp + out)
                norm_tokens = {
                    "input_tokens": inp,
                    "output_tokens": out,
                    "total_tokens": tot,
                }
            with job.lock:
                if kimi_sid and not job.session_id:
                    job.session_id = kimi_sid
                if norm_tokens:
                    job.tokens = norm_tokens
                job.enriched = True
        except Exception:
            with job.lock:
                job.enriched = True
    elif kind == "grok":
        try:
            parsed = _parse_grok_json(streams or job.stdout or "")
            grok_tokens = parsed.get("usage")
            grok_sid = parsed.get("session_id")
            norm_tokens = None
            if grok_tokens:
                inp = grok_tokens.get("input_tokens") or grok_tokens.get("input") or 0
                out = grok_tokens.get("output_tokens") or grok_tokens.get("output") or 0
                tot = grok_tokens.get("total_tokens") or grok_tokens.get("total") or (inp + out)
                # grok also has cache_read_input_tokens
                cache_read = grok_tokens.get("cache_read_input_tokens") or 0
                reasoning = grok_tokens.get("reasoning_tokens") or grok_tokens.get("reasoning") or 0
                norm_tokens = {
                    "input_tokens": inp,
                    "output_tokens": out,
                    "total_tokens": tot,
                    "cache_read_input_tokens": cache_read,
                    "reasoning_output_tokens": reasoning,
                }
            with job.lock:
                if grok_sid and not job.session_id:
                    job.session_id = grok_sid
                if norm_tokens:
                    job.tokens = norm_tokens
                job.enriched = True
        except Exception:
            with job.lock:
                job.enriched = True
    else:
        with job.lock:
            job.enriched = True


def _job_token_summary(tokens: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize a per-job token dict to a compact input/output/total view."""
    if not tokens:
        return None
    inp = tokens.get("input_tokens", 0) or 0
    out = tokens.get("output_tokens", 0) or 0
    total = tokens.get("total_tokens")
    if total is None:
        total = inp + out + (tokens.get("cache_read_input_tokens", 0) or 0) + (
            tokens.get("cache_creation_input_tokens", 0) or 0
        )
    summary = {"input_tokens": inp, "output_tokens": out, "total_tokens": total}
    for extra in (
        "cached_input_tokens",
        "reasoning_output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "model_context_window",
        "assistant_messages",
    ):
        if extra in tokens and tokens[extra]:
            summary[extra] = tokens[extra]
    return summary


def summarize_job(job: Job) -> dict[str, Any]:
    with job.lock:
        finished_at = job.finished_at
        elapsed_until = finished_at or time.time()
        kind = job.kind
        session_id = job.session_id
        model = job.model
        tokens = job.tokens
        stderr_buf = job.stderr
        stdout_buf = job.stdout
        summary = {
            "job_id": job.id,
            "kind": job.kind,
            "pid": job.process.pid,
            "status": job.status,
            "returncode": job.returncode,
            "cwd": job.cwd,
            "model": model,
            "timeout_seconds": job.timeout_seconds,
            "started_at": job.started_at,
            "finished_at": finished_at,
            "elapsed_seconds": round(elapsed_until - job.started_at, 3),
            "command": command_preview(job.command),
            "error": job.error,
            "commit": job.commit,
        }

    summary["session_id"] = session_id
    summary["tokens"] = _job_token_summary(tokens)

    # ---- Item 1: warnings[] from stderr scanning ----
    # Must work while the job is still RUNNING — that's the whole point. The streaming
    # threads update job.stderr incrementally, so warnings appear as soon as the child
    # writes a matching line, long before it exits.
    stderr_warnings = _scan_stderr_warnings(stderr_buf)
    if stderr_warnings:
        summary["warnings"] = stderr_warnings
        summary["warning_count"] = len(stderr_warnings)

    # ---- Item 2: files_changed for mutation jobs ----
    # A mutation job at 35 minutes with zero changed files is the real signal that
    # something went wrong. Cached with a short TTL so rapid polling doesn't spawn git.
    fc = _files_changed(job)
    if fc is not None:
        summary["files_changed"] = fc

    # ---- Item 3: suspicious flag ----
    # Flag implausible successes: returncode 0 but elapsed/tokens below floor.
    if job.status == "succeeded":
        susp = _suspicious_check(job)
        if susp:
            summary["suspicious"] = susp

    # A blocked subagent is stuck until someone answers, and nothing else in this payload
    # would reveal that - it just looks like a slow job. Surface it loudly.
    raised = concerns_for(job.id)
    if raised:
        summary["concerns"] = [
            {"severity": c["severity"], "concern": c["concern"],
             "evidence": c.get("evidence") or None, "concern_id": c["concern_id"]}
            for c in sorted(raised, key=lambda c: c.get("raised_at") or 0)
        ]
        critical = [c for c in raised if c.get("severity") == "critical"]
        if critical:
            summary["critical_concerns"] = (
                f"{len(critical)} CRITICAL concern(s) raised by this subagent - it flagged "
                "something it judged serious enough to report unprompted. Read them before "
                "accepting this job's output."
            )

    blocked = pending_questions_for(job.id)
    if blocked:
        # Separate auto-rejection notifications from genuine blocked questions. The
        # subagent is NOT waiting for an answer on auto-rejections (it already moved on),
        # so action_required must not claim the job is BLOCKED.
        real_questions = [q for q in blocked if not q.get("auto_rejection_notification")]
        notifications = [q for q in blocked if q.get("auto_rejection_notification")]
        if real_questions:
            summary["pending_questions"] = [
                {
                    "question_id": q["question_id"],
                    "question": q["question"],
                    "context": q.get("context") or None,
                    "waiting_seconds": round(time.time() - q["asked_at"], 1) if q.get("asked_at") else None,
                }
                for q in real_questions
            ]
            summary["action_required"] = (
                f"{len(real_questions)} subagent question(s) awaiting an answer - this job "
                "is BLOCKED until you call answer_agent(question_id=..., answer=...)."
            )
        if notifications:
            summary["auto_rejection_notifications"] = [
                {
                    "question_id": n["question_id"],
                    "path": n.get("context", "").split("Rejected path: ")[-1].split("\n")[0]
                        if "Rejected path: " in (n.get("context") or "") else "unknown",
                    "detail": n["question"][:200],
                }
                for n in notifications
            ]
    if session_id:
        if kind == "codex":
            # Interactive command Devon can paste into his own terminal to open the
            # SAME session, and the programmatic path for Claude to interject.
            summary["resume_command"] = f"codex resume {session_id}"
            summary["continue_with"] = (
                f"continue_codex_agent(job_id={job.id!r}, prompt=...) to interject non-interactively"
            )
        elif kind == "claude":
            summary["resume_command"] = f"claude --resume {session_id}"
        elif kind == "opencode":
            summary["resume_command"] = f"opencode run --session {session_id}"
            summary["continue_with"] = (
                f"continue_opencode_agent(job_id={job.id!r}, prompt=...) to interject"
            )
        elif kind == "kimi":
            summary["resume_command"] = f"kimi --session {session_id} -p <prompt>"
            summary["continue_with"] = (
                f"continue_kimi_agent(job_id={job.id!r}, prompt=...) to interject"
            )
        elif kind == "grok":
            summary["resume_command"] = f"grok --resume {session_id} -p <prompt>"
            summary["continue_with"] = (
                f"continue_grok_agent(job_id={job.id!r}, prompt=...) to interject"
            )
    elif kind == "codex":
        summary["session_note"] = (
            "session id not captured yet (available once the job finishes; codex exec is "
            "single-turn, so resume/continue after it completes)"
        )
    elif kind == "opencode":
        summary["session_note"] = (
            "session id not captured yet (available once the job finishes and JSON output is parsed)"
        )
    elif kind == "kimi":
        summary["session_note"] = (
            "session id not captured yet (available once the job finishes and JSON output is parsed)"
        )
    elif kind == "grok":
        summary["session_note"] = (
            "session id not captured yet (available once the job finishes and JSON output is parsed)"
        )
    return summary


def enrich_sync_result(kind: str, result: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    """Attach session_id / tokens / resume_command to a synchronous run result."""
    if meta.get("model"):
        result.setdefault("model", meta.get("model"))
    session_id = meta.get("session_id")
    try:
        if kind == "codex":
            if not session_id:
                # codex prints "session id:" to stderr, the agent reply to stdout.
                streams = f"{result.get('stderr','') or ''}\n{result.get('stdout','') or ''}"
                match = SESSION_ID_RE.search(streams)
                if match:
                    session_id = match.group(1)
            if session_id:
                result["session_id"] = session_id
                rollout = find_codex_rollout(session_id)
                info = parse_codex_rollout(rollout) if rollout else {}
                if info.get("tokens"):
                    tok = dict(info["tokens"])
                    if info.get("model_context_window"):
                        tok["model_context_window"] = info["model_context_window"]
                    result["tokens"] = _job_token_summary(tok)
                if info.get("model") and not result.get("model"):
                    result["model"] = info["model"]
                result["resume_command"] = f"codex resume {session_id}"
        elif kind == "claude":
            if session_id:
                result["session_id"] = session_id
                tok = _sum_claude_transcript_tokens(session_id)
                if tok:
                    claude_model = tok.pop("model", None)
                    if claude_model and not result.get("model"):
                        result["model"] = claude_model
                    result["tokens"] = _job_token_summary(tok)
                result["resume_command"] = f"claude --resume {session_id}"
        elif kind == "opencode":
            # For opencode, stdout is JSONL. Parse if result already has stdout.
            raw = f"{result.get('stderr','') or ''}\n{result.get('stdout','') or ''}"
            parsed = _parse_opencode_json_events(raw)
            if parsed.get("session_id"):
                result["session_id"] = parsed["session_id"]
                result["resume_command"] = f"opencode run --session {parsed['session_id']}"
            if parsed.get("usage"):
                usage = parsed["usage"]
                norm = {
                    "input_tokens": usage.get("input") or usage.get("input_tokens") or 0,
                    "output_tokens": usage.get("output") or usage.get("output_tokens") or 0,
                    "total_tokens": usage.get("total") or usage.get("total_tokens") or 0,
                }
                # Preserve reasoning/cache if present
                if usage.get("reasoning"):
                    norm["reasoning_output_tokens"] = usage["reasoning"]
                result["tokens"] = _job_token_summary(norm)
            # Replace stdout with cleaned reply for readability
            if parsed.get("reply"):
                # Keep original JSONL in a separate field if needed? For now truncate reply as stdout
                # But preserve original under raw_stdout if caller wants
                result["raw_stdout"] = result.get("stdout")
                result["stdout"] = truncate_text(parsed["reply"], 30000)
                result["reply"] = parsed["reply"]
        elif kind == "kimi":
            raw = f"{result.get('stderr','') or ''}\n{result.get('stdout','') or ''}"
            parsed = _parse_kimi_stream_json(raw)
            if parsed.get("session_id"):
                result["session_id"] = parsed["session_id"]
                result["resume_command"] = f"kimi --session {parsed['session_id']} -p <prompt>"
            if parsed.get("usage"):
                usage = parsed["usage"]
                norm = {
                    "input_tokens": usage.get("input") or usage.get("input_tokens") or usage.get("prompt_tokens") or 0,
                    "output_tokens": usage.get("output") or usage.get("output_tokens") or usage.get("completion_tokens") or 0,
                    "total_tokens": usage.get("total") or usage.get("total_tokens") or 0,
                }
                result["tokens"] = _job_token_summary(norm)
            if parsed.get("reply"):
                result["raw_stdout"] = result.get("stdout")
                result["stdout"] = truncate_text(parsed["reply"], 30000)
                result["reply"] = parsed["reply"]
        elif kind == "grok":
            raw = f"{result.get('stderr','') or ''}\n{result.get('stdout','') or ''}"
            parsed = _parse_grok_json(raw)
            if parsed.get("session_id"):
                result["session_id"] = parsed["session_id"]
                result["resume_command"] = f"grok --resume {parsed['session_id']} -p <prompt>"
            if parsed.get("usage"):
                usage = parsed["usage"]
                norm = {
                    "input_tokens": usage.get("input_tokens") or usage.get("input") or 0,
                    "output_tokens": usage.get("output_tokens") or usage.get("output") or 0,
                    "total_tokens": usage.get("total_tokens") or usage.get("total") or 0,
                    "cache_read_input_tokens": usage.get("cache_read_input_tokens") or 0,
                    "reasoning_output_tokens": usage.get("reasoning_tokens") or 0,
                }
                result["tokens"] = _job_token_summary(norm)
            if parsed.get("reply"):
                result["raw_stdout"] = result.get("stdout")
                result["stdout"] = truncate_text(parsed["reply"], 30000)
                result["reply"] = parsed["reply"]
    except Exception:  # pragma: no cover - defensive
        pass
    return result


def run_codex(args: dict[str, Any]) -> dict[str, Any]:
    command, prompt, cwd, timeout_seconds, meta = build_codex_command(args)
    enforce_delegation("codex", meta.get("model"))
    max_output_chars = optional_int(args, "max_output_chars", DEFAULT_MAX_OUTPUT_CHARS, 1000, 2_000_000)
    result = run_command("codex", command, prompt, cwd, timeout_seconds, max_output_chars)
    commit_paths = optional_string_list(args, "commit_paths")
    if commit_paths and result.get("status") == "succeeded":
        result["commit"] = git_commit_paths(cwd, commit_paths, optional_str(args, "commit_message") or "agent commit")
    enrich_sync_result("codex", result, meta)
    return tool_response(result)


def launch_codex(args: dict[str, Any]) -> dict[str, Any]:
    # The id must exist before the command is built: it goes into the -c env override so
    # the subagent's own bridge server knows which job its questions belong to.
    job_id = str(uuid.uuid4())
    cwd = resolve_cwd(args)
    # Scan and widen add_dirs BEFORE building the command (Correction 1)
    task = optional_str(args, "prompt")
    widened_dirs, sandbox_note = _prepare_subagent_sandbox(args, cwd, task)
    command, prompt, cwd, timeout_seconds, meta = build_codex_command(
        args, background=True, job_id=job_id, add_dirs_override=widened_dirs)
    commit_paths = optional_string_list(args, "commit_paths")
    commit_message = optional_str(args, "commit_message")
    return tool_response(launch_command("codex", command, prompt, cwd, timeout_seconds,
        commit_paths=commit_paths, commit_message=commit_message, meta=meta, job_id=job_id,
        task=task, sandbox_note=sandbox_note))


def run_claude(args: dict[str, Any]) -> dict[str, Any]:
    command, prompt, cwd, timeout_seconds, meta = build_claude_command(args)
    enforce_delegation("claude", meta.get("model"))
    max_output_chars = optional_int(args, "max_output_chars", DEFAULT_MAX_OUTPUT_CHARS, 1000, 2_000_000)
    result = run_command("claude", command, None if command[-1] != "-" else prompt, cwd, timeout_seconds, max_output_chars)
    enrich_sync_result("claude", result, meta)
    return tool_response(result)


def launch_claude(args: dict[str, Any]) -> dict[str, Any]:
    cwd = resolve_cwd(args)
    task = optional_str(args, "prompt")
    widened_dirs, sandbox_note = _prepare_subagent_sandbox(args, cwd, task)
    command, prompt, cwd, timeout_seconds, meta = build_claude_command(args, background=True, add_dirs_override=widened_dirs)
    return tool_response(launch_command("claude", command, None if command[-1] != "-" else prompt, cwd, timeout_seconds, meta=meta, task=task, sandbox_note=sandbox_note))


def run_opencode(args: dict[str, Any]) -> dict[str, Any]:
    command, prompt, cwd, timeout_seconds, meta = build_opencode_command(args)
    enforce_delegation("opencode", meta.get("model"))
    max_output_chars = optional_int(args, "max_output_chars", DEFAULT_MAX_OUTPUT_CHARS, 1000, 2_000_000)
    # opencode prompt is already embedded in command; no stdin feeding
    result = run_command("opencode", command, None, cwd, timeout_seconds, max_output_chars)
    # run_command captured JSONL; parse and enrich
    # parse for real reply already done in enrich_sync_result, but we also want to return parsed reply directly
    raw_out = result.get("stdout", "") + "\n" + result.get("stderr", "")
    parsed = _parse_opencode_json_events(raw_out)
    if parsed.get("reply"):
        # keep original JSONL in result before overwrite? run_command already truncated
        # We'll set stdout to parsed reply plus keep raw if needed
        pass
    enrich_sync_result("opencode", result, meta)
    return tool_response(result)


def launch_opencode(args: dict[str, Any]) -> dict[str, Any]:
    cwd = resolve_cwd(args)
    task = optional_str(args, "prompt")
    widened_dirs, sandbox_note = _prepare_subagent_sandbox(args, cwd, task)
    # Opencode has no --add-dir flag, so the grant cannot ride on the command the way it
    # does for every other client. It goes through OPENCODE_CONFIG_CONTENT instead, applied
    # in child_env - see opencode_permission_env. Same directories, different transport.
    sandbox_note["granted_via"] = "OPENCODE_CONFIG_CONTENT (permission.external_directory)"
    sandbox_note["opencode_addirs_note"] = (
        "Opencode has no --add-dir flag (--dir sets the working directory only), so these "
        "directories are granted through opencode's own permission config, injected per-job "
        "as an env var and merged over the user's config. Without it, opencode auto-rejects "
        "every path outside cwd - including STATE_DIR, which silently kills check_notes, "
        "ask_parent and raise_concern while the job still reports success."
    )
    command, prompt, cwd, timeout_seconds, meta = build_opencode_command(args, background=True, add_dirs_override=widened_dirs)
    return tool_response(launch_command("opencode", command, None, cwd, timeout_seconds, meta=meta, task=task, sandbox_note=sandbox_note, add_dirs=widened_dirs))


def continue_opencode_agent(args: dict[str, Any]) -> dict[str, Any]:
    """Interject into a previously launched opencode job by resuming its session.

    Opencode resumes via `opencode run --session <id> --format json <prompt>`
    """
    enforce_depth()
    job_id = require_str(args, "job_id")
    prompt = require_str(args, "prompt")
    job = get_job(job_id)
    if job.kind != "opencode":
        raise ValueError(f"continue_opencode_agent only works on opencode jobs; job {job_id} is {job.kind}")

    _maybe_enrich(job)
    with job.lock:
        session_id = job.session_id
        job_cwd = job.cwd
        running = job.returncode is None
        job_model = job.model
    if running:
        raise ValueError(
            "this opencode job is still running; resuming a live session would race. "
            "Wait for it to finish, then continue_opencode_agent."
        )
    if not session_id:
        raise ValueError(
            "no session id for this opencode job - it may not have produced JSON output yet. "
            "Relaunch or check agent_result for raw output."
        )

    timeout_seconds = optional_int(args, "timeout_seconds", 600, 1, 24 * 60 * 60)
    max_output_chars = optional_int(args, "max_output_chars", DEFAULT_MAX_OUTPUT_CHARS, 1000, 2_000_000)
    opencode_bin = os.environ.get("OPENCODE_BIN", "opencode")
    model = optional_str(args, "model") or job_model
    variant = optional_str(args, "variant")
    extra_args = optional_string_list(args, "extra_args")

    command = [
        opencode_bin,
        "run",
        "--format",
        "json",
        "--session",
        session_id,
        "--dir",
        job_cwd,
    ]
    if model:
        command.extend(["--model", model])
    if variant:
        command.extend(["--variant", variant])
    command.extend(extra_args)
    command.append(prompt)

    started_at = time.time()
    completed = subprocess.run(
        command,
        cwd=job_cwd,
        # A continued turn used to get NO grants at all (not even STATE_DIR),
        # so the report channel and any add_dirs died on the second turn while
        # the first turn had them - the silent-rejection class again. Re-grant
        # STATE_DIR and the (possibly spaced) cwd every continue.
        env=child_env("opencode", add_dirs=[str(STATE_DIR)], cwd=job_cwd),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    elapsed = round(time.time() - started_at, 3)
    # One more turn on a warm agent: keeps it near the top of warm_agents and
    # feeds the staleness/crowding verdicts there.
    note_reuse(job)
    parsed = _parse_opencode_json_events(completed.stdout + "\n" + completed.stderr)

    # Refresh parent job token totals
    try:
        if parsed.get("usage"):
            usage = parsed["usage"]
            norm = {
                "input_tokens": usage.get("input") or 0,
                "output_tokens": usage.get("output") or 0,
                "total_tokens": usage.get("total") or 0,
                "reasoning_output_tokens": usage.get("reasoning") or 0,
            }
            with job.lock:
                # Accumulate? For simplicity replace with latest cumulative? Actually opencode returns per-turn.
                # We'll sum if existing tokens exist.
                existing = job.tokens or {}
                merged = {
                    "input_tokens": (existing.get("input_tokens") or 0) + norm.get("input_tokens", 0),
                    "output_tokens": (existing.get("output_tokens") or 0) + norm.get("output_tokens", 0),
                    "total_tokens": (existing.get("total_tokens") or 0) + norm.get("total_tokens", 0),
                    "reasoning_output_tokens": (existing.get("reasoning_output_tokens") or 0) + norm.get("reasoning_output_tokens", 0),
                }
                job.tokens = merged
    except Exception:
        pass

    result = {
        "job_id": job_id,
        "session_id": session_id,
        "status": "succeeded" if completed.returncode == 0 else "failed",
        "returncode": completed.returncode,
        "elapsed_seconds": elapsed,
        "model": model,
        "reply": truncate_text(parsed.get("reply") or "", max_output_chars),
        "turn_tokens": parsed.get("usage"),
        "resume_command": f"opencode run --session {session_id}",
    }
    if completed.returncode != 0:
        result["stderr"] = truncate_text(completed.stderr, max_output_chars)
    return tool_response(result)


def run_kimi(args: dict[str, Any]) -> dict[str, Any]:
    command, prompt, cwd, timeout_seconds, meta = build_kimi_command(args)
    enforce_delegation("kimi", meta.get("model"))
    max_output_chars = optional_int(args, "max_output_chars", DEFAULT_MAX_OUTPUT_CHARS, 1000, 2_000_000)
    result = run_command("kimi", command, None, cwd, timeout_seconds, max_output_chars)
    enrich_sync_result("kimi", result, meta)
    return tool_response(result)


def launch_kimi(args: dict[str, Any]) -> dict[str, Any]:
    cwd = resolve_cwd(args)
    task = optional_str(args, "prompt")
    widened_dirs, sandbox_note = _prepare_subagent_sandbox(args, cwd, task)
    command, prompt, cwd, timeout_seconds, meta = build_kimi_command(args, background=True, add_dirs_override=widened_dirs)
    return tool_response(launch_command("kimi", command, None, cwd, timeout_seconds, meta=meta, task=task, sandbox_note=sandbox_note))


def continue_kimi_agent(args: dict[str, Any]) -> dict[str, Any]:
    """Interject into a previously launched kimi job by resuming its session.

    Kimi resumes via `kimi --session <id> -p <prompt> --output-format stream-json`
    """
    enforce_depth()
    job_id = require_str(args, "job_id")
    prompt = require_str(args, "prompt")
    job = get_job(job_id)
    if job.kind != "kimi":
        raise ValueError(f"continue_kimi_agent only works on kimi jobs; job {job_id} is {job.kind}")

    _maybe_enrich(job)
    with job.lock:
        session_id = job.session_id
        job_cwd = job.cwd
        running = job.returncode is None
        job_model = job.model

    if running:
        raise ValueError(
            "this kimi job is still running; resuming a live session would race. "
            "Wait for it to finish, then continue_kimi_agent."
        )
    if not session_id:
        raise ValueError(
            "no session id for this kimi job - it may not have produced one yet. "
            "Check agent_result for raw output or try again."
        )

    timeout_seconds = optional_int(args, "timeout_seconds", 600, 1, 24 * 60 * 60)
    max_output_chars = optional_int(args, "max_output_chars", DEFAULT_MAX_OUTPUT_CHARS, 1000, 2_000_000)
    kimi_bin = os.environ.get("KIMI_BIN") or os.environ.get("KIMI_CODE_BIN")
    if not kimi_bin:
        default_path = Path("~/.kimi-code/bin/kimi").expanduser()
        kimi_bin = str(default_path) if default_path.exists() else "kimi"
    model = optional_str(args, "model") or job_model
    extra_args = optional_string_list(args, "extra_args")

    command = [kimi_bin]
    if model:
        command.extend(["-m", model])
    command.extend(["--session", session_id, "-p", prompt, "--output-format", "stream-json"])
    command.extend(extra_args)

    started_at = time.time()
    completed = subprocess.run(
        command,
        cwd=job_cwd,
        env=child_env("kimi"),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    elapsed = round(time.time() - started_at, 3)
    # One more turn on a warm agent: keeps it near the top of warm_agents and
    # feeds the staleness/crowding verdicts there.
    note_reuse(job)
    parsed = _parse_kimi_stream_json(completed.stdout + "\n" + completed.stderr)

    try:
        if parsed.get("usage"):
            usage = parsed["usage"]
            norm = {
                "input_tokens": usage.get("input") or usage.get("input_tokens") or 0,
                "output_tokens": usage.get("output") or usage.get("output_tokens") or 0,
                "total_tokens": usage.get("total") or usage.get("total_tokens") or (usage.get("input", 0) + usage.get("output", 0)),
            }
            with job.lock:
                existing = job.tokens or {}
                merged = {
                    "input_tokens": (existing.get("input_tokens") or 0) + norm.get("input_tokens", 0),
                    "output_tokens": (existing.get("output_tokens") or 0) + norm.get("output_tokens", 0),
                    "total_tokens": (existing.get("total_tokens") or 0) + norm.get("total_tokens", 0),
                }
                job.tokens = merged
    except Exception:
        pass

    result = {
        "job_id": job_id,
        "session_id": session_id,
        "status": "succeeded" if completed.returncode == 0 else "failed",
        "returncode": completed.returncode,
        "elapsed_seconds": elapsed,
        "model": model,
        "reply": truncate_text(parsed.get("reply") or "", max_output_chars),
        "turn_tokens": parsed.get("usage"),
        "resume_command": f"{kimi_bin} --session {session_id} -p <prompt>",
    }
    if completed.returncode != 0:
        result["stderr"] = truncate_text(completed.stderr, max_output_chars)
    return tool_response(result)


def run_grok(args: dict[str, Any]) -> dict[str, Any]:
    command, prompt, cwd, timeout_seconds, meta = build_grok_command(args)
    enforce_delegation("grok", meta.get("model"))
    max_output_chars = optional_int(args, "max_output_chars", DEFAULT_MAX_OUTPUT_CHARS, 1000, 2_000_000)
    result = run_command("grok", command, None, cwd, timeout_seconds, max_output_chars)
    enrich_sync_result("grok", result, meta)
    return tool_response(result)


def launch_grok(args: dict[str, Any]) -> dict[str, Any]:
    cwd = resolve_cwd(args)
    task = optional_str(args, "prompt")
    widened_dirs, sandbox_note = _prepare_subagent_sandbox(args, cwd, task)
    sandbox_note["grok_addirs_note"] = (
        "Grok does not support per-directory sandbox flags (--sandbox accepts a profile "
        "name, not paths, and --allow controls tool permissions). add_dirs parameters "
        "could not be applied to the grok command."
    )
    command, prompt, cwd, timeout_seconds, meta = build_grok_command(args, background=True, add_dirs_override=widened_dirs)
    return tool_response(launch_command("grok", command, None, cwd, timeout_seconds, meta=meta, task=task, sandbox_note=sandbox_note))


def continue_grok_agent(args: dict[str, Any]) -> dict[str, Any]:
    """Resume a Grok session: grok --resume <id> -p <prompt> --output-format json"""
    enforce_depth()
    job_id = require_str(args, "job_id")
    prompt = require_str(args, "prompt")
    job = get_job(job_id)
    if job.kind != "grok":
        raise ValueError(f"continue_grok_agent only works on grok jobs; job {job_id} is {job.kind}")

    _maybe_enrich(job)
    with job.lock:
        session_id = job.session_id
        job_cwd = job.cwd
        running = job.returncode is None
        job_model = job.model

    if running:
        raise ValueError(
            "this grok job is still running; resuming live would race. Wait then continue."
        )
    if not session_id:
        raise ValueError("no session id for this grok job - check agent_result")

    timeout_seconds = optional_int(args, "timeout_seconds", 600, 1, 24 * 60 * 60)
    max_output_chars = optional_int(args, "max_output_chars", DEFAULT_MAX_OUTPUT_CHARS, 1000, 2_000_000)
    grok_bin = os.environ.get("GROK_BIN") or "/Users/devonedwards/.local/bin/grok"
    if not Path(grok_bin).exists():
        grok_bin = "grok"
    model = optional_str(args, "model") or job_model
    extra_args = optional_string_list(args, "extra_args")

    command = [grok_bin, "--resume", session_id, "-p", prompt, "--output-format", "json"]
    if model:
        command.extend(["-m", model])
    command.extend(["--cwd", job_cwd])
    command.extend(extra_args)

    started_at = time.time()
    completed = subprocess.run(
        command,
        cwd=job_cwd,
        env=child_env("grok"),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    elapsed = round(time.time() - started_at, 3)
    # One more turn on a warm agent: keeps it near the top of warm_agents and
    # feeds the staleness/crowding verdicts there.
    note_reuse(job)
    parsed = _parse_grok_json(completed.stdout + "\n" + completed.stderr)

    try:
        if parsed.get("usage"):
            usage = parsed["usage"]
            norm = {
                "input_tokens": usage.get("input_tokens") or usage.get("input") or 0,
                "output_tokens": usage.get("output_tokens") or usage.get("output") or 0,
                "total_tokens": usage.get("total_tokens") or usage.get("total") or 0,
            }
            with job.lock:
                existing = job.tokens or {}
                merged = {
                    "input_tokens": (existing.get("input_tokens") or 0) + norm.get("input_tokens", 0),
                    "output_tokens": (existing.get("output_tokens") or 0) + norm.get("output_tokens", 0),
                    "total_tokens": (existing.get("total_tokens") or 0) + norm.get("total_tokens", 0),
                }
                job.tokens = merged
    except Exception:
        pass

    result = {
        "job_id": job_id,
        "session_id": session_id,
        "status": "succeeded" if completed.returncode == 0 else "failed",
        "returncode": completed.returncode,
        "elapsed_seconds": elapsed,
        "model": model,
        "reply": truncate_text(parsed.get("reply") or "", max_output_chars),
        "turn_tokens": parsed.get("usage"),
        "resume_command": f"{grok_bin} --resume {session_id} -p <prompt>",
    }
    if completed.returncode != 0:
        result["stderr"] = truncate_text(completed.stderr, max_output_chars)
    return tool_response(result)


def _maybe_enrich(job: Job) -> None:
    """Best-effort token/session enrichment for a finished-but-not-yet-enriched job.

    Fast and non-blocking: only touches on-disk artifacts, never the job's pipes or
    process. Safe to call from the status path.
    """
    with job.lock:
        needs = job.returncode is not None and not job.enriched
    if needs:
        try:
            enrich_job(job)
        except Exception:  # pragma: no cover - defensive
            pass


def _cumulative_tokens(job_summaries: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    jobs_with_tokens = 0
    for summary in job_summaries:
        tok = summary.get("tokens")
        if not tok:
            continue
        jobs_with_tokens += 1
        totals["input_tokens"] += tok.get("input_tokens", 0) or 0
        totals["output_tokens"] += tok.get("output_tokens", 0) or 0
        totals["total_tokens"] += tok.get("total_tokens", 0) or 0
    totals["jobs_with_tokens"] = jobs_with_tokens
    return totals


def agent_status(args: dict[str, Any]) -> dict[str, Any]:
    job_id = optional_str(args, "job_id")
    if job_id:
        job = get_job(job_id)
        _maybe_enrich(job)
        return tool_response(summarize_job(job))

    # Bare agent_status (no job_id): return a COMPACT per-job summary — job_id, kind,
    # status, model, elapsed, tokens, files_changed, and the alarm fields (warnings,
    # suspicious, pending_questions, concerns) — and NEVER inline stdout/stderr. The
    # full payload is available via agent_status(job_id=...) and raw output via
    # agent_result. Cap the list at newest-first with a note when truncated.
    MAX_BARE_JOBS = 100
    with jobs_lock:
        all_jobs = sorted(jobs.values(), key=lambda j: j.started_at, reverse=True)
    for job in all_jobs:
        _maybe_enrich(job)
    truncated = len(all_jobs) > MAX_BARE_JOBS
    listed = all_jobs[:MAX_BARE_JOBS]
    summaries = [_compact_job_summary(job) for job in listed]
    result: dict[str, Any] = {
        "jobs": summaries,
        "job_count": len(listed),
        "cumulative_tokens": _cumulative_tokens(
            [_compact_job_summary(j, force_tokens=True) for j in listed]
        ),
    }
    if truncated:
        result["truncated"] = True
        result["truncated_note"] = (
            f"{len(all_jobs)} total jobs, showing {MAX_BARE_JOBS} newest. "
            "Use agent_status(job_id=...) for a specific job's full detail."
        )
    return tool_response(result)


def _compact_job_summary(job: Job, force_tokens: bool = False) -> dict[str, Any]:
    """A single-line summary for the bare agent_status listing.

    Deliberately omits stdout/stderr — those stay in agent_result. Includes the alarm
    fields so a parent scanning the list can spot trouble without drilling into each job.
    """
    with job.lock:
        finished_at = job.finished_at
        elapsed = (finished_at or time.time()) - job.started_at
        tokens = job.tokens
        stderr_buf = job.stderr
    summary: dict[str, Any] = {
        "job_id": job.id,
        "kind": job.kind,
        "status": job.status,
        "model": job.model,
        "cwd": job.cwd,
        "elapsed_seconds": round(elapsed, 1),
        "returncode": job.returncode,
    }
    if force_tokens or tokens:
        summary["tokens"] = _job_token_summary(tokens)
    # Alarm fields — these are cheap to compute and are the whole reason bare listing exists.
    fc = _files_changed(job)
    if fc is not None:
        summary["files_changed"] = fc
    warnings = _scan_stderr_warnings(stderr_buf)
    if warnings:
        summary["warnings"] = warnings[:5]  # cap at 5 for the listing
        summary["warning_count"] = len(warnings)
    if job.status == "succeeded":
        susp = _suspicious_check(job)
        if susp:
            summary["suspicious"] = susp
    blocked = pending_questions_for(job.id)
    if blocked:
        real = [q for q in blocked if not q.get("auto_rejection_notification")]
        notifs = [q for q in blocked if q.get("auto_rejection_notification")]
        if real:
            summary["pending_questions_count"] = len(real)
        if notifs:
            summary["auto_rejection_notifications_count"] = len(notifs)
    raised = concerns_for(job.id)
    if raised:
        summary["concerns_count"] = len(raised)
        critical = [c for c in raised if c.get("severity") == "critical"]
        if critical:
            summary["critical_concerns_count"] = len(critical)
    return summary


def agent_result(args: dict[str, Any]) -> dict[str, Any]:
    job_id = require_str(args, "job_id")
    max_output_chars = optional_int(args, "max_output_chars", DEFAULT_MAX_OUTPUT_CHARS, 1000, 2_000_000)
    job = get_job(job_id)
    _maybe_enrich(job)
    summary = summarize_job(job)
    with job.lock:
        summary["stdout"] = truncate_text(job.stdout, max_output_chars)
        summary["stderr"] = truncate_text(job.stderr, max_output_chars)
    return tool_response(summary)


def cancel_agent(args: dict[str, Any]) -> dict[str, Any]:
    job_id = require_str(args, "job_id")
    job = get_job(job_id)
    if job.returncode is not None:
        return tool_response(summarize_job(job))

    try:
        job.process.terminate()
        try:
            job.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.kill(job.process.pid, signal.SIGKILL)
            else:
                job.process.kill()
    finally:
        with job.lock:
            if job.returncode is None:
                job.returncode = job.process.returncode if job.process.returncode is not None else -signal.SIGTERM
                job.finished_at = time.time()

    return tool_response(summarize_job(job))


def _parse_codex_json_events(stdout: str) -> dict[str, Any]:
    """Parse `codex exec --json` JSONL output into reply text + token usage + thread id."""
    reply_parts: list[str] = []
    usage: dict[str, Any] | None = None
    thread_id: str | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = event.get("type")
        if etype == "thread.started":
            thread_id = event.get("thread_id") or thread_id
        elif etype == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                reply_parts.append(item["text"])
        elif etype == "turn.completed":
            usage = event.get("usage") or usage
    return {"reply": "\n".join(reply_parts).strip(), "usage": usage, "thread_id": thread_id}


def continue_codex_agent(args: dict[str, Any]) -> dict[str, Any]:
    """Resume a previously launched codex job's session and send a follow-up prompt.

    Lets the orchestrating Claude interject into a codex agent's conversation
    non-interactively: it runs `codex exec resume <session_id> "<prompt>"` against the
    same session the job created, and returns the agent's reply plus token usage. The
    session must be resumable - i.e. the original job was NOT launched with ephemeral=true
    (the new default persists sessions). Codex exec is single-turn, so continue after the
    job (or a prior continue) has finished its turn.
    """
    enforce_depth()
    job_id = require_str(args, "job_id")
    prompt = with_claude_md_preamble(require_str(args, "prompt"))
    job = get_job(job_id)
    if job.kind != "codex":
        raise ValueError(f"continue_codex_agent only works on codex jobs; job {job_id} is {job.kind}")

    _maybe_enrich(job)
    with job.lock:
        session_id = job.session_id
        resume_cfg = dict(job.resume or {})
        job_model = job.model
        job_cwd = job.cwd
        running = job.returncode is None
    if not session_id:
        if running:
            raise ValueError(
                "this codex job is still running and has no captured session id yet; "
                "wait for it to finish, then continue_codex_agent"
            )
        raise ValueError(
            "no session id for this job - it may have been launched with ephemeral=true "
            "(non-resumable). Relaunch without ephemeral to enable continue/resume."
        )

    timeout_seconds = optional_int(args, "timeout_seconds", 600, 1, 24 * 60 * 60)
    max_output_chars = optional_int(args, "max_output_chars", DEFAULT_MAX_OUTPUT_CHARS, 1000, 2_000_000)
    codex_bin = os.environ.get("CODEX_BIN", "codex")
    sandbox = enum_value(
        args,
        "sandbox",
        resume_cfg.get("sandbox", "workspace-write"),
        {"read-only", "workspace-write", "danger-full-access"},
    )
    model = optional_str(args, "model") or job_model
    # Resume does NOT inherit the original run's -c reasoning override; it falls back to the
    # user's config.toml default (which may be 'max', unsupported by lighter models and thus a
    # hard 400). Pin a valid effort so an interjection always goes through; caller can override.
    reasoning_effort = enum_value(
        args,
        "reasoning_effort",
        "low",
        {"none", "minimal", "low", "medium", "high", "xhigh"},
    )

    command = [
        codex_bin,
        "exec",
        "resume",
        session_id,
        "--json",
        "--skip-git-repo-check",
        "-c",
        "approval_policy=never",
        "-c",
        f"sandbox_mode={sandbox}",
        "-c",
        f"model_reasoning_effort={reasoning_effort}",
    ]
    if model:
        command.extend(["-m", model])
    command.append(prompt)

    started_at = time.time()
    completed = subprocess.run(
        command,
        cwd=job_cwd,
        env=child_env("codex"),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    elapsed = round(time.time() - started_at, 3)
    # One more turn on a warm agent: keeps it near the top of warm_agents and
    # feeds the staleness/crowding verdicts there.
    note_reuse(job)
    parsed = _parse_codex_json_events(completed.stdout)

    # Refresh the parent job's cumulative token totals from the (now-updated) rollout.
    try:
        rollout = find_codex_rollout(session_id)
        info = parse_codex_rollout(rollout) if rollout else {}
        if info.get("tokens"):
            tok = dict(info["tokens"])
            if info.get("model_context_window"):
                tok["model_context_window"] = info["model_context_window"]
            with job.lock:
                job.tokens = tok
    except Exception:  # pragma: no cover - defensive
        pass

    result = {
        "job_id": job_id,
        "session_id": session_id,
        "status": "succeeded" if completed.returncode == 0 else "failed",
        "returncode": completed.returncode,
        "elapsed_seconds": elapsed,
        "model": model,
        "reply": truncate_text(parsed["reply"], max_output_chars),
        "turn_tokens": parsed.get("usage"),
        "resume_command": f"codex resume {session_id}",
    }
    if completed.returncode != 0:
        result["stderr"] = truncate_text(completed.stderr, max_output_chars)
    return tool_response(result)


def continue_claude_agent(args: dict[str, Any]) -> dict[str, Any]:
    """Interject into a previously launched claude job by resuming its session.

    The claude counterpart of continue_codex_agent. Unlike codex - where the session id is
    only discovered from the rollout once the job finishes - claude session ids are
    pre-assigned at launch (see build_claude_command), so a resume target is known
    immediately. We still refuse while the job is running: `claude --resume` on a live
    session races the running process over the same transcript.
    """
    job_id = require_str(args, "job_id")
    prompt = require_str(args, "prompt")
    job = get_job(job_id)
    if job.kind != "claude":
        raise ValueError(f"continue_claude_agent only works on claude jobs; job {job_id} is {job.kind}")

    _maybe_enrich(job)
    with job.lock:
        session_id = job.session_id
        job_model = job.model
        job_cwd = job.cwd
        running = job.returncode is None
    if running:
        raise ValueError(
            "this claude job is still running; resuming a live session would race the "
            "running process over the same transcript. Wait for it to finish, then "
            "continue_claude_agent."
        )
    if not session_id:
        raise ValueError(
            "no session id for this job - it may have been launched with "
            "no_session_persistence=true (non-resumable). Relaunch without it to enable "
            "continue/resume."
        )

    timeout_seconds = optional_int(args, "timeout_seconds", 600, 1, 24 * 60 * 60)
    max_output_chars = optional_int(args, "max_output_chars", DEFAULT_MAX_OUTPUT_CHARS, 1000, 2_000_000)
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    permission_mode = enum_value(
        args,
        "permission_mode",
        "auto",
        {"acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"},
    )
    model = optional_str(args, "model") or job_model

    command = [
        claude_bin,
        "--print",
        "--resume",
        session_id,
        "--permission-mode",
        permission_mode,
    ]
    if model:
        command.extend(["--model", model])
    command.extend(["--", prompt])

    started_at = time.time()
    completed = subprocess.run(
        command,
        cwd=job_cwd,
        env=child_env("claude"),
        # DEVNULL, not inherited: keep the child off the server's JSON-RPC stdin.
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    elapsed = round(time.time() - started_at, 3)
    # One more turn on a warm agent: keeps it near the top of warm_agents and
    # feeds the staleness/crowding verdicts there.
    note_reuse(job)

    # Refresh the parent job's cumulative token totals from the (now-updated) transcript.
    try:
        tokens = _sum_claude_transcript_tokens(session_id)
        if tokens:
            with job.lock:
                job.tokens = tokens
    except Exception:  # pragma: no cover - defensive
        pass

    result = {
        "job_id": job_id,
        "session_id": session_id,
        "status": "succeeded" if completed.returncode == 0 else "failed",
        "returncode": completed.returncode,
        "elapsed_seconds": elapsed,
        "model": model,
        "reply": truncate_text(completed.stdout or "", max_output_chars),
        "resume_command": f"claude --resume {session_id}",
    }
    if completed.returncode != 0:
        result["stderr"] = truncate_text(completed.stderr, max_output_chars)
    return tool_response(result)


# ---------------------------------------------------------------------------
# peek_agent - live observation of a running subagent
#
# Approach differs deliberately from mkXultra/ai-cli-mcp (which inspired this tool).
# Theirs attaches a temporary listener to the child's stdout for an N-second window,
# so it shows nothing if the agent happens to be quiet and can never show work that
# happened before the call. We can't do that anyway: collect_job uses communicate(),
# which holds the pipes until exit.
#
# Instead we read the agent's own transcript, which both CLIs write incrementally:
#   claude -> ~/.claude/projects/<slug>/<session_id>.jsonl
#   codex  -> ~/.codex/sessions/YYYY/MM/DD/rollout-*-<session_id>.jsonl
# That gives full history plus a real line cursor, so successive peeks return only
# what's new instead of re-reading a window.
# ---------------------------------------------------------------------------


def _find_claude_transcript(session_id: str) -> Path | None:
    try:
        matches = sorted(
            find_claude_projects_dir().glob(f"*/{session_id}.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except (OSError, FileNotFoundError):
        return None
    return matches[0] if matches else None


def _find_codex_rollout_live(job: Job) -> Path | None:
    """Locate a running codex job's rollout before its session id is known.

    codex only reveals the session id once the job finishes, so mid-flight we match on
    (started after this job did) AND (session_meta.cwd == the job's cwd), newest first.
    """
    try:
        candidates = [
            p for p in CODEX_SESSIONS_DIR.glob("**/rollout-*.jsonl")
            if p.stat().st_mtime >= job.started_at - 120
        ]
    except OSError:
        return None
    for path in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for _ in range(5):
                    line = handle.readline()
                    if not line:
                        break
                    try:
                        entry = json.loads(line) or {}
                    except json.JSONDecodeError:
                        continue
                    payload = entry.get("payload") or {}
                    # `session_meta` is a TOP-LEVEL entry type; its payload carries cwd but
                    # has no "type" key of its own (unlike event_msg/response_item entries).
                    if entry.get("type") == "session_meta" or payload.get("type") == "session_meta":
                        meta_cwd = payload.get("cwd") or entry.get("cwd")
                        if meta_cwd and str(Path(meta_cwd).resolve()) == job.cwd:
                            return path
        except OSError:
            continue
    return None


def _resolve_transcript(job: Job) -> Path | None:
    with job.lock:
        cached = job.transcript_path
        session_id = job.session_id
        kind = job.kind
    if cached and Path(cached).exists():
        return Path(cached)

    path: Path | None = None
    if kind == "claude" and session_id:
        path = _find_claude_transcript(session_id)
    elif kind == "codex":
        path = find_codex_rollout(session_id) if session_id else None
        if path is None:
            path = _find_codex_rollout_live(job)
    if path is not None:
        with job.lock:
            job.transcript_path = str(path)
    return path


def _summarize(value: Any, limit: int = 200) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


def _claude_events(entry: dict[str, Any], include_tool_calls: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    etype = entry.get("type")
    if etype not in ("assistant", "user"):
        return out
    message = entry.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        if content.strip():
            out.append({"kind": "message", "role": etype, "text": _summarize(content, 4000)})
        return out
    if not isinstance(content, list):
        return out
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" and block.get("text", "").strip():
            out.append({"kind": "message", "role": etype, "text": _summarize(block["text"], 4000)})
        elif btype == "thinking" and include_tool_calls and block.get("thinking", "").strip():
            out.append({"kind": "thinking", "text": _summarize(block["thinking"], 600)})
        elif btype == "tool_use" and include_tool_calls:
            name = block.get("name", "?")
            out.append({"kind": "tool_call", "tool": name, "summary": _summarize(block.get("input", {}))})
        elif btype == "tool_result" and include_tool_calls:
            out.append({"kind": "tool_result", "summary": _summarize(block.get("content", ""))})
    return out


def _codex_output_text(output: Any) -> str:
    """function_call_output carries a list of {type,text} blocks; custom_* a plain string."""
    if isinstance(output, list):
        return " ".join(
            block.get("text", "") for block in output
            if isinstance(block, dict) and block.get("text")
        )
    return output if isinstance(output, str) else _summarize(output)


def _codex_events(entry: dict[str, Any], include_tool_calls: bool) -> list[dict[str, Any]]:
    payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
    ptype = payload.get("type")

    # Assistant prose arrives as event_msg/agent_message (payload.message).
    if ptype == "agent_message" and (payload.get("message") or payload.get("text")):
        return [{"kind": "message", "role": "assistant",
                 "text": _summarize(payload.get("message") or payload.get("text"), 4000)}]
    if ptype == "user_message" and (payload.get("message") or payload.get("text")):
        return [{"kind": "message", "role": "user",
                 "text": _summarize(payload.get("message") or payload.get("text"), 1000)}]
    if ptype in ("task_started", "task_complete"):
        return [{"kind": "status", "text": ptype}]

    if not include_tool_calls:
        return []
    # Codex emits shell work as custom_tool_call (payload.input, a JS snippet) and plain
    # tool calls as function_call (payload.arguments, a JSON string); both have *_output
    # counterparts keyed by call_id.
    if ptype in ("custom_tool_call", "function_call"):
        return [{"kind": "tool_call", "tool": payload.get("name") or ptype,
                 "summary": _summarize(payload.get("input") or payload.get("arguments") or payload)}]
    if ptype in ("custom_tool_call_output", "function_call_output"):
        return [{"kind": "tool_result", "summary": _summarize(_codex_output_text(payload.get("output")))}]
    if ptype == "reasoning":
        # `summary` is usually [] and encrypted_content is opaque - emit only real text.
        parts = [s.get("text", "") for s in (payload.get("summary") or []) if isinstance(s, dict)]
        text = " ".join(p for p in parts if p)
        return [{"kind": "thinking", "text": _summarize(text, 600)}] if text.strip() else []
    return []


def _opencode_events(entry: dict[str, Any], include_tool_calls: bool) -> list[dict[str, Any]]:
    """Normalize opencode JSON events to peek_agent event format."""
    out: list[dict[str, Any]] = []
    etype = entry.get("type")
    if etype == "text":
        part = entry.get("part") or {}
        text = part.get("text") or ""
        if text.strip():
            out.append({"kind": "message", "role": "assistant", "text": _summarize(text, 4000)})
    elif etype == "step_start":
        out.append({"kind": "status", "text": "step_start"})
    elif etype == "step_finish":
        part = entry.get("part") or {}
        reason = part.get("reason") or "step_finish"
        out.append({"kind": "status", "text": reason})
        if include_tool_calls and part.get("tokens"):
            out.append({"kind": "tool_result", "summary": _summarize(part.get("tokens"))})
    elif etype == "tool_call":
        if include_tool_calls:
            part = entry.get("part") or {}
            out.append({"kind": "tool_call", "tool": part.get("tool") or etype, "summary": _summarize(part)})
    elif etype == "tool_result":
        if include_tool_calls:
            part = entry.get("part") or {}
            out.append({"kind": "tool_result", "summary": _summarize(part)})
    return out


def warm_agents(args: dict[str, Any]) -> dict[str, Any]:
    """List agents that are still resumable, so a warm one can be reused.

    The point of the tool is to make reuse the easy path. A fresh agent starts from
    nothing: it re-reads the same files, rediscovers the same layout, and re-earns
    the same corrections before it is as useful as one that already finished a turn
    on this problem. Where that context is the expensive part, resuming is strictly
    cheaper than replacing.

    It is not unconditional, though - see roster_verdict. Each entry says whether it
    is worth resuming and why, so a stale or crowded session can be retired instead
    of dragged forward past its usefulness.
    """
    kind = optional_str(args, "kind")
    cwd = optional_str(args, "cwd")
    include_retired = optional_bool(args, "include_retired", False)
    reusable_only = optional_bool(args, "reusable_only", False)
    limit = optional_int(args, "limit", 20, 1, 200)

    # Anything running or finished in THIS process may not be on disk yet (the roster
    # is written when a job finishes), so fold live jobs in rather than miss them.
    with jobs_lock:
        live = list(jobs.values())
    for job in live:
        save_to_roster(job)

    resolved_cwd = str(Path(cwd).expanduser().resolve()) if cwd else None
    entries: list[dict[str, Any]] = []
    for record in _iter_roster():
        if kind and record.get("kind") != kind:
            continue
        if resolved_cwd and record.get("cwd") != resolved_cwd:
            continue
        if record.get("retired") and not include_retired:
            continue
        verdict, why = roster_verdict(record)
        if reusable_only and verdict != "reuse":
            continue
        age_seconds = time.time() - (record.get("last_used_at") or 0)
        entries.append({
            "job_id": record.get("job_id"),
            "kind": record.get("kind"),
            "model": record.get("model"),
            "cwd": record.get("cwd"),
            "task": (record.get("task") or "").strip()[:200],
            "turns": record.get("turns") or 1,
            "idle_minutes": round(age_seconds / 60),
            "last_status": record.get("status"),
            "tokens": _job_token_summary(record.get("tokens")),
            "verdict": verdict,
            "why": why,
            "continue_with": f"continue_{record.get('kind')}_agent",
            "concerns": len(concerns_for(record.get("job_id") or "")),
        })
        if len(entries) >= limit:
            break

    reusable = [e for e in entries if e["verdict"] == "reuse"]
    return tool_response({
        "count": len(entries),
        "reusable": len(reusable),
        "agents": entries,
        "guidance": (
            "Prefer resuming an agent whose verdict is 'reuse' over launching a fresh one "
            "for related work: it already holds the context, so you pay for the new turn "
            "instead of re-teaching the problem. Match on cwd and on what the task line "
            "says it was doing - a warm agent pointed at the wrong problem is worse than "
            "a cold one, because its old framing follows it. Do NOT resume one whose "
            "verdict is 'stale' or 'crowded' without re-briefing it, and use retire_agent "
            "on any that has stopped being worth its context."
            if entries else
            "No resumable agents on file. Launch a fresh one; it will be recorded here "
            "when it finishes, so the next related task can resume it instead."
        ),
    })


def retire_agent(args: dict[str, Any]) -> dict[str, Any]:
    """Mark a warm agent as no longer worth reusing - the 'to a point' in reuse.

    Reuse has a ceiling: a session can go stale against a moving repo, accumulate so
    much context that re-reading it costs more than a fresh start, or simply go wrong
    in a way that would poison whatever it touches next. Retiring is how that gets
    said out loud, instead of leaving a bad agent at the top of the warm list where
    its warmth reads as an endorsement.

    The underlying session is untouched - `claude --resume` / `codex resume` still
    work by hand. This only removes it from what the bridge recommends.
    """
    job_id = require_str(args, "job_id")
    reason = optional_str(args, "reason") or "retired without a stated reason"
    unretire = optional_bool(args, "unretire", False)

    job = get_job(job_id)
    with job.lock:
        job.retired = not unretire
        job.retired_reason = None if unretire else reason
    save_to_roster(job)
    return tool_response({
        "job_id": job_id,
        "retired": not unretire,
        "reason": None if unretire else reason,
        "note": (
            "Back in the warm list and available to continue_*."
            if unretire else
            "Dropped from warm_agents recommendations. The session itself is untouched - "
            f"resume it by hand with `{'claude --resume' if job.kind == 'claude' else 'codex resume'} "
            f"{job.session_id}` if you change your mind, or call retire_agent with "
            "unretire=true."
        ),
    })


def peek_agent(args: dict[str, Any]) -> dict[str, Any]:
    """Read what a subagent has done so far, from its own transcript, without waiting."""
    job_id = require_str(args, "job_id")
    since = optional_int(args, "since", 0, 0, 10_000_000)
    limit = optional_int(args, "limit", 30, 1, 500)
    include_tool_calls = optional_bool(args, "include_tool_calls", True)
    job = get_job(job_id)
    _maybe_enrich(job)

    with job.lock:
        kind, status, session_id = job.kind, job.status, job.session_id

    # Opencode/Kimi/Grok stdout is now streamed incrementally (item 0), so peek works
    # live while the job is running — no transcript file needed. The cursor contract uses
    # line indices into the buffered stdout.
    if kind in ("opencode", "kimi", "grok"):
        with job.lock:
            raw = job.stdout or ""
        if not raw:
            return tool_response({
                "job_id": job_id, "status": status, "events": [], "cursor": since,
                "note": (
                    f"{kind} job has no stdout yet (still starting) - retry shortly. "
                    "Stdout is streamed incrementally, so events will appear as soon as "
                    "the agent starts writing output."
                ),
            })
        events: list[dict[str, Any]] = []
        line_no = 0
        # Choose parser based on kind
        def parse_line(entry_dict, inc, k=kind):
            if k == "opencode":
                return _opencode_events(entry_dict, inc)
            elif k == "grok":
                out = []
                txt = entry_dict.get("text")
                if isinstance(txt, str) and txt.strip():
                    out.append({"kind": "message", "role": "assistant", "text": _summarize(txt, 4000)})
                # streaming-json or other shapes
                if entry_dict.get("role") == "assistant":
                    content = entry_dict.get("content")
                    if isinstance(content, str) and content.strip():
                        out.append({"kind": "message", "role": "assistant", "text": _summarize(content, 4000)})
                if inc and entry_dict.get("tool_calls"):
                    for tc in entry_dict.get("tool_calls", []):
                        name = (tc.get("function") or {}).get("name") or tc.get("name") or "tool"
                        out.append({"kind": "tool_call", "tool": name, "summary": _summarize(tc)})
                return out
            else:
                # kimi and generic
                out = []
                if entry_dict.get("role") == "assistant":
                    content = entry_dict.get("content")
                    if isinstance(content, str) and content.strip():
                        out.append({"kind": "message", "role": "assistant", "text": _summarize(content, 4000)})
                    elif isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict):
                                t = item.get("text") or item.get("content")
                                if t:
                                    out.append({"kind": "message", "role": "assistant", "text": _summarize(t, 4000)})
                if inc and entry_dict.get("tool_calls"):
                    for tc in entry_dict["tool_calls"]:
                        name = (tc.get("function") or {}).get("name") or tc.get("name") or "tool"
                        out.append({"kind": "tool_call", "tool": name, "summary": _summarize(tc)})
                return out

        for line_no, line in enumerate(raw.splitlines(), start=1):
            if line_no <= since:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                # plain text fallback
                if line.strip():
                    events.append({"kind": "message", "role": "assistant", "text": _summarize(line, 4000), "line": line_no})
                continue
            if kind == "opencode":
                parsed = _opencode_events(entry, include_tool_calls)
            else:
                parsed = parse_line(entry, include_tool_calls, kind)
                # also try opencode parser as fallback
                if not parsed:
                    parsed = _opencode_events(entry, include_tool_calls)
                # and grok json fallback
                if not parsed and kind == "grok" and entry.get("text"):
                    parsed = [{"kind": "message", "role": "assistant", "text": _summarize(entry.get("text"), 4000)}]
            for ev in parsed:
                ev["line"] = line_no
            events.extend(parsed)
        truncated = len(events) > limit
        if truncated:
            events = events[-limit:]
        return tool_response({
            "job_id": job_id,
            "kind": kind,
            "status": status,
            "session_id": session_id,
            "transcript": f"{kind} stdout buffer (live)",
            "events": events,
            "event_count": len(events),
            "truncated_older": truncated,
            "cursor": line_no,
        })

    path = _resolve_transcript(job)
    if path is None:
        return tool_response({
            "job_id": job_id, "status": status, "events": [], "cursor": since,
            "note": (
                "no transcript located yet. A codex job's rollout appears a few seconds after "
                "launch; a claude job's transcript appears once the session writes its first "
                "turn. Retry shortly." if status == "running" else
                "no transcript found - the job may have been launched non-persistently "
                "(ephemeral / no_session_persistence), which leaves nothing to read."
            ),
        })

    events: list[dict[str, Any]] = []
    line_no = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_no, line in enumerate(handle, start=1):
                if line_no <= since:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                parsed = (_claude_events if kind == "claude" else _codex_events)(entry, include_tool_calls)
                for event in parsed:
                    event["line"] = line_no
                events.extend(parsed)
    except OSError as exc:
        raise ValueError(f"could not read transcript {path}: {exc}") from exc

    truncated = len(events) > limit
    if truncated:
        # Keep the NEWEST events - what the agent is doing now is what matters.
        events = events[-limit:]

    return tool_response({
        "job_id": job_id,
        "kind": kind,
        "status": status,
        "session_id": session_id,
        "transcript": str(path),
        "events": events,
        "event_count": len(events),
        "truncated_older": truncated,
        # Feed back as `since` to get only what's new on the next peek.
        "cursor": line_no,
    })


def find_codex_state_db() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    candidates = sorted(codex_home.glob("state_*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"no state_*.sqlite found under {codex_home}")
    return candidates[0]


def codex_usage(args: dict[str, Any]) -> dict[str, Any]:
    since_hours = optional_int(args, "since_hours", 24 * 7, 0, 24 * 365 * 5)
    db_path = find_codex_state_db()
    cutoff = int(time.time()) - since_hours * 3600 if since_hours > 0 else 0

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        by_model = conn.execute(
            """
            SELECT COALESCE(model, 'unknown') AS model,
                   COUNT(*) AS sessions,
                   COALESCE(SUM(tokens_used), 0) AS tokens
            FROM threads
            WHERE created_at >= ?
            GROUP BY model
            ORDER BY tokens DESC
            """,
            (cutoff,),
        ).fetchall()
        totals = conn.execute(
            "SELECT COUNT(*) AS sessions, COALESCE(SUM(tokens_used), 0) AS tokens FROM threads WHERE created_at >= ?",
            (cutoff,),
        ).fetchone()
    finally:
        conn.close()

    return tool_response(
        {
            "source": str(db_path),
            "since_hours": since_hours if since_hours > 0 else "all-time",
            "by_model": [dict(row) for row in by_model],
            "total_sessions": totals["sessions"],
            "total_tokens": totals["tokens"],
            "note": (
                "Local per-thread token totals only, for every Codex session on this machine "
                "(bridge-launched or interactive). This is NOT the same as OpenAI's ChatGPT-plan "
                "rate limits (rolling 5-hour and weekly caps) - those percentages are only exposed "
                "by the `/status` command in an interactive `codex` TUI session, or the usage "
                "dashboard at chatgpt.com/codex. No CLI/API/app-server method currently returns "
                "them programmatically (open feature requests: openai/codex#15281, #20310)."
            ),
        }
    )


ANSI_CSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]")
ANSI_OSC_RE = re.compile(rb"\x1b\][^\x07]*\x07")

STATUS_FIELD_RES = {
    "model": re.compile(r"Model:\s*([^\r\n(]+?)\s*\(([^)]*)\)"),
    "account": re.compile(r"Account:\s*(\S+)\s*\(([^)]*)\)"),
    "5h": re.compile(r"5h limit:\s*\[[^\]]*\]\s*(\d+)% left\s*\(resets ([^)]*)\)"),
    "weekly": re.compile(r"Weekly limit:\s*\[[^\]]*\]\s*(\d+)% left\s*\(resets ([^)]*)\)"),
}


def _pty_pump(fd: int, buf_holder: list[bytes], seconds: float, stop_substr: str | None = None) -> bool:
    """Read from fd for up to `seconds`, appending to buf_holder[0]. Returns False if fd closed."""
    start = time.time()
    while time.time() - start < seconds:
        r, _, _ = _select.select([fd], [], [], 0.2)
        if fd in r:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                return False
            if not chunk:
                return False
            buf_holder[0] += chunk
        if stop_substr and stop_substr.encode() in buf_holder[0]:
            return True
    return True


def codex_status(args: dict[str, Any]) -> dict[str, Any]:
    """Drive an interactive `codex` TUI in a pty just long enough to run /status.

    There is no CLI/API/app-server method that returns ChatGPT-plan rate limits
    (see codex_usage's note) - /status in the interactive TUI is the only place
    that renders them, so this automates answering the trust prompt and typing
    /status, then screen-scrapes the rendered panel. This is UI automation, not
    a stable API - it can break on a Codex TUI redesign.
    """
    if pty is None:
        raise RuntimeError("codex_status requires a POSIX pty and is unavailable on this platform")

    cwd = resolve_cwd(args)
    codex_bin = os.environ.get("CODEX_BIN", "codex")
    timeout_seconds = optional_int(args, "timeout_seconds", 30, 5, 120)

    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        os.execvp(codex_bin, [codex_bin, "--no-alt-screen", "-c", "mcp_servers={}"])
        os._exit(127)  # pragma: no cover - only on exec failure

    try:
        import fcntl
        import struct
        import termios

        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 140, 0, 0))
        os.set_blocking(fd, False)

        buf = [b""]
        deadline = time.time() + timeout_seconds

        _pty_pump(fd, buf, min(6, max(1, deadline - time.time())), stop_substr="trust")
        if b"Yes, continue" in buf[0]:
            os.write(fd, b"\r")

        _pty_pump(fd, buf, max(1, deadline - time.time()), stop_substr="/model to change")
        time.sleep(0.5)
        os.write(fd, b"/status")
        time.sleep(0.3)
        os.write(fd, b"\r")
        _pty_pump(fd, buf, max(1, deadline - time.time()), stop_substr="Weekly limit")

        clean_bytes = ANSI_CSI_RE.sub(b"", buf[0])
        clean_bytes = ANSI_OSC_RE.sub(b"", clean_bytes)
        clean = clean_bytes.decode("utf-8", errors="replace")
    finally:
        # The pty child is a session/process-group leader (setsid) whose npm wrapper
        # spawns further children (the real binary, plugin subprocesses). Killing only
        # the leader pid leaves those alive and can make a plain blocking waitpid hang
        # indefinitely even though the leader itself already died - so signal the whole
        # group and reap non-blockingly with a bound instead of trusting waitpid(pid, 0).
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        reap_deadline = time.time() + 2.0
        while time.time() < reap_deadline:
            try:
                reaped_pid, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                break
            if reaped_pid == pid:
                break
            time.sleep(0.1)
        try:
            os.close(fd)
        except OSError:
            pass

    parsed: dict[str, Any] = {}
    model_match = STATUS_FIELD_RES["model"].search(clean)
    if model_match:
        parsed["model"] = model_match.group(1).strip()
        parsed["reasoning"] = model_match.group(2).strip()
    account_match = STATUS_FIELD_RES["account"].search(clean)
    if account_match:
        parsed["account"] = account_match.group(1).strip()
        parsed["plan"] = account_match.group(2).strip()
    five_h_match = STATUS_FIELD_RES["5h"].search(clean)
    if five_h_match:
        parsed["five_hour_percent_left"] = int(five_h_match.group(1))
        parsed["five_hour_resets"] = five_h_match.group(2).strip()
    weekly_match = STATUS_FIELD_RES["weekly"].search(clean)
    if weekly_match:
        parsed["weekly_percent_left"] = int(weekly_match.group(1))
        parsed["weekly_resets"] = weekly_match.group(2).strip()

    if not five_h_match or not weekly_match:
        return tool_response(
            {
                "ok": False,
                "reason": "could not find rate-limit lines in the captured screen; the TUI may not have "
                "reached the /status panel in time, or its layout changed",
                "parsed": parsed,
                "raw_tail": clean[-4000:],
            }
        )

    return tool_response(
        {
            "ok": True,
            "note": "Screen-scraped from the interactive `codex` TUI's /status command via a pty - "
            "not a documented/stable API. Reflects real ChatGPT-plan usage at the moment this ran.",
            **parsed,
        }
    )


USAGE_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def find_claude_projects_dir() -> Path:
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
    projects = base / "projects"
    if not projects.is_dir():
        raise FileNotFoundError(f"no projects dir found under {base}")
    return projects


def _parse_iso_timestamp(ts: str) -> float | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _empty_usage_bucket() -> dict[str, int]:
    return {token_field: 0 for token_field in USAGE_TOKEN_FIELDS} | {"assistant_messages": 0}


def claude_usage(args: dict[str, Any]) -> dict[str, Any]:
    """Sum token usage from this machine's local Claude Code session transcripts.

    Every assistant turn (including the one currently running) is written to
    ~/.claude/projects/<project>/<session>.jsonl with a real usage object as it
    happens - this reads that directly rather than approximating from a fresh,
    unrelated nested session. It cannot compute a rate-limit percentage though:
    the plan's exact quota thresholds aren't stored locally. For that, run
    /usage in an interactive `claude` session (same limitation as Codex's
    5h/weekly rate limits - no CLI/API exposes the percentage directly).
    """
    since_hours = optional_int(args, "since_hours", 24 * 7, 0, 24 * 365 * 5)
    projects_dir = find_claude_projects_dir()
    cutoff_epoch = time.time() - since_hours * 3600 if since_hours > 0 else 0

    by_model: dict[str, dict[str, int]] = {}
    total = _empty_usage_bucket()
    sessions_scanned = 0

    for jsonl_path in projects_dir.glob("*/*.jsonl"):
        try:
            if since_hours > 0 and jsonl_path.stat().st_mtime < cutoff_epoch:
                continue
        except OSError:
            continue
        sessions_scanned += 1
        try:
            with jsonl_path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("type") != "assistant":
                        continue
                    if since_hours > 0:
                        msg_epoch = _parse_iso_timestamp(entry.get("timestamp", ""))
                        if msg_epoch is not None and msg_epoch < cutoff_epoch:
                            continue
                    message = entry.get("message") or {}
                    usage = message.get("usage")
                    if not usage:
                        continue
                    model = message.get("model", "unknown")
                    bucket = by_model.setdefault(model, _empty_usage_bucket())
                    for key in USAGE_TOKEN_FIELDS:
                        value = usage.get(key, 0) or 0
                        bucket[key] += value
                        total[key] += value
                    bucket["assistant_messages"] += 1
                    total["assistant_messages"] += 1
        except OSError:
            continue

    return tool_response(
        {
            "source": str(projects_dir),
            "since_hours": since_hours if since_hours > 0 else "all-time",
            "sessions_scanned": sessions_scanned,
            "by_model": by_model,
            "total": total,
            "note": (
                "Local token totals summed from this machine's Claude Code session transcripts, "
                "including the currently running session. This is NOT the same as Anthropic's actual "
                "rate-limit percentage (5-hour/weekly) - the plan's exact quota thresholds aren't "
                "stored locally, so no percentage can be derived here. For that, run /usage in an "
                "interactive `claude` session."
            ),
        }
    )


STATUSLINE_CACHE_FILE = Path("~/.claude/statusline_cache.json").expanduser()
CODEX_STATUS_CACHE_FILE = Path("~/.claude/codex_status_cache.json").expanduser()
CODEX_STATUS_REFRESH_SCRIPT = Path("~/.claude/codex_status_refresh.py").expanduser()
# Beyond this age the cached Codex numbers are considered stale; route_status kicks off
# a non-blocking background refresh and labels the returned data loudly.
CODEX_STATUS_STALE_SECONDS = 15 * 60

_last_bg_refresh_at = 0.0
_bg_refresh_lock = threading.Lock()


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _trigger_codex_status_refresh() -> bool:
    """Fire the standalone codex status refresh script in the background (non-blocking).

    Debounced so repeated route_status calls don't spawn a swarm of refreshers. Returns
    True if a refresh was actually launched. Never blocks and never raises.
    """
    global _last_bg_refresh_at
    with _bg_refresh_lock:
        now = time.time()
        if now - _last_bg_refresh_at < 120:  # at most one background refresh / 2 min
            return False
        if not CODEX_STATUS_REFRESH_SCRIPT.exists():
            return False
        _last_bg_refresh_at = now
    try:
        subprocess.Popen(
            [sys.executable, str(CODEX_STATUS_REFRESH_SCRIPT)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception:  # pragma: no cover - defensive
        return False


def _humanize_age(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes}m ago"
    hours = minutes / 60
    return f"{hours:.1f}h ago"


def route_status(args: dict[str, Any]) -> dict[str, Any]:
    """Fast, cache-based rate-limit snapshot for both providers, for routing decisions.

    Unlike codex_status (a ~15-30s live pty scrape of the Codex TUI), this just reads two
    files that are kept fresh independently of this MCP server and of any particular
    Claude session, so it works no matter which project/cwd you're currently in:

    - ~/.claude/statusline_cache.json: Claude's own rate_limits, refreshed by the Claude
      Code harness itself on every turn (the statusLine hook) - only present after the
      first API response in a session.
    - ~/.claude/codex_status_cache.json: Codex's rate limits, refreshed every 5 minutes by
      a standalone launchd job (codexstatusrefresh -> codex_status_refresh.py), since Codex
      has no equivalent live hook to tap into.

    Near-instant, at the cost of the Codex numbers being up to ~5 minutes stale (see
    codex.stale_seconds). Use this for routine "who has headroom right now" checks before
    delegating work; fall back to codex_status only when you need a guaranteed-fresh read.
    """
    result: dict[str, Any] = {}

    statusline_data = _read_json_file(STATUSLINE_CACHE_FILE)
    if statusline_data:
        rate_limits = statusline_data.get("rate_limits") or {}
        five_hour = rate_limits.get("five_hour") or {}
        seven_day = rate_limits.get("seven_day") or {}
        if five_hour or seven_day:
            result["claude"] = {
                "five_hour_used_percentage": five_hour.get("used_percentage"),
                "five_hour_resets_at": five_hour.get("resets_at"),
                "seven_day_used_percentage": seven_day.get("used_percentage"),
                "seven_day_resets_at": seven_day.get("resets_at"),
                "source": str(STATUSLINE_CACHE_FILE),
            }
        else:
            result["claude_note"] = (
                "statusline cache exists but has no rate_limits yet - only present after "
                "the first API response in a session"
            )
    else:
        result["claude_note"] = f"no statusline cache found at {STATUSLINE_CACHE_FILE} yet"

    codex_data = _read_json_file(CODEX_STATUS_CACHE_FILE)
    if codex_data and codex_data.get("ok"):
        fetched_at = codex_data.get("fetched_at")
        stale_seconds = (time.time() - fetched_at) if fetched_at else None
        is_stale = stale_seconds is not None and stale_seconds > CODEX_STATUS_STALE_SECONDS
        codex_block = {
            "five_hour_percent_left": codex_data.get("five_hour_percent_left"),
            "five_hour_resets": codex_data.get("five_hour_resets"),
            "weekly_percent_left": codex_data.get("weekly_percent_left"),
            "weekly_resets": codex_data.get("weekly_resets"),
            "model": codex_data.get("model"),
            "plan": codex_data.get("plan"),
            "fetched_at": fetched_at,
            "age": _humanize_age(stale_seconds),
            "stale_seconds": round(stale_seconds) if stale_seconds is not None else None,
            "is_stale": is_stale,
            "source": str(CODEX_STATUS_CACHE_FILE),
        }
        if is_stale:
            refreshed = _trigger_codex_status_refresh()
            codex_block["freshness_warning"] = (
                f"STALE - these Codex numbers are {_humanize_age(stale_seconds)} "
                f"(> {CODEX_STATUS_STALE_SECONDS // 60}m threshold). "
                + (
                    "A background refresh was just kicked off; re-run route_status in ~30-60s "
                    "for current numbers, or call codex_status for a guaranteed-fresh live read."
                    if refreshed
                    else "A background refresh is already in flight or the refresher is missing; "
                    "re-run route_status shortly, or call codex_status for a live read."
                )
            )
        result["codex"] = codex_block
    else:
        _trigger_codex_status_refresh()
        result["codex_note"] = (
            f"no valid Codex status cache at {CODEX_STATUS_CACHE_FILE} yet - the "
            "codexstatusrefresh launchd job may not have run yet or isn't installed. "
            "A background refresh was requested; re-run route_status shortly."
        )

    return tool_response(result)


TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "run_codex_agent": run_codex,
    "launch_codex_agent": launch_codex,
    "run_claude_agent": run_claude,
    "launch_claude_agent": launch_claude,
    "run_opencode_agent": run_opencode,
    "launch_opencode_agent": launch_opencode,
    "run_kimi_agent": run_kimi,
    "launch_kimi_agent": launch_kimi,
    "run_grok_agent": run_grok,
    "launch_grok_agent": launch_grok,
    "agent_status": agent_status,
    "agent_result": agent_result,
    "cancel_agent": cancel_agent,
    "continue_codex_agent": continue_codex_agent,
    "continue_claude_agent": continue_claude_agent,
    "continue_opencode_agent": continue_opencode_agent,
    "continue_kimi_agent": continue_kimi_agent,
    "continue_grok_agent": continue_grok_agent,
    "peek_agent": peek_agent,
    "warm_agents": warm_agents,
    "retire_agent": retire_agent,
    "ask_parent": ask_parent,
    "pending_questions": pending_questions,
    "answer_agent": answer_agent,
    "escalate_question": escalate_question,
    "raise_concern": raise_concern,
    "list_concerns": list_concerns,
    "send_note": send_note,
    "check_notes": check_notes,
    "codex_usage": codex_usage,
    "codex_status": codex_status,
    "claude_usage": claude_usage,
    "route_status": route_status,
}


def tool_schema() -> list[dict[str, Any]]:
    prompt_schema = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "Instructions for the subagent. Give it what you would want if you were the "
                    "one receiving this task and could not see the rest of the conversation: "
                    "the purpose behind the task, what its output feeds into, which judgement "
                    "calls are its own to make, and any constraint that would be expensive to "
                    "discover late. A subagent with context produces better work and asks "
                    "better questions; one handed a decontextualized fragment has to guess at "
                    "what you meant, and will usually guess plausibly and wrongly."
                ),
            },
            "cwd": {
                "type": "string",
                "description": "Working directory for the subagent. Defaults to the MCP server cwd.",
            },
            "multi_phase": {
                "type": "boolean",
                "default": True,
                "description": (
                    "Does this task have distinct phases? Set false for one-shot work - the "
                    "mid-flight note channel is then dropped from the subagent's briefing, "
                    "since a short job finishes before it would ever check."
                ),
            },
            "preamble_sections": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["core", "abort", "escalate", "delegate", "notes", "concerns", "standing"],
                },
                "description": (
                    "Advanced: trim the subagent's briefing to these sections. Omit for automatic "
                    "selection, which is what you want almost always. 'core' (the question "
                    "channel), 'concerns' and 'standing' are cheap and broadly useful; 'abort' "
                    "matters when a wrong guess is destructive; 'notes' only on multi-phase work. "
                    "'escalate' and 'delegate' are dropped automatically when the subagent would "
                    "be at the recursion ceiling and so cannot launch anything."
                ),
            },
            "model": {"type": "string", "description": "Optional model override."},
            "timeout_seconds": {
                "type": "integer",
                "minimum": 1,
                "maximum": 86400,
                "default": DEFAULT_TIMEOUT_SECONDS,
            },
            "max_output_chars": {
                "type": "integer",
                "minimum": 1000,
                "maximum": 2000000,
                "default": DEFAULT_MAX_OUTPUT_CHARS,
            },
            "add_dirs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Additional directories to make available to the subagent.",
            },
            "extra_args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Advanced CLI arguments appended before the prompt/stdin marker.",
            },
        },
        "required": ["prompt"],
        "additionalProperties": False,
    }

    codex_props = dict(prompt_schema["properties"])
    codex_props["model"] = {
        "type": "string",
        "description": (
            "Optional model override. Cheaper/faster models exist alongside the frontier "
            "default (e.g. lightweight 'mini' and fast/affordable tiers) - pick a lighter one "
            "for simple or read-only tasks and reserve the frontier default for hard tasks. "
            "The exact current lineup and per-model effort levels are in "
            "~/.codex/models_cache.json (or `codex debug models`), since names/tiers change "
            "over time and shouldn't be hardcoded here."
        ),
    }
    codex_props.update(
        {
            "sandbox": {
                "type": "string",
                "enum": ["read-only", "workspace-write", "danger-full-access"],
                "default": "workspace-write",
            },
            "approval_policy": {
                "type": "string",
                "enum": ["never", "on-request", "untrusted"],
                "default": "never",
            },
            "profile": {"type": "string", "description": "Optional Codex config profile."},
            "ephemeral": {
                "type": "boolean",
                "default": False,
                "description": (
                    "If true, run without persisting the session to disk - faster cleanup but "
                    "NON-RESUMABLE (continue_codex_agent and `codex resume` won't work). Defaults "
                    "to false so sessions are resumable/interjectable."
                ),
            },
            "skip_git_repo_check": {"type": "boolean", "default": True},
            "commit_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional. Explicit repo-relative paths to stage and commit ON THE HOST "
                    "after the agent finishes successfully. Codex's workspace-write sandbox "
                    "blocks writes to .git, so the agent cannot commit its own edits; the bridge "
                    "stages exactly these paths (never `git add -A`) and commits them. The commit "
                    "result (hash, staged files) is returned under 'commit' in agent_result/status."
                ),
            },
            "commit_message": {
                "type": "string",
                "description": "Commit message used when commit_paths is set (include any trailer). Defaults to 'agent commit'.",
            },
        }
    )
    codex_schema = {
        **prompt_schema,
        "properties": codex_props,
    }

    claude_props = dict(prompt_schema["properties"])
    claude_props.update(
        {
            "permission_mode": {
                "type": "string",
                "enum": ["acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"],
                "default": "auto",
            },
            "output_format": {
                "type": "string",
                "enum": ["text", "json", "stream-json"],
                "default": "text",
            },
            "no_session_persistence": {
                "type": "boolean",
                "default": False,
                "description": (
                    "If true, don't persist the session - NON-RESUMABLE (`claude --resume` won't "
                    "work). Defaults to false so the session can be resumed."
                ),
            },
            "session_id": {
                "type": "string",
                "description": (
                    "Optional explicit session id (uuid) to run under, so you control the "
                    "`claude --resume <id>` handle. Auto-generated when omitted (unless "
                    "no_session_persistence is true)."
                ),
            },
            "allowed_tools": {"type": "array", "items": {"type": "string"}},
            "disallowed_tools": {"type": "array", "items": {"type": "string"}},
        }
    )
    claude_schema = {
        **prompt_schema,
        "properties": claude_props,
    }

    opencode_props = dict(prompt_schema["properties"])
    opencode_props["model"] = {
        "type": "string",
        "description": (
            "Optional model override in provider/model format (e.g. meta/muse-spark-1.1, "
            "deepseek/deepseek-v4-flash for the direct paid DeepSeek Flash API, "
            "opencode/big-pickle, anthropic/claude-opus-4). Opencode supports many providers. "
            "Use a lighter/cheaper model for simple tasks."
        ),
    }
    opencode_props.update(
        {
            "variant": {
                "type": "string",
                "description": "Optional model variant (reasoning effort: high, low, minimal, etc).",
            },
            "agent": {
                "type": "string",
                "description": "Optional opencode agent name to use.",
            },
            "output_format": {
                "type": "string",
                "enum": ["json", "default"],
                "default": "json",
                "description": "Output format. json gives structured events + tokens; default is formatted text.",
            },
        }
    )
    opencode_schema = {
        **prompt_schema,
        "properties": opencode_props,
    }

    kimi_props = dict(prompt_schema["properties"])
    kimi_props["model"] = {
        "type": "string",
        "description": (
            "Optional model alias (e.g. kimi-code/k3, kimi-code/kimi-for-coding, kimi-code/kimi-k2.5). "
            "Defined in ~/.kimi-code/config.toml models table. Use cheaper model for bulk work."
        ),
    }
    kimi_props.update(
        {
            "output_format": {
                "type": "string",
                "enum": ["stream-json", "text"],
                "default": "stream-json",
                "description": "Output format. stream-json gives structured events; text is plain transcript.",
            },
            "yolo": {
                "type": "boolean",
                "default": True,
                "description": "Auto-approve (kimi -p runs in auto).",
            },
        }
    )
    kimi_schema = {
        **prompt_schema,
        "properties": kimi_props,
    }

    grok_props = dict(prompt_schema["properties"])
    grok_props["model"] = {
        "type": "string",
        "description": (
            "Optional model ID (e.g. grok-4.5, grok-4, grok-3). Available from `grok models`. "
            "Use default or cheaper variant for bulk work."
        ),
    }
    grok_props.update(
        {
            "output_format": {
                "type": "string",
                "enum": ["json", "plain", "streaming-json"],
                "default": "json",
                "description": "Output format for grok -p. json gives structured reply + tokens.",
            },
        }
    )
    grok_schema = {
        **prompt_schema,
        "properties": grok_props,
    }

    return [
        {
            "name": "run_codex_agent",
            "description": "Run Codex non-interactively via `codex exec` and wait for completion.",
            "inputSchema": codex_schema,
        },
        {
            "name": "launch_codex_agent",
            "description": "Launch Codex in the background via `codex exec`; poll with agent_status and agent_result.",
            "inputSchema": codex_schema,
        },
        {
            "name": "run_claude_agent",
            "description": "Run Claude Code non-interactively via `claude --print` and wait for completion.",
            "inputSchema": claude_schema,
        },
        {
            "name": "launch_claude_agent",
            "description": "Launch Claude Code in the background via `claude --print`; poll with agent_status and agent_result.",
            "inputSchema": claude_schema,
        },
        {
            "name": "run_opencode_agent",
            "description": "Run Opencode non-interactively via `opencode run` and wait for completion. Model param uses provider/model format (e.g. deepseek/deepseek-v4-flash for paid DeepSeek Flash, meta/muse-spark-1.1, opencode/big-pickle, anthropic/claude-sonnet-4).",
            "inputSchema": opencode_schema,
        },
        {
            "name": "launch_opencode_agent",
            "description": "Launch Opencode in the background via `opencode run`; poll with agent_status and agent_result. Supports provider/model selection, including deepseek/deepseek-v4-flash for paid DeepSeek Flash.",
            "inputSchema": opencode_schema,
        },
        {
            "name": "run_kimi_agent",
            "description": "Run Kimi Code non-interactively via `kimi -p` and wait. Model alias from config (e.g. kimi-code/k3, kimi-code/kimi-for-coding). Needs provider configured or login.",
            "inputSchema": kimi_schema,
        },
        {
            "name": "launch_kimi_agent",
            "description": "Launch Kimi Code in background via `kimi -p`; poll with agent_status and agent_result. Supports model selection.",
            "inputSchema": kimi_schema,
        },
        {
            "name": "run_grok_agent",
            "description": "Run Grok Code non-interactively via `grok -p` and wait. Model ID like grok-4.5. Grok must be logged in (`grok login`).",
            "inputSchema": grok_schema,
        },
        {
            "name": "launch_grok_agent",
            "description": "Launch Grok Code in background via `grok -p`; poll with agent_status and agent_result. Model selection via -m.",
            "inputSchema": grok_schema,
        },
        {
            "name": "agent_status",
            "description": "Show one background subagent job or all background subagent jobs.",
            "inputSchema": {
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "additionalProperties": False,
            },
        },
        {
            "name": "agent_result",
            "description": "Return stdout/stderr for a background subagent job.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "max_output_chars": {
                        "type": "integer",
                        "minimum": 1000,
                        "maximum": 2000000,
                        "default": DEFAULT_MAX_OUTPUT_CHARS,
                    },
                },
                "required": ["job_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cancel_agent",
            "description": "Terminate a running background subagent job.",
            "inputSchema": {
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "continue_codex_agent",
            "description": (
                "Interject into a previously launched codex job by resuming its session and "
                "sending a follow-up prompt (`codex exec resume <session_id>`). Returns the "
                "agent's reply plus token usage. Requires the job to be resumable (launched "
                "without ephemeral=true, which is now the default) and finished with its current "
                "turn (codex exec is single-turn). Devon can open the same session interactively "
                "with the `codex resume <id>` command shown in agent_status."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "The launched codex job to continue."},
                    "prompt": {"type": "string", "description": "Follow-up message to send into the session."},
                    "model": {"type": "string", "description": "Optional model override for this turn."},
                    "sandbox": {
                        "type": "string",
                        "enum": ["read-only", "workspace-write", "danger-full-access"],
                        "description": "Sandbox for this turn. Defaults to the original job's sandbox.",
                    },
                    "reasoning_effort": {
                        "type": "string",
                        "enum": ["none", "minimal", "low", "medium", "high", "xhigh"],
                        "default": "low",
                        "description": (
                            "Reasoning effort for this turn. Defaults to 'low' so the resume never "
                            "hits an unsupported-effort error inherited from config.toml."
                        ),
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 86400,
                        "default": 600,
                    },
                    "max_output_chars": {
                        "type": "integer",
                        "minimum": 1000,
                        "maximum": 2000000,
                        "default": DEFAULT_MAX_OUTPUT_CHARS,
                    },
                },
                "required": ["job_id", "prompt"],
                "additionalProperties": False,
            },
        },
        {
            "name": "continue_claude_agent",
            "description": (
                "Interject into a previously launched claude job by resuming its session and "
                "sending a follow-up prompt (`claude --print --resume <session_id>`). Returns the "
                "agent's reply. The claude counterpart of continue_codex_agent. Requires the job "
                "to be resumable (launched without no_session_persistence=true, which is now the "
                "default) and finished - resuming a live session races the running process over "
                "the same transcript. Devon can open the same session interactively with the "
                "`claude --resume <id>` command shown in agent_status."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "The launched claude job to continue."},
                    "prompt": {"type": "string", "description": "Follow-up message to send into the session."},
                    "model": {"type": "string", "description": "Optional model override for this turn."},
                    "permission_mode": {
                        "type": "string",
                        "enum": ["acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"],
                        "default": "auto",
                        "description": (
                            "Permission mode for this turn. 'bypassPermissions' runs fully "
                            "ungated (no approval for edits or commands) - use deliberately."
                        ),
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 86400,
                        "default": 600,
                    },
                    "max_output_chars": {
                        "type": "integer",
                        "minimum": 1000,
                        "maximum": 2000000,
                        "default": DEFAULT_MAX_OUTPUT_CHARS,
                    },
                },
                "required": ["job_id", "prompt"],
                "additionalProperties": False,
            },
        },
        {
            "name": "continue_opencode_agent",
            "description": (
                "Interject into a previously launched opencode job by resuming its session and "
                "sending a follow-up prompt (`opencode run --session <id>`). Returns the agent's "
                "reply plus token usage. Requires the job to be finished - resuming live would race. "
                "Model can be overridden per turn using provider/model format (e.g. meta/muse-spark-1.1)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "The launched opencode job to continue."},
                    "prompt": {"type": "string", "description": "Follow-up message to send into the session."},
                    "model": {"type": "string", "description": "Optional model override for this turn (provider/model)."},
                    "variant": {"type": "string", "description": "Optional model variant (reasoning effort)."},
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 86400,
                        "default": 600,
                    },
                    "max_output_chars": {
                        "type": "integer",
                        "minimum": 1000,
                        "maximum": 2000000,
                        "default": DEFAULT_MAX_OUTPUT_CHARS,
                    },
                },
                "required": ["job_id", "prompt"],
                "additionalProperties": False,
            },
        },
        {
            "name": "continue_kimi_agent",
            "description": (
                "Interject into a previously launched kimi job by resuming its session and "
                "sending a follow-up prompt (`kimi --session <id> -p <prompt>`). Returns the agent's "
                "reply plus token usage. Requires finished job."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "The launched kimi job to continue."},
                    "prompt": {"type": "string", "description": "Follow-up message to send into the session."},
                    "model": {"type": "string", "description": "Optional model alias override for this turn."},
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 86400,
                        "default": 600,
                    },
                    "max_output_chars": {
                        "type": "integer",
                        "minimum": 1000,
                        "maximum": 2000000,
                        "default": DEFAULT_MAX_OUTPUT_CHARS,
                    },
                },
                "required": ["job_id", "prompt"],
                "additionalProperties": False,
            },
        },
        {
            "name": "continue_grok_agent",
            "description": (
                "Interject into a previously launched grok job by resuming its session and "
                "sending a follow-up prompt (`grok --resume <id> -p <prompt>`). Returns the agent's "
                "reply plus token usage. Requires finished job."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "The launched grok job to continue."},
                    "prompt": {"type": "string", "description": "Follow-up message to send into the session."},
                    "model": {"type": "string", "description": "Optional model override for this turn."},
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 86400,
                        "default": 600,
                    },
                    "max_output_chars": {
                        "type": "integer",
                        "minimum": 1000,
                        "maximum": 2000000,
                        "default": DEFAULT_MAX_OUTPUT_CHARS,
                    },
                },
                "required": ["job_id", "prompt"],
                "additionalProperties": False,
            },
        },
        {
            "name": "peek_agent",
            "description": (
                "Watch a running subagent's progress WITHOUT waiting for it to finish. Reads the "
                "agent's own transcript (claude session JSONL / codex rollout JSONL), which both "
                "CLIs write incrementally, and returns normalized events - messages, tool calls, "
                "status. agent_result cannot do this: it blocks until the job exits. Pass the "
                "returned `cursor` back as `since` on the next call to get only what's new."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "The launched job to observe."},
                    "since": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                        "description": "Transcript line cursor from a previous peek. 0 reads from the start.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 500,
                        "default": 30,
                        "description": "Max events to return; the NEWEST are kept if there are more.",
                    },
                    "include_tool_calls": {
                        "type": "boolean",
                        "default": True,
                        "description": "Include tool calls/results and thinking, not just messages.",
                    },
                },
                "required": ["job_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "ask_parent",
            "description": (
                "SUBAGENT-ONLY. Ask the parent agent that launched you a question and BLOCK "
                "until it answers. Use when you hit a genuine fork in the road: an ambiguous "
                "requirement, a missing path or credential, an irreversible step you aren't sure "
                "is wanted, or a costly-to-undo design choice. Do NOT use it for anything you can "
                "settle by reading the repo. Only works for background-launched subagents "
                "(launch_*); a synchronous run_* agent's parent is blocked and cannot answer. "
                "On timeout, proceed on best judgement and state the assumption in your report."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "A specific, answerable question. Include the options you're choosing between.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional: what you've already established, so the parent can answer without re-deriving it.",
                    },
                    "on_timeout": {
                        "type": "string",
                        "enum": ["proceed", "abort"],
                        "default": "proceed",
                        "description": (
                            "What to do if nobody answers. 'proceed' (default) = advisory, guess "
                            "and flag the assumption. 'abort' = the answer is load-bearing; stop "
                            "that part of the task rather than guess. Use 'abort' when guessing "
                            "wrong would be destructive, irreversible, or expensive to undo."
                        ),
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 5,
                        "maximum": 86400,
                        "default": 600,
                        "description": "How long to wait before on_timeout applies.",
                    },
                },
                "required": ["question"],
                "additionalProperties": False,
            },
        },
        {
            "name": "pending_questions",
            "description": (
                "PARENT-SIDE. List questions from subagents that are blocked waiting on you. "
                "A blocked subagent makes no progress until answered - it just looks like a slow "
                "job - so check this whenever a launched job seems to be taking a long time. "
                "agent_status also surfaces pending questions for its job."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "Optional: only questions from this job."},
                    "include_answered": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include already-answered and timed-out questions.",
                    },
                    "escalated_only": {
                        "type": "boolean",
                        "default": False,
                        "description": "Only questions an intermediate agent could not answer.",
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "send_note",
            "description": (
                "PARENT-SIDE. Leave a course-correction for a RUNNING subagent, picked up at "
                "its next check. Use with peek_agent when you can see it heading somewhere "
                "wrong and would rather redirect it than let it finish and redo the work. "
                "This is NOT an interrupt: the subagent reads notes before irreversible "
                "actions and at phase boundaries, so delivery is not immediate and a "
                "short-lived job may finish without ever reading it. For a job that has "
                "already finished, use continue_claude_agent / continue_codex_agent instead."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "The running job to redirect."},
                    "note": {
                        "type": "string",
                        "description": (
                            "The correction. Say what to do differently and why - the subagent "
                            "is told this supersedes its plan where they conflict, so vague "
                            "notes produce worse results than none."
                        ),
                    },
                },
                "required": ["job_id", "note"],
                "additionalProperties": False,
            },
        },
        {
            "name": "check_notes",
            "description": (
                "SUBAGENT-ONLY. Read any notes your parent has left while watching you. "
                "Returns immediately; empty when there is nothing. Call it before any "
                "irreversible or destructive action and at phase boundaries - not in a loop "
                "and not between every small step."
            ),
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "raise_concern",
            "description": (
                "SUBAGENT-ONLY. Flag something wrong that is OUTSIDE your assigned task, "
                "without blocking. Use for a bug in code you were only reading past, a "
                "security or data-loss risk, a premise in your instructions you believe is "
                "mistaken, or output from your own helper you don't trust. Recording it does "
                "NOT pause you - keep working and mention it in your final report too. If you "
                "cannot safely proceed without a reply, that is a question: use ask_parent "
                "with on_timeout='abort' instead."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "concern": {
                        "type": "string",
                        "description": "What is wrong and why it matters. Be specific enough to act on.",
                    },
                    "evidence": {
                        "type": "string",
                        "description": "File, line, command output, or quote that supports it.",
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["info", "warning", "critical"],
                        "default": "warning",
                        "description": (
                            "'critical' means acting on this matters more than finishing your "
                            "task - reserve it for that."
                        ),
                    },
                },
                "required": ["concern"],
                "additionalProperties": False,
            },
        },
        {
            "name": "warm_agents",
            "description": (
                "List agents whose sessions are still resumable, with a per-agent verdict on "
                "whether reusing one beats launching a fresh one. CHECK THIS BEFORE launching "
                "an agent for work related to something already done: a warm agent has already "
                "read the files, learned the layout, and had its wrong assumptions corrected, "
                "and a new agent has to pay for all of that again in tokens and wall-clock to "
                "get back to where the last one already was. Survives a server restart - the "
                "sessions live on disk, not in this process. Reuse is bounded, though: entries "
                "come back marked 'reuse', 'stale' (idle too long, its picture of the repo may "
                "be wrong), 'crowded' (so much context that re-reading it costs more than "
                "starting over), 'suspect' (last run failed), or 'retired'. Resume with the "
                "continue_* tool named in each entry."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["codex", "claude", "opencode", "kimi", "grok"],
                        "description": "Optional: only agents of this client.",
                    },
                    "cwd": {
                        "type": "string",
                        "description": (
                            "Optional: only agents that worked in this directory. The strongest "
                            "signal that a warm agent's context is relevant to your task."
                        ),
                    },
                    "reusable_only": {
                        "type": "boolean",
                        "default": False,
                        "description": "Only agents whose verdict is 'reuse'.",
                    },
                    "include_retired": {"type": "boolean", "default": False},
                    "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 200},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "retire_agent",
            "description": (
                "Mark a warm agent as no longer worth reusing, so warm_agents stops "
                "recommending it. Use it when a session has gone stale against a changed "
                "repo, has accumulated more context than it is worth, or produced work you "
                "do not trust - leaving a bad agent at the top of the warm list makes its "
                "warmth read as an endorsement. The session itself is untouched and can "
                "still be resumed by hand. Pass unretire=true to undo."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "reason": {
                        "type": "string",
                        "description": (
                            "Why it is being retired. Worth stating - a future reader "
                            "deciding whether to resume it will want to know."
                        ),
                    },
                    "unretire": {"type": "boolean", "default": False},
                },
                "required": ["job_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "list_concerns",
            "description": (
                "PARENT-SIDE. Read concerns subagents raised unprompted. These are things they "
                "noticed outside their assigned task, so they will NOT appear in the job's "
                "result unless the agent also mentioned them. agent_status surfaces a job's "
                "concerns inline; use this to sweep across jobs or filter by severity."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "Optional: only this job's concerns."},
                    "min_severity": {
                        "type": "string",
                        "enum": ["info", "warning", "critical"],
                        "default": "info",
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "escalate_question",
            "description": (
                "PARENT-SIDE. Pass a subagent's question UP the chain when you cannot answer "
                "it either. Use this instead of inventing a plausible answer: a fabricated "
                "answer is worse than the subagent's own guess, because it carries your "
                "authority. Escalating does NOT answer the question and does NOT hand it back "
                "to you - whoever answers unblocks the original subagent directly, so never "
                "call answer_agent for a question you escalated. Check the outcome with "
                "pending_questions(include_answered=true); seeing no answer addressed to you "
                "is expected, not a failure. If you are the TOP-LEVEL agent facing an escalated "
                "question, put it to the human and relay their reply with answer_agent."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "question_id": {"type": "string", "description": "From pending_questions / agent_status."},
                    "note": {
                        "type": "string",
                        "description": "Why you can't answer, and anything you ruled out. Saves the next agent repeating it.",
                    },
                },
                "required": ["question_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "answer_agent",
            "description": (
                "PARENT-SIDE. Answer a subagent's pending question, unblocking it within ~2s. "
                "Get question_id from pending_questions or agent_status."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "question_id": {"type": "string", "description": "From pending_questions / agent_status."},
                    "answer": {
                        "type": "string",
                        "description": "The answer. Be decisive and specific - the subagent acts on this directly.",
                    },
                },
                "required": ["question_id", "answer"],
                "additionalProperties": False,
            },
        },
        {
            "name": "codex_status",
            "description": (
                "Get REAL ChatGPT-plan rate limit usage (5-hour and weekly percent left, reset times) "
                "by driving an interactive `codex` TUI session in a pty and running /status - the only "
                "place this data is exposed, since no CLI/API/app-server method returns it directly. "
                "This is UI screen-scraping, not a stable API: it can fail or break if the TUI changes. "
                "Takes ~10-20s and does not consume a model turn (just reads local session state)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "cwd": {
                        "type": "string",
                        "description": "Directory to run in. Defaults to the MCP server cwd.",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 5,
                        "maximum": 120,
                        "default": 30,
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "claude_usage",
            "description": (
                "Report local token usage by model from this machine's Claude Code session "
                "transcripts (~/.claude/projects/*/*.jsonl), including the currently running "
                "session. This is local token totals only, not Anthropic's rate-limit percentage - "
                "that isn't derivable locally (quota thresholds aren't stored on disk); run /usage "
                "in an interactive `claude` session for the actual 5-hour/weekly percentage."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "since_hours": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 24 * 365 * 5,
                        "default": 24 * 7,
                        "description": "Look back this many hours. 0 means all-time.",
                    }
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "codex_usage",
            "description": (
                "Report local token usage by model from Codex's own state DB "
                "(~/.codex/state_*.sqlite), across every Codex session on this machine "
                "(bridge-launched or interactive), grouped by model. This is local token "
                "totals only, not OpenAI's ChatGPT-plan rate limit percentage - that isn't "
                "queryable via CLI/API today; check `/status` in an interactive `codex` "
                "session or chatgpt.com/codex for the actual 5-hour/weekly plan usage."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "since_hours": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 24 * 365 * 5,
                        "default": 24 * 7,
                        "description": "Look back this many hours. 0 means all-time.",
                    }
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "route_status",
            "description": (
                "Fast, cache-based rate-limit snapshot for both Claude and Codex, meant for "
                "routine 'who has headroom right now' checks before delegating work - unlike "
                "codex_status (a ~15-30s live pty scrape), this just reads two files kept "
                "fresh independently of this MCP server: ~/.claude/statusline_cache.json "
                "(Claude's own rate_limits, refreshed every turn via the statusLine hook) and "
                "~/.claude/codex_status_cache.json (Codex's rate limits, refreshed every 5 "
                "minutes by the codexstatusrefresh launchd job). Works from any project/cwd. "
                "Falls back to a note explaining why data is missing (cache not yet populated, "
                "launchd job not installed, etc.) instead of erroring."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    ]


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    request_id = message.get("id")
    method = message.get("method")

    if request_id is None:
        return None

    try:
        if method == "initialize":
            params = message.get("params") or {}
            protocol_version = params.get("protocolVersion", "2024-11-05")
            return json_rpc_result(
                request_id,
                {
                    "protocolVersion": protocol_version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            )

        if method == "tools/list":
            return json_rpc_result(request_id, {"tools": tool_schema()})

        if method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if not isinstance(name, str) or name not in TOOL_HANDLERS:
                raise ValueError(f"unknown tool: {name}")
            if not isinstance(args, dict):
                raise ValueError("tool arguments must be an object")
            return json_rpc_result(request_id, TOOL_HANDLERS[name](args))

        return json_rpc_error(request_id, -32601, f"method not found: {method}")
    except subprocess.TimeoutExpired as exc:
        return json_rpc_result(
            request_id,
            tool_error(
                f"subagent timed out after {exc.timeout} seconds",
                {"stdout": exc.stdout or "", "stderr": exc.stderr or ""},
            ),
        )
    except Exception as exc:
        log(traceback.format_exc())
        return json_rpc_result(request_id, tool_error(str(exc)))


_stdout_lock = threading.Lock()


def _emit(response: dict[str, Any] | None) -> None:
    if response is None:
        return
    with _stdout_lock:
        sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def _dispatch_async(message: dict[str, Any]) -> None:
    """Run one request and emit its response. Used on worker threads for tools/call."""
    _emit(handle_request(message))


def main() -> int:
    log(f"starting; depth={current_depth()} max_depth={max_depth()}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            _emit(json_rpc_error(None, -32700, f"parse error: {exc}"))
            continue

        # tools/call handlers can run for many minutes (run_codex_agent, codex_status,
        # continue_codex_agent, ...). Dispatch them on worker threads so quick control
        # calls - agent_status, agent_result, cancel_agent - are never blocked behind a
        # long-running call in the single request loop. JSON-RPC responses carry their id,
        # so out-of-order emission is fine. The initialize/tools/list handshake and other
        # methods stay inline (they're fast and order-sensitive).
        if isinstance(message, dict) and message.get("method") == "tools/call":
            threading.Thread(target=_dispatch_async, args=(message,), daemon=True).start()
        else:
            _emit(handle_request(message))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
