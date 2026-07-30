# agent-bridge-mcp

Local stdio MCP server that lets Claude Code, Codex, Opencode, and Kimi Code launch each
other as subagents - any direction.

Subagents can be watched while they work (`peek_agent`), can ask the launching
agent questions and block on the answer (`ask_parent`), and leave resumable
sessions you can pick up yourself in a terminal.

Now includes Opencode + Kimi Code support:
- `run_opencode_agent` / `launch_opencode_agent` / `continue_opencode_agent`
- `run_kimi_agent` / `launch_kimi_agent` / `continue_kimi_agent`
- Any client can call any other with specific model selection, e.g.:
  - Opencode: `model: "anthropic/claude-sonnet-4"`, `model: "opencode/north-mini-code-free"`, `model: "meta/muse-spark-1.1"`
  - Kimi: `model: "kimi-code/k3"`, `model: "kimi-code/kimi-for-coding"`
  - Claude: `model: "haiku"` / `sonnet` / `opus`
  - Codex: `model: "gpt-5.4-mini"` etc.

- [USAGE.md](USAGE.md) - recipes for driving subagents
- [DEVELOPMENT.md](DEVELOPMENT.md) - architecture, testing, and the gotchas
- `python3 tests/test_protocol.py` - fast checks, no tokens spent

## Tools

- `run_codex_agent` - run `codex exec` and wait for completion.
- `launch_codex_agent` - start `codex exec` in the background.
- `run_claude_agent` - run `claude --print` and wait for completion.
- `launch_claude_agent` - start `claude --print` in the background.
- `run_opencode_agent` - run `opencode run` and wait (supports `provider/model` like `meta/muse-spark-1.1`, `opencode/big-pickle`, `anthropic/claude-sonnet-4`).
- `launch_opencode_agent` - start `opencode run` in background.
- `run_kimi_agent` - run `kimi -p` and wait (model alias like `kimi-code/k3`, `kimi-code/kimi-for-coding`).
- `launch_kimi_agent` - start `kimi -p` in background.
- `agent_status` - list jobs or inspect one job.
- `agent_result` - collect background job stdout/stderr.
- `cancel_agent` - terminate a background job.
- `continue_codex_agent` - interject a follow-up turn into a finished codex job.
- `continue_claude_agent` - same for a finished claude job.
- `continue_opencode_agent` - same for a finished opencode job.
- `continue_kimi_agent` - same for a finished kimi job.
- `peek_agent` - watch a running subagent's progress without waiting for it (claude + codex via transcripts, opencode/kimi via stdout JSONL).
- `warm_agents` - list agents whose sessions are still resumable, with a verdict on whether reusing one beats launching fresh. Survives a server restart.
- `retire_agent` - stop recommending a warm agent that has gone stale or bad.
- `ask_parent` - **subagent-side**: ask the launching parent a question and block.
- `pending_questions` / `answer_agent` - **parent-side**: see and answer those questions.
- `escalate_question` - **parent-side**: pass a question up when you can't answer it either.
- `send_note` / `check_notes` - redirect a running subagent mid-flight.
- `raise_concern` / `list_concerns` - subagents flag problems outside their task, without blocking.

## Watching a subagent (`peek_agent`)

`agent_result` blocks until the job exits, so it is useless mid-flight. `peek_agent`
reads the agent's own transcript instead - `~/.claude/projects/<slug>/<session_id>.jsonl`
or `~/.codex/sessions/**/rollout-*.jsonl` - which both CLIs write incrementally. It
returns normalized events (messages, tool calls, results, status) plus a `cursor`;
pass that back as `since` to get only what is new.

Codex hides its session id until the job ends, so a running codex job's rollout is
located by matching `session_meta.cwd` against the job's cwd, newest first.

## Subagent questions (`ask_parent`)

A subagent runs in its own agent-bridge server process, so the parent's in-memory job
table is invisible to it. Questions go through `~/.agent-bridge/questions/<id>.json`,
written atomically (`os.replace`) and polled by the asker.

