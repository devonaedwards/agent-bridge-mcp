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

## Redirect an agent mid-flight

`peek_agent` shows you an agent heading somewhere wrong; `send_note` redirects it
without losing the work already done.

```
peek_agent(job_id)                        -> see it going wrong
send_note(job_id, note="do X instead")    -> queued
```

**Delivery is not immediate and not guaranteed.** A launched agent is a one-shot
process with no stdin, so there is no way to interrupt it. The note sits in a mailbox
the agent reads at the points where a correction is still worth having: before an
irreversible action, and at phase boundaries. If it is mid-step you wait for that step
to end; if the job is short it may finish having never checked.

That means notes work well for multi-phase work and badly for anything quick. For a
job that has already finished, use `continue_*` instead — `send_note` refuses, since
a finished agent will never read it.

Write the note as a correction with a reason. The subagent is told it supersedes its
current plan where they conflict, so a vague note produces worse results than none.

Observed working: an agent given a 4-phase task checked at the phase-1 boundary, took
a note redirecting phases 2-4, applied it to exactly those, and left phase 1 alone. It
also noted in its report that it had *not* checked between phases 2 and 3 — so a note
landing in that window would have arrived one phase late. That gap is inherent.

## Concerns: see something, say something

A subagent that notices something wrong *outside* its assigned task - a bug in code it
was only reading past, a security risk, a mistaken premise, a helper's output it doesn't
trust - can flag it with `raise_concern`. It does not block; the agent keeps working.

```
list_concerns(min_severity="critical")   -> what subagents flagged unprompted
```

`agent_status` also surfaces a job's concerns inline, with a `critical_concerns` warning.
Check them before accepting a job's output: by definition these are things nobody asked
about, so they will not appear in the result unless the agent also volunteered them.

Observed working: an agent given a purely mechanical task (count the lines in a file)
did exactly that, and also flagged a `shutil.rmtree("/")` buried in the file as critical,
with the file and line - having only read the file in order to count it.

The distinction from `ask_parent` matters: a question blocks because the agent needs an
answer to continue. A concern does not. An agent that cannot safely proceed should ask,
with `on_timeout="abort"`.

## Delegating drudgery to a cheaper model

A subagent can hand its own toil to a cheaper model - bulk mechanical edits, log
scanning, reformatting - by launching helpers of its own:

- It may only delegate **downward**: strictly lower on the capability ladder than the
  model it is running. Upward or sideways is refused, so the affordance can't route real
  work back to a frontier model.
- It must **name the model** explicitly; an unnamed or unknown model is refused.
- At most **2 helpers** per subagent (`AGENT_BRIDGE_MAX_HELPERS` to change).
- Top-level launches (yours) are unrestricted - the limits apply only to subagents.

The delegating agent stays responsible for the result, including anything a helper got
wrong, and is told so.

Ladders, most capable first. Codex's is read from `~/.codex/models_cache.json`, which
already lists models in descending capability, so it tracks the lineup automatically:

```
claude: fable-5 > opus-4-8 > opus-4-7 > opus-4-6 > sonnet-5 > sonnet-4-6 > haiku-4-5
codex:  gpt-5.6-sol > gpt-5.6-terra > gpt-5.6-luna > gpt-5.5 > gpt-5.4 > gpt-5.4-mini
```

## Trimming the subagent briefing

Every background launch prepends a briefing covering the question channel, aborting,
escalation, notes, delegation, concerns, and the agent's standing to ask for context.
Each section earned its place from an observed failure, but sending all of them every
time dilutes the ones that matter for the task in hand.

**Structural gating is automatic and not overridable.** A subagent at the recursion
ceiling cannot launch anything, so the `escalate` and `delegate` sections are dropped -
telling it otherwise advertises a door that is locked. Same when `AGENT_BRIDGE_MAX_HELPERS`
is 0. A caller cannot re-enable these; the gate wins over an explicit section list.

**Caller controls:**

```
launch_claude_agent(prompt=..., multi_phase=False)          # drops the notes section
launch_claude_agent(prompt=..., preamble_sections=[...])    # advanced: exact set
```

`multi_phase=False` is the one worth reaching for: a one-shot job finishes before it
would ever hit a phase boundary, so the note channel is pure overhead. Sections are
`core`, `abort`, `escalate`, `delegate`, `notes`, `concerns`, `standing`; omit
`preamble_sections` for automatic selection, which is right almost always.

Observed: a trimmed briefing (`core` + `concerns` + `standing`, single-phase — 44%
smaller) still produced the behavior it kept. The agent given a line-counting task
flagged a `shutil.rmtree("/")` in the file as critical, exactly as under the full
briefing. Trimming the sections you don't need does not appear to cost the ones you do —
but that is one observation, not a guarantee.

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
