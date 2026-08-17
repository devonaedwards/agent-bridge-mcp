# agent-bridge-mcp

Local stdio MCP server (no third-party dependencies) that lets one coding
agent launch and supervise others: Claude Code, Codex, Grok CLI, Kimi Code,
and opencode (any provider/model opencode supports, notably DeepSeek Flash).
Version 0.5.0. The authoritative documentation is the source
(`agent_bridge_mcp.py`, ~6.2k lines, heavily commented); this README is the
map, not the territory.

## House policy (Devon, 2026-08-16)

Delegation runs on **opencode DeepSeek Flash lanes**
(`launch_opencode_agent` with `model: "deepseek/deepseek-v4-flash"`), NOT
Claude/Fable subagents: Fable subagents draw from the same Claude weekly
usage pool as the orchestrator (three lanes died mid-work when the limit hit
on 2026-08-16). Codex Sol/Terra when their quota is live; Grok (`grok-4.6`)
for diagnosis. Claude stays the orchestrator.

## Tools (33)

Launch/run/continue, one family per backend — `run_*` waits, `launch_*`
backgrounds, `continue_*` resumes a FINISHED session with a follow-up turn:

- `run_codex_agent` / `launch_codex_agent` / `continue_codex_agent`
- `run_claude_agent` / `launch_claude_agent` / `continue_claude_agent`
- `run_grok_agent` / `launch_grok_agent` / `continue_grok_agent`
- `run_kimi_agent` / `launch_kimi_agent` / `continue_kimi_agent`
- `run_opencode_agent` / `launch_opencode_agent` / `continue_opencode_agent`

Supervision:

- `agent_status` — one job or all; includes `files_changed` (the real
  progress signal), token usage, and stderr-derived `warnings[]`
  (sandbox/permission rejections surface here even when exit code is 0).
- `peek_agent` — stream a RUNNING agent's transcript incrementally
  (`agent_result` blocks until exit; peek does not).
- `agent_result` — final stdout/stderr; `cancel_agent`; `retire_agent`.
- `warm_agents` — reusable finished sessions worth continuing before
  spending tokens on a fresh helper.
- `codex_status`, `codex_usage`, `claude_usage`, `route_status` — health and
  quota.

Parent/child channel (always permitted for children, never gated by an
allowlist): `ask_parent` (blocking question), `check_notes` / `send_note`
(mid-flight notes), `raise_concern` / `list_concerns`, `answer_agent`,
`pending_questions`, `escalate_question` (hop a question up one level).

## Operational facts that bite

- **Opencode sandbox**: paths outside the job's cwd are auto-rejected;
  grants go through `OPENCODE_CONFIG_CONTENT` (`permission.external_directory`),
  which the server injects per-job from `add_dirs`. Without it, even
  STATE_DIR access dies silently — which kills `ask_parent`/`check_notes`
  while the job still reports success. Stage cross-repo specs in-repo, or
  pass `add_dirs`.
- **Spaced paths** (fixed 2026-08-17): opencode derives permission subjects
  from shell tokens, so `Astro\ Backups` (escaped) never matched the
  unescaped grant glob — and a spaced cwd could make a job's own repo look
  external. The server now grants BOTH spellings of every directory and
  self-grants a spaced cwd. Continued turns also used to get NO grants at
  all (report channel dead on turn two); `continue_opencode_agent` now
  re-grants STATE_DIR + cwd. Takes effect on server restart.
- **Tool naming differs per client**: Claude/Kimi see
  `mcp__agent-bridge__ask_parent`; codex normalizes to
  `mcp__agent_bridge__ask_parent` (register it under `agent_bridge` there);
  opencode and grok see bare names (`ask_parent`).
- **Codex reads AGENTS.md, not CLAUDE.md** — every codex prompt gets a
  preamble redirecting it to the repo's CLAUDE.md.
- **Preambles are assembled per-launch** from sections (core / abort /
  escalate / delegate / notes / concerns / standing), structurally gated so
  a child is never advertised a door that's locked (e.g. at max depth it is
  not told how to delegate). `multi_phase: false` drops the notes channel
  for one-shots.
- **Stream buffers cap at 200k chars** (head 40k + tail preserved, middle
  dropped) so error tails survive chatty agents. When a result is still too
  big, opencode transcripts live in `~/.local/share/opencode/opencode.db`
  (sqlite, `part` table).
- **DeepSeek Flash craft**: it ends turns early — force an explicit call
  structure (write handoff / merge+verify / report) for multi-step work.
  Model tiers: `deepseek/deepseek-v4-flash` (paid direct API),
  `opencode/deepseek-v4-flash-free` (free tier fallback).
- `AGENT_BRIDGE_MAX_DEPTH=2` bounds recursive delegation. Binary overrides:
  `CODEX_BIN`, `CLAUDE_BIN`, `OPENCODE_BIN`, etc.

## Registration

```bash
# Claude Code
claude mcp add -s user agent-bridge -- python3 /Users/devonedwards/src/agent-bridge-mcp/agent_bridge_mcp.py
# Codex (underscore name, see tool-naming note above)
codex mcp add agent_bridge -- python3 /Users/devonedwards/src/agent-bridge-mcp/agent_bridge_mcp.py
```

Restart each client after registration. The copy in the astro workspace
(`_agent-bridge-mcp/`) is a point-in-time mirror; this repo is the source of
truth.
