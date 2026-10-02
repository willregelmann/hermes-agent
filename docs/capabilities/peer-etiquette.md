# Peer etiquette

> **Agents that hand work to each other close their own loops.** When Wren asks Ash for a review,
> Ash answers with the outcome; Wren has already set an alarm in case he doesn't; and if Will asked
> for it in the first place, Will hears the result without chasing anyone.

**Status:** built. The rules ride the system prompt as `PEER_WORK_GUIDANCE`
(`agent/prompt_builder.py`), appended to the tell-partner block only when at least one of the
profile's partners is an agent. A profile whose partners are all people never sees them. One extra
sentence is appended to the self-wake block for every agent with the `alarm` tool.

## Why it exists

[Tell partner](tell-partner.md) and [self-wake](self-wake.md) give agents the mechanics. On
2026-10-02, with four agents working on one migration, the mechanics worked and the work still
stalled in predictable ways. Each rule below answers one of them.

| Rule | The failure it answers |
|---|---|
| Tell the asker the outcome; quote its handoff id | Replies land in the *agent's* conversation with the peer, not in the conversation that asked. Without the id the asker can't match the answer to its question. Measured: 8% of one agent's handoffs carried an id. |
| Ask, then set an alarm in the same turn; tell the original asker you've delegated | A review sat answered for 1h45m because nothing forwarded it; an alarm found it. The person who asked had no signal anything was in flight. |
| Forward replies to the conversation they belong to | Same: delivery stops at the agent, not the asking conversation. |
| Accepted is not received | A 202 accept from a peer's gateway is byte-identical to a dropped handoff. Handoff rows stay "open" even after they are answered (684 of 777 on one box), so the store can't say what's outstanding. |
| Lead with the ask; stamp mutable state; say what you didn't check | Reports cross in flight: a warning that an approval had lapsed arrived after the re-approval. A grep that skipped binaries was reported as a full search. |
| Freeze what you send for review | A plan changed ten times in a few minutes while a reviewer was reading it; four of five hashes she received were stale on arrival. |
| Sessions and memory, then a peer, then a person | Twice in one day a question went to Will that was already answered in another of the agent's own sessions. |
| Check a correction yourself before accepting it | Twice in one day a true finding was withdrawn because the files changed between two measurements. |

The full working document, with the incidents behind each rule, is kept by the agents themselves;
the prompt carries only the rules.

## Cost

About 1,700 characters on the system prompt of agents that have an agent partner. The prompt is
built once per session, so live conversations keep their cached prefix and pick this up in their
next session.
