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

## Delegating down, sideways, and hiring up

A subagent can launch agents of its own downward, sideways, or upward. All three are
bounded and all require it to **name the model**. An unnamed model is refused, and so is
a claude or codex id the bridge cannot place on either scale below.
Top-level launches (yours) are unrestricted - the limits apply only to subagents.

**Down - drudgery to a cheaper model.** Bulk mechanical edits, log scanning,
reformatting, repetitive lookups.

**Sideways - a peer at the same tier.** An independent look: a second opinion on a
design call, or an adversarial read of a conclusion the agent is unsure of. Codex/Sol
and Claude/Opus sit in the same band, so either can ask the other to review its work.
A peer's disagreement is evidence to weigh, not a verdict that overrides the agent.

Down and sideways share one budget: up to **2 helpers** per subagent
(`AGENT_BRIDGE_MAX_HELPERS`). Neither needs a reason.

**Up - a hard piece to a more capable one.** The in-house-specialist model: an agent that
runs into a research question it would be guessing at, a design or judgement call with
real consequences, or writing that has to be right can put a stronger model on that
piece. It costs real money, so it is deliberately narrower than delegating down:

- It must pass **`specialist_reason`** - what it wants from the specialist and why the
  stronger model is needed. A token reason is refused; this is what the human sees in
  `agent_status` next to the bill.
- **1 specialist** per subagent by default (`AGENT_BRIDGE_MAX_ESCALATIONS`; `0` disables
  hiring up entirely).

The two budgets are independent, so spending helpers doesn't cost specialists. Either
way the launching agent stays responsible for the result, including anything that came
back wrong, and writes the final report itself. It is told so.

### How models are ranked

**Neither ladder is a list of model ids to keep up to date.** The previous one was, and
it rotted: written before `claude-opus-5` shipped, it ranked an Opus 5 subagent as
"unknown", which silently switched the direction check off for the model actually in
use. Both sides now derive their ordering.

**Codex** reads `~/.codex/models_cache.json`, which lists models in descending
capability, so it tracks the lineup on its own. Entries that aren't a capability rung
(`codex-auto-review`, `gpt-reserve` - spare capacity, not a tier) are dropped.

**Claude** has no such cache locally, and the bridge must not make a network call to
launch an agent, so claude ids are ranked structurally: the **family** fixes the tier,
the **version** orders within it.

```
family:   mythos = fable  >  opus  >  sonnet  >  haiku
version:  numeric, per segment - 4.10 > 4.8, and 5-1 > 5
```

So `claude-opus-5-2`, `claude-opus-6` and `claude-opus-8-33` all rank themselves the day
they ship, with nothing here to edit. Dated snapshots (`claude-haiku-4-5-20251001`) and
context suffixes (`[1m]`) rank as the base model, and the bare aliases the CLI itself
takes (`--model opus`) mean "latest in that family".

The one thing that still needs a human is a genuinely **new family name**. Capability
can't be inferred from a word, so an unrecognized family is left unranked and logged
with the fix (`add it to CLAUDE_FAMILY_BANDS`) rather than guessed at. Unranked is the
permissive outcome - the launch goes ahead, charged to the helper budget - so a new
family costs a log line, not a blocked agent.

**Across vendors** the two ladders aren't comparable to each other: one's index 6 is not
"below" the other's index 5, they are just integers. A claude<->codex comparison uses
the coarse shared bands instead. Claude's band comes from the family. Codex's comes
from the model name (`CODEX_BAND_PATTERNS`), not its cache position, because the cache
can carry entries that are not rungs and on some installs omits Sol entirely:

```
0  fable, mythos
1  opus,   gpt-5.6-sol, gpt-5.5
2  sonnet, terra, luna, gpt-5.4
3  haiku,  -mini / -nano
```

For example:

```
sol -> opus      sideways (helper budget)
opus -> sol      sideways (helper budget)
opus -> haiku    downward (helper budget)
opus -> fable    upward (specialist budget, needs specialist_reason)
sonnet -> sol    upward (specialist budget, needs specialist_reason)
```

Within one vendor the fine-grained key still decides, so `opus-5 -> opus-4-7` is still
downward. A codex model missing from the local cache falls back to its band.

Only claude and codex are ranked. Opencode, kimi and grok models are unranked: the
launch is allowed, logged, and charged to the helper budget. The same applies to any
model whose direction can't be worked out.

