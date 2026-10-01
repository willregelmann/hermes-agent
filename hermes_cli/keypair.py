"""``hermes keypair`` — sign with the active profile's key (``agent/profile_keypair.py``)."""

from __future__ import annotations

import base64
import sys
from pathlib import Path


def _cmd_show(_args) -> None:
    from agent.profile_keypair import public_key_pem
    sys.stdout.write(public_key_pem())


def _cmd_sign(args) -> None:
    from agent.profile_keypair import sign
    message = sys.stdin.buffer.read() if args.file == "-" else Path(args.file).read_bytes()
    print(base64.b64encode(sign(message)).decode("ascii"))


_HANDLERS = {"show": _cmd_show, "sign": _cmd_sign}


def keypair_command(args) -> int:
    from agent.profile_keypair import KeypairMissingError
    handler = _HANDLERS.get(getattr(args, "keypair_action", None))
    if handler is None:
        print("Usage: hermes keypair {show|sign [file]}")
        return 2
    try:
        handler(args)
    except (KeypairMissingError, OSError, ValueError) as exc:
        print(f"hermes keypair: {exc}", file=sys.stderr)
        return 1
    return 0
