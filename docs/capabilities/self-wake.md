# Self-wake

> **Before a turn ends, an agent can set an alarm for itself. When the alarm goes off, the same
> conversation wakes and the agent takes another turn.** Wren tells Will "I'll check the deploy
> again in an hour". An hour later Wren wakes in that conversation, checks the deploy, and tells
> Will what it found.

**Status:** built (#53). Agents use the `alarm` tool (`set`, `list`, `cancel`).

## What it enables

- **Promises that are kept.** "I'll check back in an hour" is something the agent actually
  arranges, not only something it says.
- **Waiting on slow things.** Examples are a deploy, a CI run, a long download, or a reply from
  someone else. The agent comes back when it expects the thing to be done, instead of stopping or
  watching it continuously.
- **Follow-ups at a set time.** "I'll remind you at 5 pm" leads to a turn at 5 pm in the same
  conversation.
- **Pacing its own work.** An agent can split long work across turns spread over time. At the end
  of each woken turn it sets the next alarm if more is needed.

## How it behaves

**Agents know about it from their system prompt.** Wherever the tool is loaded, the system prompt
says to set an alarm in the same turn whenever the agent says it will check back later. It also
explains how the woken turn looks and how to stay silent.

**It's set during a turn, for a delay or a time.** Examples are "in 1 hour", "in 20 minutes" or
"at 17:00". Each alarm goes off once. To repeat, the agent sets another alarm in the woken turn.

**Setting an alarm is confirmed.** The agent gets back an alarm id and the time it's due. If the
alarm can't be set, the agent is told so, and it doesn't end up promising something that won't
happen.

**The agent leaves itself a note.** The note is written when the alarm is set, by the turn that
knows why it's waiting ("check whether the deploy to ha-pi finished; if it failed, look at the
unit log"). The woken turn starts from that note, not from a blank prompt.

**It wakes the same conversation.** The woken turn has the conversation's full history. The note
arrives marked as the agent's own alarm, along with when it was set, rather than as a message from
the partner. The partner sees the agent's reply as an ordinary message, and never sees the alarm
itself.

**A woken turn can end silently.** If there's nothing worth saying (the deploy is still running,
nothing has changed), the agent can finish the turn without messaging the partner. The turn still
happens and stays in the conversation's history, and the agent can set another alarm before it
ends.

**Partners can be people or other agents.** Self-wake works the same way in a conversation with
another agent as in one with a person: the woken turn runs in that conversation with its full
history, and its reply is sent to the other agent just as it would be to a person.

**It waits its turn.** If the conversation is busy when the alarm goes off, the alarm fires once
the current turn finishes. A new message from the partner is always handled first.

**It survives restarts.** Alarms are stored durably. If the agent is down when an alarm is due, the
alarm fires as soon as the agent is back, and the woken turn knows it's running late.

**`/new` cancels it.** An alarm belongs to the conversation it was set in. If the partner starts
fresh with `/new` or `/reset` before it goes off, the alarm is cancelled along with that
conversation. If it still matters, the agent can set a new alarm in the new conversation. Nothing
else cancels an alarm: a restart, the agent being unloaded to free memory, or the conversation
being compressed all leave it pending.

**The agent can see and cancel its alarms.** If the deploy finishes early, or the plan changes,
the agent can cancel an alarm it set in the conversation.

## How it differs from existing features

| Feature | Who sets it | Where it runs | When it runs |
|---|---|---|---|
| **Self-wake** | The agent | The same conversation | Once, after a delay or at a time |
| `/loop` | The person, with a slash command | The same conversation | Right away, then repeatedly |
| Cron job | The person or the agent | A fresh session with no conversation history | On a schedule |

## No preemptive guards

There are no limits on how many alarms an agent sets, how often, or how far ahead. Guards get added
once a real failure shows they're needed.

## Limits

- **An alarm fires only in the session it was set in.** It never fires in another session, because
  only that session has the context the alarm was made for. The session has to exist and something
  has to be able to run a turn in it: the gateway for a chat, the desktop backend with the session
  open, or a CLI running that session. If nothing can when the alarm is due, it fires as soon as the
  session is next opened, marked late.

## Related

- [Tell partner](tell-partner.md): a self-wake is the same kind of wake, with the
  agent's own conversation as the target and a delay before it fires.
- [Session](../primitives/session.md): a woken turn is an ordinary turn in the session, so the
  prompt cache and history stay intact.
