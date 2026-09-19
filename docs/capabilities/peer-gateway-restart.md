# Peer gateway restart

> **An agent can ask another agent's gateway to restart.** The gateway lets the work in progress
> finish, restarts, and comes back. When a change lands on Wren's machine, Ash asks Wren's gateway
> to restart. Nobody has to press the button, and nobody needs to log into Wren's machine.

**Status:** built (#54). Agents use the `restart_peer_gateway` tool, and people use
`hermes peer restart <peer>`.

## Why it exists

An agent can't restart its own gateway from a command: the restart kills the process that is
running the command. So a person used to do it, even when the only thing they added was pressing
the button. A gateway can restart itself cleanly, the same way it does when someone types
`/restart` in chat. This capability lets a peer ask it to.

## What it enables

- **Restarting a peer to pick up a change** that is already on its machine, such as edited config
  or updated code.
- **Agents restarting each other** with no human in the loop.
- **Restarting without access to the machine.** The requester needs no login and no service
  permissions on the peer's machine.

## How it behaves

**Agents know about it from their system prompt.** Whenever an agent has registered peers, its
system prompt explains the capability and when to use it. The agent doesn't have to discover it from
the tool list.

**The request goes to the peer's gateway, not its agent.** No agent turn runs, and the peer's agent
doesn't have to be responsive. Only the gateway has to be up and reachable.

**It is a graceful restart.** Turns already in progress finish first, up to the normal drain
timeout. Then the gateway restarts under its service manager. This is exactly what happens when
someone types `/restart` in one of its chats.

**Holding the peer's key is the consent.** Anyone holding the peer's API key can ask. That is the
same credential a registered peer already uses to message it, and whoever gave out the key has
agreed to restarts in advance.

**Checking that it came back is by convention.** After asking, the requester messages the peer
("which version are you running?"). There is no automatic "back online" notice.

**Every agent on that gateway restarts.** If the peer's gateway serves several profiles, all of
them restart, not just the one that was addressed.

## No preemptive guards

There are no limits on who among the key holders may ask, how often, or when. Waiting for work in
progress to finish is not a guard; it is what makes the restart graceful.

## Limits

- **The peer's gateway has to be running and reachable.** A gateway that is down can't be asked,
  and that case goes to Will.
- **Restart only.** It doesn't change which code the peer runs. Deploying new code is separate.
  Today that is `hermes peer deploy`, over ssh.

## Related

- [Profile](../primitives/profile.md): one gateway can serve several profiles, and a restart
  restarts all of them.
- [Tell partner](tell-partner.md): the check-back message afterwards is an ordinary
  message to the peer.
