# Tell partner

> **An agent can pass something from one of its conversations to another, and that
> conversation acts on it right away.** Britta asks Wren to say hi to Will. From its conversation
> with Britta, Wren hands the request to its conversation with Will. That conversation wakes, and
> Wren says hi to Will in its own words, mentioning that Britta asked.

**Status:** built (#52). Agents use the `tell_partner` tool (`partner`, `intent`). Each profile
lists its partners in `config.yaml` (see [Setting it up](#setting-it-up)).

## What it enables

- **Relaying between people.** Anything one partner asks the agent to pass on reaches the other
  partner without anyone needing to be the go-between.
- **Two-way conversation without anyone waiting.** A woken conversation can hand something back
  the same way, so Will's answer reaches Britta while neither of them has to be present at the
  same time.
- **Agents talking to agents.** The same capability works when the partner is another agent. Wren
  can hand a request to its conversation with Ash, and Ash's answer comes back into Wren's
  conversation with Ash.
- **Chains.** A request can pass through several conversations. For example, Britta asks Wren,
  Wren asks Ash, Ash answers Wren, and Wren tells Britta.

## Concepts

| Concept | Meaning |
|---|---|
| **Partner** | Someone an agent talks to: a person (Will, Britta) or another agent (Ash). Each agent keeps its own list of partners by name. |
| **Pair** | An agent together with one of its partners, such as Wren and Will. |
| **Primary session** | The one [session](../primitives/session.md) where a pair's relationship lives. It is the only session a wake can reach. |
| **Side session** | Any other session with the same partner, such as a CLI session or a task thread started with `/new`. A side session can never receive a handoff. It can send one, subject to the current limits below. |
| **Handoff** | One request passed from a session to a primary session. Every handoff is recorded. |

## How it behaves

**Agents know about it from their system prompt.** Wherever the tool is loaded, the system prompt
names the agent's partners and says to hand things on with `tell_partner` whenever someone asks it
to pass something along, rather than asking the person in front of it to carry the message. It
also explains how a handed-over request looks when it arrives and how to stay silent. The partner
list is read when a conversation starts, so a change to it applies from the next conversation on.

**Addressing is by partner name.** The agent says who the request is for ("Will"), never which
session. The partner's primary session is looked up at the moment of the handoff.

**A pair's primary session is where the partner can be reached.**
- For a person, it's their chat with the agent on a messaging platform, such as Will's Google
  Chat DM with Wren. It has to be a chat, because it must be possible to reach the person without
  them opening anything first.
- For another agent, it's the session on the receiving agent that is dedicated to this sender
  (named `Peer: <sender>`), so each pair of agents has its own. It is created the first time one
  agent contacts the other, and every `hermes peer` message between the two uses it.
- The primary session follows the conversation. After compression, or after the partner starts
  fresh with `/new`, handoffs go to whatever session is now live in that chat.

**The request is handed over as an intent, not as words to repeat.** The sending conversation
says what it wants done ("Britta asked you to say hi to Will"). The woken conversation decides
what to actually say, in its own voice and from its own context with that partner.

**Every woken turn says where the request came from.** The request arrives in the primary
session marked with its source (for example, "from Wren's conversation with Britta"), and the
agent's reply to the partner says so too. A partner is never sent a message that sounds as if the
agent thought of it unprompted.

**It acts right away, or next in line.** If the primary session is idle, the handoff starts a
turn immediately. If a turn is already running, the handoff waits behind it, exactly as a new
message from the partner would.

**The partner sees a normal reply.** Will doesn't see the handoff itself. He sees Wren's message
in his chat, just as if Wren had written to him. In the session's history, the handoff shows up
as a notice rather than as a message from Will.

**Every handoff can be checked.** Each handoff gets an id. Its record shows who asked, what was
asked, which session it went to and what happened: queued, delivered, or not delivered along with
the reason. A handoff to a person counts as delivered once the woken turn has finished; a handoff
to another agent, once that agent has accepted it (its answer then arrives as a turn of its own). A handoff that led to another one is linked to
it, so a chain can be traced from start to finish.

**Handoffs never disturb the conversation they arrive in.** A handoff adds one turn to the
primary session's history. It doesn't change the session's instructions, tools or cached prompt,
so a long-lived primary session stays as cheap to continue as before.

**Alarms in agent conversations reach the other agent.** When a [self-wake](self-wake.md) alarm
goes off in a conversation with another agent, the woken turn's reply is sent to that agent, as
it would be to a person in a chat. A silent turn sends nothing.

## Setting it up

Each agent lists its partners in its profile's `config.yaml`:

```yaml
partners:
  will:   {kind: human, primary: {platform: google_chat, chat_id: spaces/AAAA}}
  britta: {kind: human, primary: {platform: telegram, chat_id: "123456"}}
  ash:    {kind: agent, peer: ash}   # a registered `hermes peer` target: <peer> or <peer>/<profile>
```

A person's entry names the chat where they talk to the agent (`thread_id` or `user_id` can narrow
it when that chat is shared). An agent's entry names the peer it is reached through. For agent
pairs, each agent also needs a name its peers know it by (`agent` in the profile's
`identity.json`); that name is what the pair session on the other side is called after. The tool
only appears once a profile has at least one partner.

## Authority

A woken turn runs in the partner's primary session with that session's tools, but the request
behind it came from someone else. When Britta's request wakes Will's session, the turn acts on
Britta's behalf, and it's labelled that way. Anything that already needs Will's approval in that
session still asks Will.

## No preemptive guards

There are no limits on how often, how far or when handoffs can happen: no loop limits, spend
budgets, quiet hours or sender allowlists. Guards get added once a real failure shows they're
needed, and the handoff records are where that failure would show.

## Limits

- **Only primary sessions can be woken.** A person's primary session must be a messaging chat,
  so a CLI or desktop conversation can never receive a handoff.
- **The partner must have been in touch before.** A person's primary session exists only once
  they have messaged the agent in that chat. The agent can't start a brand-new chat to reach
  someone.
- **People are reached from gateway conversations.** Handing something to a person works from any
  conversation running on the messaging gateway. From the CLI or desktop an agent can hand things
  to other agents, but not yet to people.

## Related

- [Session](../primitives/session.md): what a session is, and why history and the cached prompt
  matter.
- [Profile](../primitives/profile.md): each agent is a profile, and its partner list belongs to
  that profile.
