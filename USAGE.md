# Using agent-bridge

Recipes for driving subagents. See `README.md` for the tool list and registration,
`DEVELOPMENT.md` for how it works inside.

## Pick the right launcher

`run_*` blocks until the agent finishes and hands back its output. `launch_*` returns a
`job_id` immediately.

Use `run_*` for short, self-contained work where you have nothing else to do. Use
`launch_*` for anything else — it is the only mode where you can watch the agent, and
the only mode where the agent can ask you questions. A `run_*` parent is blocked inside
its own call and could never answer, so `ask_parent` is disabled there by design.

## Watch an agent work

```
launch_claude_agent(prompt=..., cwd=...)   -> job_id
peek_agent(job_id)                         -> events + cursor
peek_agent(job_id, since=<cursor>)         -> only what is new
```

`agent_result` blocks until the process exits, so it tells you nothing mid-flight.
`peek_agent` reads the agent's own transcript instead and works while it runs.

Pass `include_tool_calls=false` for just the prose. The `limit` keeps the **newest**
events when there are more, so a long-running agent shows you what it is doing now
rather than what it started with.

Peek also works *after* a job finishes — useful for understanding how an agent reached
a wrong answer, which raw stdout usually will not tell you.

## Let an agent ask you questions

Background-launched agents are told the channel exists and are expected to use it.

```
pending_questions()                              -> blocked agents + question_ids
answer_agent(question_id=..., answer=...)        -> unblocks within ~2s
```

`agent_status(job_id)` also surfaces `pending_questions` and an `action_required` note.
Check one of these whenever a launched job seems slow — **a blocked agent is
indistinguishable from a slow one** until you look. It will sit there until the timeout
(default 600s), then proceed on its own judgement and flag the assumption in its report.

Answer decisively. The agent acts on your reply directly, so "maybe try X" produces
worse results than "do X, not Y".

Good tasks to launch this way are ones where a wrong assumption is expensive:
destructive operations, ambiguous scope, anything touching credentials or production.
The agent decides whether to ask; a task with no real ambiguity will just proceed.

## Continue a finished agent

Both kinds keep their sessions, so a finished job can be picked back up:

```
continue_codex_agent(job_id=..., prompt=...)     # programmatic follow-up turn
continue_claude_agent(job_id=..., prompt=...)
```

Neither works while the job is still running — for codex there is no session id yet,
and for claude a second process would race the running one over the same transcript.

To take over **yourself**, `agent_status` returns a `resume_command`:

```
claude --resume <session_id>
codex resume <session_id>
```

Paste it into a terminal and you are in that session interactively, full history intact.
That is the whole point of persisting sessions by default — pass `ephemeral=true`
(codex) or `no_session_persistence=true` (claude) only when you are sure you will never
want to look back, since it makes the job non-resumable.

## Permissions

Claude agents default to `permission_mode: "auto"`. Pass `"bypassPermissions"` for a
fully ungated run — no approval for edits or commands. It is per-call and never sticky,
so check the returned `command` for `--permission-mode bypassPermissions` if you need
to be sure it applied.

Codex agents run `--ask-for-approval never` with `--sandbox workspace-write`. That
sandbox blocks writes to `.git`, so a codex agent cannot commit its own work; use
`commit_paths` and the bridge commits those exact paths on the host after the job
succeeds. It never runs `git add -A`.

## Recursion

`AGENT_BRIDGE_MAX_DEPTH` (default 2) caps how deep agents can launch agents. Raise it
only deliberately — each level multiplies token spend, and a runaway chain is tedious
to kill by hand.
