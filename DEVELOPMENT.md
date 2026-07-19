# Development

Single file, no third-party dependencies, stdlib only. That constraint is deliberate —
the server gets spawned by two different CLIs in environments with unpredictable PATHs
and no guaranteed venv, so it must run under whatever `python3` it lands on.

## Testing

```bash
python3 tests/test_protocol.py        # fast, no agents spawned, no tokens spent
python3 tests/test_live.py claude     # ~1 min, spends tokens
python3 tests/test_live.py codex      # ~3 min, spends tokens
```

Run `test_protocol.py` before every commit. Run the live tests after touching anything
in the launch path, the preamble, or the codex `-c` overrides.

**The live tests speak JSON-RPC to a freshly spawned server rather than going through
an MCP client.** This is not incidental. A client keeps its server process alive across
edits, so testing through your editor's client silently exercises whatever code was
loaded at connect time. Every "why didn't my fix work" moment in this repo's history was
that. If you must test through a client, reconnect it first (`/mcp` in Claude Code).

## Architecture

Three layers:

1. **JSON-RPC loop** (`main`, `handle_request`) — `tools/call` dispatches on worker
   threads so a long `run_codex_agent` cannot block a quick `agent_status`. Responses
   carry their id, so out-of-order emission is fine.
2. **Job table** (`jobs`, `Job`) — in-memory, per server process. `launch_*` spawns a
   process and a `collect_job` thread that blocks in `communicate()` until exit.
3. **On-disk state** — the only thing shared between server processes.

### Why on-disk state exists

A subagent gets its **own** agent-bridge server process. The parent's `jobs` dict is
invisible to it. So anything crossing that boundary goes through the filesystem:

- `~/.agent-bridge/questions/<id>.json` — one file per question, written atomically via
  a temp file plus `os.replace`. Never read-modify-write the whole file in place;
  concurrent parent answers and child polls will interleave and lose data.
- `AGENT_BRIDGE_JOB_ID` in the child env — how a question is addressed back to its job.
  Correlation is structural rather than asking the model to echo a UUID back.

### Why peek reads transcripts

`collect_job` holds the pipes in `communicate()` until the process exits, so job stdout
simply does not exist mid-flight. But both CLIs write their own transcript
incrementally, so that is what `peek_agent` reads:

- claude → `~/.claude/projects/<slug>/<session_id>.jsonl`
- codex → `~/.codex/sessions/YYYY/MM/DD/rollout-*-<session_id>.jsonl`

Claude session ids are pre-assigned at launch, so the path is known immediately. Codex
does not reveal its id until exit, so `_find_codex_rollout_live` matches on
`session_meta.cwd` against the job's cwd, newest first.

## Gotchas that cost real debugging time

**Transcript entry shapes are not uniform.** `session_meta` is a *top-level* entry type
whose payload has no `type` key; `event_msg` and `response_item` entries carry their
type inside `payload`. Checking `payload["type"]` for `session_meta` produces a branch
that is silently dead. Codex assistant prose is `payload.message`, not `payload.text`.
`function_call_output.output` is a *list* of blocks while `custom_tool_call_output.output`
is a plain string.

**Codex `-c` override syntax**, all verified against codex-cli 0.144.6:

| Rule | Failure mode if broken |
|---|---|
| Table is `mcp_servers`, not `mcpServers` | **Silent.** No error, server never appears |
| Hyphenated names must be bare in a `-c` path | Quotes get embedded in the server name |
| Values are TOML, not JSON | `expected struct RawMcpServerConfig` |
| `default_tools_approval_mode="approve"` | Calls denied as `"user cancelled"` in 0.0s |
| `tool_timeout_sec` > `ask_parent` timeout | Blocked call killed at 60s |
| `-c` key must MATCH the config.toml key | Duplicate server, hashed tool names |

That last one is subtle. `-c` merges into an entry with the same key but registers a
*second* server under a different one — and since codex normalizes `-` to `_`, both
collapse to `agent_bridge` and it disambiguates with hash suffixes
(`mcp__agent_bridge_529cc70a97db__launch_claude_agent`). Tool names then vary per
launch and subagents have to grep `ALL_TOOLS` for every call. So the injection uses the
hyphenated `agent-bridge` key to match `config.toml`, and lets codex's own
normalization produce the `mcp__agent_bridge__*` tool names.

`"auto"` looks like the right approval mode and is accepted, but it defers to the
approval policy — and we launch codex with `--ask-for-approval never`, so it still denies.
Only `"approve"` pre-approves. To discover valid enum values, feed codex a bogus one and
read the error: it enumerates them.

**Codex normalizes `-` to `_` in server names**, so the bridge registers there as
`agent_bridge` and its tools appear as `mcp__agent_bridge__*`. Advertising the
hyphenated name sends the subagent grepping `ALL_TOOLS` to find it.

**`stdin` must never be inherited.** A subagent that reads stdin will steal the server's
JSON-RPC pipe and hang every subsequent request. Always `subprocess.DEVNULL` unless
piping a prompt.

## Adding a tool

1. Write the handler `(args: dict) -> dict`, returning `tool_response(...)` or raising
   `ValueError` for bad input (the dispatcher converts exceptions to tool errors).
2. Register it in `TOOL_HANDLERS`.
3. Add its schema in `tool_schema()`. `test_protocol.py` asserts parity between the two
   and will fail if you add one without the other.

Write the description for a model that will decide *when* to call it, not just how. The
descriptions that work say what the tool is for and, where it matters, what it is not
for — `ask_parent` explicitly rules out questions the agent could answer by reading the
repo, and `peek_agent` says outright that `agent_result` cannot do this.

## Prior art

`ask_parent` follows [dvcrn/mcp-server-subagent](https://github.com/dvcrn/mcp-server-subagent),
which pioneered subagent→parent questions; it uses unsynchronized whole-file rewrites,
has no timeouts, and implements "blocking" by instructing the child to `sleep 30` in a
loop. This version blocks server-side with atomic writes and real deadlines.

`peek_agent` follows [mkXultra/ai-cli-mcp](https://github.com/mkXultra/ai-cli-mcp), whose
peek attaches a listener to the child's stdout for an N-second window — so it returns
nothing if the agent happens to be quiet, and can never show work from before the call.
Reading transcripts gives full history plus a real cursor.