## Reusing a warm agent instead of launching a fresh one

`warm_agents` lists agents whose sessions are still resumable. Check it before
launching anything for related work: an agent that already worked the problem holds
context you would otherwise pay to rebuild, and `continue_*_agent` costs one turn
instead of a whole re-education.

The roster is on disk (`~/.agent-bridge/roster/`), so it survives a server restart -
`continue_*_agent` works on an agent the current process has never seen.

Each entry carries a verdict: `reuse` (warm and recent), `stale` (idle too long, its
picture of the repo may be wrong), `crowded` (enough context that re-reading it may
cost more than starting fresh), `suspect` (last run failed), `retired`. Filter with
`reusable_only=true`, and narrow by `cwd` - matching directory is the strongest signal
that a warm agent's context is relevant.

`retire_agent` drops one from the recommendations when it goes stale or bad, so its
warmth stops reading as an endorsement. The session itself is untouched and still
resumable by hand.

## Trimming the subagent briefing

Every background launch prepends a briefing covering the question channel, aborting,
escalation, notes, delegating down, hiring up, concerns, and the agent's standing to ask
for context.
Each section earned its place from an observed failure, but sending all of them every
time dilutes the ones that matter for the task in hand.

**Structural gating is automatic and not overridable.** A subagent at the recursion
ceiling cannot launch anything, so the `escalate`, `delegate` and `specialist` sections
are dropped - telling it otherwise advertises a door that is locked. Same for `delegate`
when `AGENT_BRIDGE_MAX_HELPERS` is 0, and for `specialist` when
`AGENT_BRIDGE_MAX_ESCALATIONS` is 0. A caller cannot re-enable these; the gate wins over
an explicit section list.

**Caller controls:**

```
launch_claude_agent(prompt=..., multi_phase=False)          # drops the notes section
launch_claude_agent(prompt=..., preamble_sections=[...])    # advanced: exact set
```

`multi_phase=False` is the one worth reaching for: a one-shot job finishes before it
would ever hit a phase boundary, so the note channel is pure overhead. Sections are
`core`, `abort`, `escalate`, `delegate`, `specialist`, `notes`, `concerns`, `standing`; omit
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

## Watching for trouble at a glance

Bare `agent_status()` (no job_id) now returns a **compact** listing — one line per job
with the alarm fields, never inlining stdout/stderr. The new fields the parent should
watch:

- **`warnings[]`** — stderr lines matching sandbox/permission-rejection patterns
  (`permission requested`, `auto-reject`, `denied`, `external_directory`, etc.), deduped
  with counts. Present on a running job as soon as the child writes a matching line.

- **`files_changed`** — count of paths from `git status --porcelain` in the job's cwd.
  A mutation job at 35 minutes with zero changed files is the real signal. Omitted when
  cwd is not a git repo. Cached for 5 seconds so rapid polling doesn't spawn git per call.

- **`suspicious`** — if `returncode` is 0 but the job finished in under 15 seconds with
  under 1000 total tokens, or has stderr warnings but claimed success. A bare boolean
  would make the parent guess, so a `reason` string is always attached. False positives
  are possible but tuned conservatively — a noisy flag would be ignored within a day.

- **`auto_rejection_notifications`** — when the streaming stderr watcher sees a sandbox
  auto-rejection, the bridge raises it as a pending question attributed to that job. The
  subagent already got the rejection and moved on, so these do NOT claim the job is
  BLOCKED. They appear alongside genuine `pending_questions` where the subagent IS waiting.

The full per-job payload (with resume commands, concerns, and session notes) is still
available via `agent_status(job_id=...)`. Raw stdout/stderr is exclusively in
`agent_result`.

**Out-of-sandbox path scan at launch:** every `launch_*` scans the prompt for paths
(`~/...`, `/absolute/...`, `../../foo`) that resolve outside cwd and are not covered by
`add_dirs`. On a hit the bridge logs a `sandbox_path_note` in the launch response naming
the paths. The existing `add_dirs` parameter is the fix — the note tells you what paths
to add before they become auto-rejection errors that cost a full launch.

## Recursion

`AGENT_BRIDGE_MAX_DEPTH` (default 2) caps how deep agents can launch agents. Raise it
only deliberately — each level multiplies token spend, and a runaway chain is tedious
to kill by hand.
