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
SERVER_VERSION = "0.2.0"

DEFAULT_TIMEOUT_SECONDS = 900
DEFAULT_MAX_OUTPUT_CHARS = 30000
DEFAULT_MAX_DEPTH = 2

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
        "agent(s) of your own on a CHEAPER model than the one you are running, for the toil in "
        "your task - bulk mechanical edits, scanning long logs, reformatting, repetitive lookups. "
        "Name the model explicitly when you do; you can only delegate downward, never to an equal "
        "or better model. Keep the judgement, the design decisions, and the final report for "
        "yourself: you remain fully responsible for the work, including anything a helper got "
        "wrong, so check what comes back rather than passing it through unread. Delegating is a "
        "way to spend your attention where it matters, not a way to hand off accountability.\n"
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


def with_ask_parent_preamble(prompt: str, tool_name: str = ASK_PARENT_TOOL,
                             notes_tool: str = CHECK_NOTES_TOOL,
                             concern_tool: str = RAISE_CONCERN_TOOL,
                             sections: list[str] | None = None,
                             multi_phase: bool = True) -> str:
    """Advertise the parent channels. Background launches only, gated by section."""
    chosen = select_preamble_sections(sections, multi_phase)
    preamble = "".join(
        PREAMBLE_SECTIONS[name].format(
            tool=tool_name, notes_tool=notes_tool, max_helpers=max_helpers(),
            concern_tool=concern_tool)
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
    process: subprocess.Popen[str]
    stdout: str = ""
    stderr: str = ""
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
    lock: threading.Lock = field(default_factory=threading.Lock)

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


def resolve_cwd(args: dict[str, Any]) -> str:
    raw_cwd = optional_str(args, "cwd", os.getcwd())
    assert raw_cwd is not None
    cwd = str(Path(raw_cwd).expanduser().resolve())
    if not Path(cwd).is_dir():
        raise ValueError(f"`cwd` does not exist or is not a directory: {cwd}")
    return cwd


def child_env(kind: str, job_id: str | None = None, model: str | None = None) -> dict[str, str]:
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
# Delegation: a subagent may hand its own drudgery to a CHEAPER model.
#
# A depth-limited subagent would otherwise have to do all its own toil while
# everything above it delegates freely. It can now launch helpers of its own -
# but only at a strictly lower capability tier, and only a couple - so the
# affordance can't be used to route real work back up to a frontier model or to
# fan out without bound. The delegating agent stays accountable for the result.
# ---------------------------------------------------------------------------

# Most capable first. Claude's ladder is static (see the claude-api skill for the
# current lineup); codex's is read from its own cache, which already lists models
# in descending capability, so it doesn't rot when the lineup changes.
CLAUDE_MODEL_TIERS = [
    "claude-fable-5",
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
DEFAULT_MAX_HELPERS = 2

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


def model_rank(kind: str, model: str | None) -> int | None:
    """Position in the capability ladder; lower is more capable. None if unknown."""
    if not model:
        return None
    tiers = CLAUDE_MODEL_TIERS if kind == "claude" else codex_model_tiers()
    target = _normalize_model(model)
    for index, known in enumerate(tiers):
        if target == known or target.startswith(known):
            return index
    return None


def max_helpers() -> int:
    try:
        return max(0, int(os.environ.get("AGENT_BRIDGE_MAX_HELPERS", str(DEFAULT_MAX_HELPERS))))
    except ValueError:
        return DEFAULT_MAX_HELPERS


def enforce_delegation(kind: str, requested_model: str | None) -> None:
    """Gate a subagent launching its own helper. No-op for a top-level agent.

    Top-level launches are the human's call and stay unrestricted. A subagent
    (AGENT_BRIDGE_JOB_ID is set) may only delegate downward, and only a bounded
    number of times.
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
    own_rank = model_rank(os.environ.get("AGENT_BRIDGE_PARENT", kind), own_model)
    if not requested_model:
        raise ValueError(
            "as a subagent you must name the `model` you are delegating to, and it must be "
            "less capable than your own. Delegate drudgery (mechanical edits, bulk reads, "
            f"formatting, log scanning) to a cheaper model - for {kind} the cheaper tiers are "
            f"{', '.join((CLAUDE_MODEL_TIERS if kind == 'claude' else codex_model_tiers())[-2:])}."
        )
    requested_rank = model_rank(kind, requested_model)
    if requested_rank is None:
        raise ValueError(
            f"unknown model '{requested_model}' - cannot confirm it is a lower tier than "
            "yours. Name a model from the known ladder: "
            f"{', '.join(CLAUDE_MODEL_TIERS if kind == 'claude' else codex_model_tiers())}"
        )
    # Unknown own_rank means we can't prove the delegation goes downward. Allow it, since
    # the count cap still bounds the blast radius, but say so in the log.
    if own_rank is None:
        log(f"delegation: own model {own_model!r} not on the ladder; tier check skipped")
    elif requested_rank <= own_rank:
        raise RuntimeError(
            f"you may only delegate DOWNWARD. You are running {own_model}; "
            f"'{requested_model}' is equal or more capable, so this would escalate rather "
            "than offload. Pick a cheaper model for the drudgery, and keep the judgement "
            "calls yourself - you remain responsible for the result either way."
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


def build_codex_command(
    args: dict[str, Any], background: bool = False, job_id: str | None = None
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
    add_dirs = optional_string_list(args, "add_dirs")
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


def build_claude_command(args: dict[str, Any], background: bool = False) -> tuple[list[str], str, str, int, dict[str, Any]]:
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
    add_dirs = optional_string_list(args, "add_dirs")
    allowed_tools = optional_string_list(args, "allowed_tools")
    disallowed_tools = optional_string_list(args, "disallowed_tools")
    # An allowlist is exhaustive: if the caller passed one and forgot ask_parent, the
    # subagent is told it can ask (via the preamble) and then blocked from doing so.
    # Add it back rather than let that contradiction ship.
    if background and allowed_tools:
        for required in (ASK_PARENT_TOOL, CHECK_NOTES_TOOL, RAISE_CONCERN_TOOL):
            if required not in allowed_tools:
                allowed_tools = [*allowed_tools, required]
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


def run_command(kind: str, command: list[str], prompt: str | None, cwd: str, timeout_seconds: int, max_output_chars: int) -> dict[str, Any]:
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


def launch_command(kind: str, command: list[str], prompt: str | None, cwd: str, timeout_seconds: int, commit_paths: list[str] | None = None, commit_message: str | None = None, meta: dict[str, Any] | None = None, job_id: str | None = None) -> dict[str, Any]:
    enforce_depth()
    enforce_delegation(kind, (meta or {}).get("model"))
    # The job id reaches the child so ask_parent can address questions back at this job.
    # Callers that must bake it into the command itself (launch_codex, via -c env
    # overrides) generate it first and pass it in; otherwise make one here.
    job_id = job_id or str(uuid.uuid4())
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=child_env(kind, job_id=job_id, model=(meta or {}).get("model")),
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
    )

    with jobs_lock:
        jobs[job.id] = job

    thread = threading.Thread(target=collect_job, args=(job, prompt), daemon=True)
    thread.start()

    return {
        "job_id": job.id,
        "kind": kind,
        "pid": process.pid,
        "status": job.status,
        "cwd": cwd,
        "timeout_seconds": timeout_seconds,
        "session_id": job.session_id,
        "command": command_preview(command),
    }


def collect_job(job: Job, prompt: str | None) -> None:
    try:
        stdout, stderr = job.process.communicate(input=prompt, timeout=job.timeout_seconds)
        with job.lock:
            job.stdout = stdout or ""
            job.stderr = stderr or ""
            job.returncode = job.process.returncode
            job.finished_at = time.time()
    except subprocess.TimeoutExpired:
        job.process.terminate()
        try:
            stdout, stderr = job.process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.kill(job.process.pid, signal.SIGKILL)
            else:
                job.process.kill()
            stdout, stderr = job.process.communicate()
        with job.lock:
            job.stdout = stdout or ""
            job.stderr = stderr or ""
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

    # Optional post-agent commit (Codex jobs given commit_paths). Runs on the host,
    # outside the agent sandbox, so it lands even though workspace-write blocks .git.
    # Only on a clean success; explicit paths only, never `git add -A`.
    if job.commit_paths and job.returncode == 0:
        result = git_commit_paths(job.cwd, job.commit_paths, job.commit_message or "agent commit")
        with job.lock:
            job.commit = result


def get_job(job_id: str) -> Job:
    with jobs_lock:
        job = jobs.get(job_id)
    if job is None:
        raise ValueError(f"unknown job_id: {job_id}")
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
    carries per-turn usage. Safe to call repeatedly; cheap and best-effort.
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
        summary["pending_questions"] = [
            {
                "question_id": q["question_id"],
                "question": q["question"],
                "context": q.get("context") or None,
                "waiting_seconds": round(time.time() - q["asked_at"], 1) if q.get("asked_at") else None,
            }
            for q in blocked
        ]
        summary["action_required"] = (
            f"{len(blocked)} subagent question(s) awaiting an answer - this job is BLOCKED "
            "until you call answer_agent(question_id=..., answer=...)."
        )
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
    elif kind == "codex":
        summary["session_note"] = (
            "session id not captured yet (available once the job finishes; codex exec is "
            "single-turn, so resume/continue after it completes)"
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
    command, prompt, cwd, timeout_seconds, meta = build_codex_command(args, background=True, job_id=job_id)
    commit_paths = optional_string_list(args, "commit_paths")
    commit_message = optional_str(args, "commit_message")
    return tool_response(launch_command("codex", command, prompt, cwd, timeout_seconds, commit_paths=commit_paths, commit_message=commit_message, meta=meta, job_id=job_id))


def run_claude(args: dict[str, Any]) -> dict[str, Any]:
    command, prompt, cwd, timeout_seconds, meta = build_claude_command(args)
    enforce_delegation("claude", meta.get("model"))
    max_output_chars = optional_int(args, "max_output_chars", DEFAULT_MAX_OUTPUT_CHARS, 1000, 2_000_000)
    result = run_command("claude", command, None if command[-1] != "-" else prompt, cwd, timeout_seconds, max_output_chars)
    enrich_sync_result("claude", result, meta)
    return tool_response(result)


def launch_claude(args: dict[str, Any]) -> dict[str, Any]:
    command, prompt, cwd, timeout_seconds, meta = build_claude_command(args, background=True)
    return tool_response(launch_command("claude", command, None if command[-1] != "-" else prompt, cwd, timeout_seconds, meta=meta))


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

    with jobs_lock:
        all_jobs = list(jobs.values())
    for job in all_jobs:
        _maybe_enrich(job)
    summaries = [summarize_job(job) for job in all_jobs]
    return tool_response(
        {
            "jobs": summaries,
            "cumulative_tokens": _cumulative_tokens(summaries),
        }
    )


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
    "agent_status": agent_status,
    "agent_result": agent_result,
    "cancel_agent": cancel_agent,
    "continue_codex_agent": continue_codex_agent,
    "continue_claude_agent": continue_claude_agent,
    "peek_agent": peek_agent,
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
