"""Blue-green deploy targeting rules for ``hermes peer deploy``.

THE PROPERTY THIS FILE EXISTS TO PROTECT
========================================

An agent must never actuate its own gateway lifecycle. The safety of the
blue-green scheme rests entirely on separation of duties: one agent is always
known-good and able to roll the other back. If an agent can name its own host
as the deploy target, that property is gone and we have rebuilt the forbidden
self-restart with extra steps.

Every check here is therefore about ONE question — *is this target actually
somebody else?* — and every check answers it from configuration and from the
network, never by inspecting the text of a command.

WHY NOT PARSE SSH
-----------------

The obvious implementation is to let the lifecycle guard permit a command that
"looks peer-directed", i.e. one starting with ``ssh <host>``. That is a regex
standing between an agent and its own kill switch, and ssh is uncommonly good
at making one host look like another:

  * ``~/.ssh/config`` ``Host`` aliases rename anything to anything;
  * ``ProxyJump`` / ``ProxyCommand`` land the session somewhere else entirely;
  * ``-o HostName=...`` overrides the hostname given on the command line;
  * a DNS name can resolve to a loopback or to this machine's own LAN address;
  * ``HostName localhost`` in a config stanza is legal and invisible in argv.

Each of those is a way to look peer-directed while landing on self. So the
allow is not "a command that looks remote" but "a named CLI verb whose target
is a key in ``bot_peers``, resolved and then proven not to be this machine."
Free-form ssh containing lifecycle phrasing stays blocked exactly as before.

WHAT IS DELIBERATELY NOT DEFENDED AGAINST
-----------------------------------------

An operator who edits ``bot_peers`` to point a peer name at their own host can
defeat this. That is accepted: config is trusted input, the agent does not
write it unsupervised, and the check exists to prevent an agent from reasoning
its way into self-restart, not to survive a hostile local admin.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Iterable, Optional
from urllib.parse import urlparse


class DeployTargetError(Exception):
    """Raised when a deploy target is missing, malformed, or resolves to self.

    Deliberately its own type: callers must be able to fail closed on a
    targeting problem without catching unrelated network errors.
    """


def _own_hostnames() -> set[str]:
    """Every name this machine answers to, lowercased."""
    names = {"localhost", "127.0.0.1", "::1"}
    try:
        host = socket.gethostname()
        names.add(host.lower())
        # The mDNS/.local form is how these boxes actually address each other.
        names.add(f"{host.lower()}.local")
        names.add(socket.getfqdn().lower())
    except OSError:
        pass
    return names


def _own_addresses() -> set[str]:
    """Every IP literal bound on this machine.

    Used to catch a peer entry whose hostname is unfamiliar but which resolves
    to an address we are listening on — the DNS-alias case.
    """
    addrs: set[str] = {"127.0.0.1", "::1"}
    try:
        host = socket.gethostname()
        for family, _, _, _, sockaddr in socket.getaddrinfo(host, None):
            addrs.add(str(sockaddr[0]))
    except (OSError, socket.gaierror):
        pass
    # Also whatever the default route binds, which getaddrinfo(hostname) can miss.
    for probe in ("8.8.8.8", "1.1.1.1"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect((probe, 80))
                addrs.add(str(s.getsockname()[0]))
            finally:
                s.close()
        except OSError:
            continue
    return addrs


def _resolve(host: str) -> set[str]:
    try:
        return {str(ai[4][0]) for ai in socket.getaddrinfo(host, None)}
    except (OSError, socket.gaierror):
        return set()


def host_from_peer_url(url: str) -> Optional[str]:
    """Extract the bare hostname from a peer's configured base URL.

    Whitespace-only input must yield None rather than a blank-ish host: a
    peer entry of "   " would otherwise sail through the self-checks (it
    matches no known name and resolves to nothing) and be handed to ssh.
    """
    if not url or not url.strip():
        return None
    url = url.strip()
    parsed = urlparse(url if "://" in url else f"http://{url}")
    host = (parsed.hostname or "").strip().lower()
    return host or None


def assert_target_is_not_self(
    peer_name: str,
    peer_url: str,
    *,
    extra_self_names: Iterable[str] = (),
) -> str:
    """Return the validated target hostname, or raise ``DeployTargetError``.

    Three independent checks, all of which must pass. They are independent on
    purpose: a hostname can be unfamiliar while resolving to us, and an address
    can be a loopback that no name reveals.
    """
    host = host_from_peer_url(peer_url)
    if not host:
        raise DeployTargetError(
            f"peer '{peer_name}' has no resolvable host in its URL ({peer_url!r}). "
            "Refusing to deploy to an unparseable target."
        )

    self_names = _own_hostnames() | {n.lower() for n in extra_self_names}

    # 1. Name match, including the .local form and the FQDN.
    if host in self_names:
        raise DeployTargetError(
            f"peer '{peer_name}' resolves to THIS machine ({host}). An agent must "
            "never deploy itself — blue-green requires a second party. Check "
            "bot_peers in config.yaml."
        )

    # 2. Literal loopback, which no name lookup would flag.
    try:
        if ipaddress.ip_address(host).is_loopback:
            raise DeployTargetError(
                f"peer '{peer_name}' points at loopback ({host}). An agent must "
                "never deploy itself."
            )
    except ValueError:
        pass  # not an IP literal; the name checks above/below cover it

    # 3. Address match: the hostname is unfamiliar but lands on an address we
    #    are bound to. This is the ssh-alias / DNS case the docstring warns of.
    resolved = _resolve(host)
    if resolved:
        mine = _own_addresses()
        overlap = resolved & mine
        if overlap:
            raise DeployTargetError(
                f"peer '{peer_name}' ({host}) resolves to an address on THIS "
                f"machine ({', '.join(sorted(overlap))}). An agent must never "
                "deploy itself."
            )
        if any(ipaddress.ip_address(a).is_loopback for a in resolved):
            raise DeployTargetError(
                f"peer '{peer_name}' ({host}) resolves to loopback. An agent must "
                "never deploy itself."
            )

    return host


def validate_sha(sha: str) -> str:
    """Accept only a full or abbreviated hex SHA.

    The value is interpolated into a remote git command, so anything that is
    not hex is refused rather than escaped — a deploy target is not a place to
    be clever about quoting.
    """
    cleaned = (sha or "").strip()
    if not cleaned:
        raise DeployTargetError("no SHA given.")
    if not (7 <= len(cleaned) <= 40):
        raise DeployTargetError(
            f"SHA {cleaned!r} must be 7-40 characters; got {len(cleaned)}."
        )
    if not all(c in "0123456789abcdefABCDEF" for c in cleaned):
        raise DeployTargetError(
            f"SHA {cleaned!r} is not hexadecimal. Refusing to interpolate a "
            "non-SHA value into a remote git command."
        )
    return cleaned.lower()
