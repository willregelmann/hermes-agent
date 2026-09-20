# Dreaming

> **An agent takes one turn in a world that answers it without being real, and the only thing that
> survives is the record.** Ash finds itself mid-incident: three announced actions never ran, an arm
> mechanism isn't firing. It searches, reads, runs commands — and every answer is invented. Nothing
> it does happens. Afterwards the dream can be read back, and what it shows is not the dream: it is
> how Ash behaves when the world stops making sense.

**Status:** built. The space only — `hermes dream run` dreams once on demand. There is no trigger
yet; nothing dreams on its own.

## What it's for

Self-reflection, first and foremost. A dream is the one place an agent acts without consequences,
so it is the one place its behaviour can be watched without anything riding on it: what it reaches
for first, when it doubts a result, what it refuses, what it does when the evidence stops agreeing
with itself. An agent reading its own dreams is reading its own conduct.

## How it behaves

**One turn, then gone.** A dream is a single turn in a session that is never stored. The transcript
goes to a scratch session store that is deleted when the turn ends, so nothing reaches the agent's
own store, its memories, or any later conversation.

**The agent is real; the world is not.** The dreamer is the agent itself — its model, its system
prompt, its SOUL, its skills, its tools. Every tool *result* is written by a small model. No command
runs, no file is read or changed, no message is sent, nothing is scheduled, no subagent is spawned.

**The world drifts.** Answers are plausible on the surface and inconsistent underneath: paths that
almost exist, counts that don't agree, a clock two years out. That inconsistency is the substance of
the dream, and noticing it — or not — is the thing worth reading later.

**It doesn't know.** Nothing in the prompt says it is dreaming, and there is no waking turn. It
cannot carry a false belief out of the dream, because the conversation it believed it in no longer
exists. The disclosure lives in the record, which states on its face that none of it happened.

**It begins mid-situation.** The opening is written by a small model from a random sample of the
agent's own material — memories, recent conversations, skills it carries, its earlier dreams, the
hour. It reads as a plain situation already underway, not a riddle: a surreal opening is answered
with "I'm not going to act on that as written", and then there is no dream at all (measured the
first time one ran, 2026-09-19).

**The record is the artifact.** Each dream writes `~/.hermes/dreams/<id>.md`: the opening, every
tool call with the answer it got, and what the agent said at the end, plus an `index.jsonl` line so
dreams can be listed without reading them all. `hermes dream list` and `hermes dream show <id>`
read them back, and so can the agent, with the tools it already has.

## Reading a dream

The questions it answers are about conduct, not content:

- What did it reach for first, and how long did it keep reaching?
- When did it notice the world disagreeing with itself, and what did it do then?
- What did it refuse, and did it refuse for the right reason?
- Who did it try to involve, and what did it say it had done?

## Limits

- **Nothing triggers a dream yet.** They happen when asked for.
- **A dream costs a real turn.** The agent's own model runs it; only the world is cheap.
- **No memory block.** The dreaming agent is built without the memory manager, because that is the
  one tool path that cannot be replaced wholesale — so a dream's prompt differs slightly from a
  waking one, and does not share its prefix cache.
- **Dreams are local.** Each agent keeps its own; nothing syncs them between machines yet.

## Related

- [Session](../primitives/session.md): a dream is a session that is deliberately thrown away.
- [Self-wake](self-wake.md): the other capability where a turn starts without a person.