- Background-launched subagents get a preamble telling them the channel exists, and
  the channel is **always allowed** - `ask_parent`, `check_notes`, and `raise_concern`
  are force-added to `--allowedTools` on every background launch (not only when the
  caller passed an allowlist) and stripped from `--disallowedTools` if a caller put
  them there. A child that is sandboxed, blocked, or out of its depth has to be able
  to *say so*; if its only channel home is itself gated, the failure arrives as
  silence - the job burns its timeout and reports nothing, which is worse than a
  refusal because nobody learns why. Safe to always add because claude's
  `--allowedTools` is an additive auto-approve list, not an exhaustive whitelist, so
  it grants the child nothing else. Codex gets the same guarantee via
  `default_tools_approval_mode="approve"`, and grok via `--allow` rules.
- `ask_parent` blocks server-side until answered or `timeout_seconds` elapses; on
  timeout the subagent is told to proceed on its own judgement and flag the assumption.
- `agent_status` surfaces `pending_questions` and an `action_required` note, because a
  blocked subagent otherwise just looks like a slow one.
- Only works for `launch_*` (background) jobs. A `run_*` agent's parent is blocked
  inside the call and could never answer, so those jobs get no `AGENT_BRIDGE_JOB_ID`
  and `ask_parent` fails fast instead of hanging.

Codex subagents only see `ask_parent` if the bridge is registered in
`~/.codex/config.toml` (see Registration); the preamble is gated on that so codex is
never told about a tool it cannot call.

