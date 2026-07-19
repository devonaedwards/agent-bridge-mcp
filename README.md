# agent-bridge-mcp

Local stdio MCP server that lets Claude Code launch Codex subagents and lets
Codex launch Claude Code subagents.

Subagents can be watched while they work (`peek_agent`), can ask the launching
agent questions and block on the answer (`ask_parent`), and leave resumable
sessions you can pick up yourself in a terminal.

- [USAGE.md](USAGE.md) - recipes for driving subagents
- [DEVELOPMENT.md](DEVELOPMENT.md) - architecture, testing, and the gotchas
- `python3 tests/test_protocol.py` - fast checks, no tokens spent

## Tools

- `run_codex_agent` - run `codex exec` and wait for completion.
- `launch_codex_agent` - start `codex exec` in the background.
- `run_claude_agent` - run `claude --print` and wait for completion.
- `launch_claude_agent` - start `claude --print` in the background.
- `agent_status` - list jobs or inspect one job.
- `agent_result` - collect background job stdout/stderr.
- `cancel_agent` - terminate a background job.
- `continue_codex_agent` - interject a follow-up turn into a finished codex job.
- `continue_claude_agent` - same for a finished claude job.
- `peek_agent` - watch a running subagent's progress without waiting for it.
- `ask_parent` - **subagent-side**: ask the launching parent a question and block.
- `pending_questions` / `answer_agent` - **parent-side**: see and answer those questions.

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
  `ask_parent` is force-added to `allowed_tools` if the caller passed an allowlist
  without it.
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

Restart each client after registration. Both directions work: claude can launch
codex subagents and codex can launch claude subagents, and either can be the
parent answering the other's questions.

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
- `AGENT_BRIDGE_MAX_DEPTH=2` prevents unbounded Claude-to-Codex-to-Claude loops.
  Set a larger value in the MCP server environment only when deeper delegation is
  intentional.
- Override binary paths with `CODEX_BIN` or `CLAUDE_BIN` if needed.

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
