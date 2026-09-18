# Agents that keep their own relationships and their own time, so that people are there only for the decisions.

Status: living document, rewritten 2026-09-18. If you are reading this cold (a fresh cron tick,
after a compaction, or as a newly added agent), this is the *why*. The *what* is in
[`docs/primitives/`](docs/primitives/) and [`docs/capabilities/`](docs/capabilities/). The *how*
is in the GitHub issues.

## What this means

Today an agent speaks only when spoken to. Every turn starts because a person typed something, so
people end up doing jobs that aren't decisions:

- **carrying messages** between conversations the agent is already part of;
- **reminding** the agent of what it said it would do;
- **being the trigger** that starts every turn.

The agent we are building is a continuous participant instead. A turn can start because:

- **a partner spoke**, as today;
- **another of its conversations needed it**: Britta asks Wren to tell Will something, and it
  reaches Will without Britta carrying it;
- **another agent needed it**: Wren asks Ash, and Ash's answer comes back to Wren;
- **the agent decided it was time**: "I'll check back in an hour" is something the agent
  arranges, not just something it says.

## The shape

- **An agent is a [profile](docs/primitives/profile.md).** It has its own memory, credentials and
  history, and is isolated from every other agent.
- **A relationship is a pair**: an agent and one partner, where the partner is a person or another
  agent. Each pair has one **primary session**, the continuous
  [session](docs/primitives/session.md) where that relationship lives and where the partner can
  always be reached. Other sessions with the same partner are allowed, but the primary is the one
  that can be addressed.
- **Sessions reach each other by intent, not by relaying text.** A conversation hands another
  conversation *what is needed*, and the receiving conversation decides what to say, in its own
  words ([primary-session wake](docs/capabilities/primary-session-wake.md)).
- **An agent owns its own time.** Before a turn ends it can set an alarm, and the same session
  wakes when it goes off ([self-wake](docs/capabilities/self-wake.md)).

## What stays with people

Decisions: what matters, what is good enough, when something is wrong, and anything that can't be
undone. A person also stays the check on a mechanism that can now reach people without them. That
check only works if they can see where every message came from.

## How we trust it: visibility, not restriction

- **Every self-started action says where it came from.** A message the agent sends on its own
  initiative names its origin ("Britta asked me to…", "I said I'd check back…"). The danger is not
  an unprompted message; it's one that *sounds authorised*.
- **Every attempt leaves a record.** Handoffs, wakes and alarms are recorded, including the ones
  that failed and why. A chain of actions can be traced back to what started it.
- **No preemptive guards.** Capability is granted freely. Limits (loop caps, budgets, quiet hours,
  allowlists) are added only when the records show a real failure. See the root `AGENTS.md`,
  "What we don't want".

## The test for a new capability

**After it lands, is a person still doing something that isn't a decision?** If yes, that thing is
the next capability. The evidence is usually a habit that looks like helpfulness: an agent asking
someone to pass a message on, or promising to "check back" with no way to do so. A workaround is
the specification.