Prior art: `ask_parent` follows [dvcrn/mcp-server-subagent](https://github.com/dvcrn/mcp-server-subagent),
and `peek_agent` follows [mkXultra/ai-cli-mcp](https://github.com/mkXultra/ai-cli-mcp).
Both differ here: the question channel uses atomic writes and real server-side blocking
with timeouts, and peek is transcript+cursor based rather than a fixed live window.

## Registration

Claude Code:

```bash
claude mcp add -s user agent-bridge -- python3 /Users/devonedwards/src/agent-bridge-mcp/agent_bridge_mcp.py
```

Codex - required if you want to START a session in codex and launch from there.
Without it `codex mcp list` returns `[]` and codex has no bridge tools at all:

```bash
codex mcp add agent-bridge -- python3 /Users/devonedwards/src/agent-bridge-mcp/agent_bridge_mcp.py
```

Then add to the generated `[mcp_servers.agent-bridge]` block, since codex's 60s
default would kill a blocked `ask_parent`:

```toml
tool_timeout_sec = 3600
startup_timeout_sec = 30
```

Opencode - so Opencode can launch Claude/Codex/Opencode/Kimi/Grok subagents and pick models:

```bash
opencode mcp add agent-bridge -- python3 /Users/devonedwards/src/agent-bridge-mcp/agent_bridge_mcp.py
```

Verify:

```bash
opencode mcp list
# should show ✓ agent-bridge connected
```

Then you can ask Opencode:

> Use the agent-bridge MCP to launch a Claude subagent with model haiku to summarize this repo.

> Use launch_opencode_agent with model opencode/north-mini-code-free for cheap bulk work.

Opencode exposes tools directly as `launch_claude_agent`, `run_codex_agent`, `ask_parent`, etc. (no `mcp__` prefix).

Kimi Code - so Kimi can launch Claude/Codex/Opencode/Kimi/Grok subagents and pick models:

```bash
cat ~/.kimi-code/mcp.json
# create if missing:
mkdir -p ~/.kimi-code
cat > ~/.kimi-code/mcp.json <<'JSON'
{
  "mcpServers": {
    "agent-bridge": {
      "command": "python3",
      "args": ["/Users/devonedwards/src/agent-bridge-mcp/agent_bridge_mcp.py"],
      "toolTimeoutMs": 3600000,
      "startupTimeoutMs": 30000
    }
  }
}
JSON
```

Verify in TUI: `/mcp` should show `agent-bridge` connected. Or run `kimi` and type `/mcp-config` to check.

Kimi needs a provider configured (run `/login` or set `default_model` in `~/.kimi-code/config.toml`). Without it, `run_kimi_agent` will error "No model configured". Once configured, you can:

> Use the agent-bridge MCP to launch an Opencode subagent with model meta/muse-spark-1.1 to audit this repo.

Kimi exposes tools as `mcp__agent-bridge__launch_claude_agent`, etc.

Grok Code - so Grok can launch Claude/Codex/Opencode/Kimi/Grok subagents and pick models:

```bash
# Grok inherits from Claude's ~/.claude.json automatically, but also add to its own config for robustness:
grok mcp add agent-bridge -- python3 /Users/devonedwards/src/agent-bridge-mcp/agent_bridge_mcp.py
grok mcp list        # should show agent-bridge
grok mcp doctor      # should show healthy
```

Then ask Grok:

> Use the agent-bridge MCP to launch a Claude subagent with model haiku to review the diff.

> Use launch_grok_agent with model grok-4.5 for heavy work, or launch_opencode_agent with cheap model for bulk edits.

Grok exposes tools directly as `launch_claude_agent`, `run_grok_agent`, `ask_parent`, etc. (20+ tools) plus `mcp__` prefixed? Actually logs show direct names.

Grok must be logged in (`grok login`) and default model set (`grok-4.5`). Verify with `grok models`.

Restart each client after registration. All directions work: claude can launch
codex/opencode/kimi/grok subagents, codex can launch claude/opencode/kimi/grok, opencode can launch
claude/codex/opencode/kimi/grok, kimi can launch claude/codex/opencode/kimi/grok, grok can launch claude/codex/opencode/kimi/grok, and any parent can answer the other's questions.

### Codex subagents self-register

`launch_codex_agent` injects the bridge into each codex invocation with `-c` overrides,
so a codex subagent can call `ask_parent` even when `~/.codex/config.toml` has no
`mcp_servers` block. `-c` merges with any servers the user already has. Verified
against codex-cli 0.144.6; four things there are load-bearing:

- The table is `mcp_servers` (snake_case). **`mcpServers` is silently ignored** - no
  warning, no error, the server simply never appears.
- Hyphenated names must be **bare** in a `-c` dotted path. Quoting embeds the quote
  characters in the name rather than escaping it.
- Codex normalizes `-` to `_` in server names, so the bridge registers there as
  `agent_bridge` and its tools appear as `mcp__agent_bridge__<tool>`. The preamble is
  kind-aware so each subagent is told the name it will actually see.
- `default_tools_approval_mode="approve"` is required. We launch codex with
  `--ask-for-approval never`, under which an MCP call needing approval is denied
  outright (`"user cancelled"` in 0.0s) instead of prompting. `"auto"` defers to the
  approval policy and so still denies - only `"approve"` pre-approves the server.
- `tool_timeout_sec` is raised to 3600; codex's default of 60 would kill a blocked
  `ask_parent` long before a human-paced answer arrives.

## Defaults

- Codex runs with `--ask-for-approval never`, `--sandbox workspace-write`,
  `--ephemeral`, and `--skip-git-repo-check` unless overridden.
- Claude runs with `--print`, `--permission-mode auto`, and
  `--no-session-persistence` unless overridden.
- Opencode runs with `opencode run --format json --dir <cwd>` plus optional `-m provider/model`.
- Kimi runs with `kimi -p <prompt> --output-format stream-json` plus optional `-m <model>`. CWD via subprocess cwd.
- Grok runs with `grok -p <prompt> --output-format json -m <model> --cwd <cwd>`.
- `AGENT_BRIDGE_MAX_DEPTH=2` prevents unbounded loops (e.g. Claude->Codex->Claude).
  Set a larger value in the MCP server environment only when deeper delegation is
  intentional.
- Override binary paths with `CODEX_BIN`, `CLAUDE_BIN`, `OPENCODE_BIN`, `KIMI_BIN`, `GROK_BIN` if needed.
- Delegation direction is enforced for subagents (top-level launches are the human's
  call and stay unrestricted). See "Delegation" below.
- Warm-agent roster lives in `~/.agent-bridge/roster/`. Tunable with
  `AGENT_BRIDGE_ROSTER_STALE_HOURS` (default 24), `AGENT_BRIDGE_ROSTER_MAX_TURNS`
  (default 12), `AGENT_BRIDGE_ROSTER_KEEP` (default 200).

## Delegation: downward and sideways

A subagent may launch up to `AGENT_BRIDGE_MAX_HELPERS` (default 2) helpers of its own,
**downward** to a cheaper model or **sideways** to a peer at its own level. It may not
delegate *upward* - that is escalation dressed as offloading, and `escalate_question`
is the honest way to do it.

Sideways is the point of a bridge between frontier models: "Sol reviews what Opus
wrote" is a second opinion, not an escalation. Codex/Sol and Claude/Opus are declared
peers in `PEER_MODELS`, in both directions.

Direction is decided by cross-vendor **capability class**, not by ladder position.
Comparing raw ladder indices across vendors is meaningless - index 0 of the codex
ladder is not "as capable as" index 0 of opencode's - and doing it silently blocked
every cross-vendor peer handoff while waving through nonsense the other way.

| Class | Members (abridged) |
| --- | --- |
| `apex` | `claude-fable-5`, `claude-mythos-5` |
| `frontier` | `claude-opus-5`/`4-8`/`4-7`/`4-6`, `gpt-5.6-sol`, `gpt-5.5`, `meta/muse-spark-1.1`, `opencode/big-pickle`, `kimi-code/k3`, `grok-4.5` |
| `workhorse` | `claude-sonnet-5`/`4-6`, `gpt-5.6-terra`/`luna`, `gpt-5.4`, `kimi-for-coding`, `grok-4`/`3` |
| `light` | `claude-haiku-4-5`, `gpt-5.4-mini`, `grok-3-mini`, `*-free`, `*-highspeed` |

Unclassifiable models fall back to name patterns, then to allow-with-a-log (the helper
count still bounds the blast radius). A model unknown to both the ladder and the class
table is refused rather than waved through on a typo. A subagent launched without an
explicit model has no `AGENT_BRIDGE_MODEL`, so its direction cannot be proven and the
check is skipped - name the model if you want the gate to bite.

## Reusing warm agents (`warm_agents`)

An agent that has been working a problem has paid for its context: the files it read,
the layout it learned, the corrections it absorbed. Replacing it with a fresh agent
means paying for all of that again, in tokens and wall-clock, to arrive back where the
last one already was.

`continue_*_agent` could always resume a session, but the job table lived only in the
server's memory - so every restart orphaned every warm agent even though its session
was still on disk, fully resumable. The roster is now written to
`~/.agent-bridge/roster/<job_id>.json` when a job finishes, and `get_job` rehydrates
from it, so `continue_*_agent` works on an agent this process has never seen.

Reuse is bounded. Each entry carries a verdict rather than an invitation:

| Verdict | Meaning |
| --- | --- |
| `reuse` | Warm and recent - resuming beats re-teaching a new agent. |
| `stale` | Idle past `AGENT_BRIDGE_ROSTER_STALE_HOURS`; its picture of the repo may be wrong. Re-brief it. |
| `crowded` | Past `AGENT_BRIDGE_ROSTER_MAX_TURNS`; re-reading that context may now cost more than starting fresh. |
| `suspect` | Its last run failed - read `agent_result` before trusting what it thinks it knows. |
| `retired` | A human called `retire_agent` on it. |

Match on `cwd` and on the task line: a warm agent pointed at the *wrong* problem is
worse than a cold one, because its old framing follows it. Warm and relevant beats
fresh; warm and irrelevant does not.

## Example Prompt

Ask Claude:

```text
Use the agent-bridge MCP to launch a Codex subagent in this repo. Ask it to
inspect the test suite and report the most likely failing area without editing
files.
```

Ask Codex:

```text
Use the agent-bridge MCP to launch a Claude subagent in this repo. Ask it to
review the diff for behavioral risks and return only findings.
```
