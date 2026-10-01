"""Per-profile Ed25519 keypair: a signing identity the agent can use but never read.

The gateway creates ``<HERMES_HOME>/keypair/profile_ed25519.pem`` (PKCS8, owner-only) at startup
for every profile it serves and never replaces an existing one. The private key stays behind this
module: callers get ``sign()`` and the public key, and the directory is on the agent-facing
read/write/delivery denylists (``agent/file_safety.py``, ``gateway/platforms/base.py``,
``hermes_cli/web_routers/files.py``). ``--clone-all`` skips it, so a clone gets its own key.
Nothing consumes the signatures yet.
"""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path
from typing import Optional, Union

from hermes_constants import display_hermes_home, get_hermes_home, mkdir_under_hermes_home

KEYPAIR_DIR = "keypair"
PRIVATE_KEY_NAME = "profile_ed25519.pem"

_Home = Optional[Union[str, Path]]


class KeypairMissingError(RuntimeError):
    """The profile has no keypair yet (its gateway has not started since this shipped)."""


def private_key_path(home: _Home = None) -> Path:
    """Private key location for ``home`` (the active profile's home when omitted)."""
    return (Path(home) if home is not None else get_hermes_home()) / KEYPAIR_DIR / PRIVATE_KEY_NAME


def ensure_keypair(home: _Home = None) -> bool:
    """Generate the profile's keypair unless it already has one; True when a key was created."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    path = private_key_path(home)
    mkdir_under_hermes_home(path.parent)
    with suppress(OSError):
        os.chmod(path.parent, 0o700)
    if path.exists():
        return False
    pem = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    )
    try:
        # O_EXCL: a concurrent creator wins and its key is the profile's key.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    try:
        os.write(fd, pem)
    finally:
        os.close(fd)
    return True


def _load_private_key(home: _Home):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    path = private_key_path(home)
    try:
        pem = path.read_bytes()
    except FileNotFoundError:
        raise KeypairMissingError(
            f"This profile has no keypair yet ({display_hermes_home()}/{KEYPAIR_DIR}/). "
            "The gateway generates one at startup: restart it with `hermes gateway restart`."
        ) from None
    key = load_pem_private_key(pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f"{path} does not hold an Ed25519 private key")
    return key


def sign(message: bytes, home: _Home = None) -> bytes:
    """64-byte Ed25519 signature of ``message`` under the profile's private key."""
    return _load_private_key(home).sign(message)


def public_key_pem(home: _Home = None) -> str:
    """The profile's public key as SubjectPublicKeyInfo PEM (what ``openssl pkeyutl -verify -pubin`` reads)."""
    from cryptography.hazmat.primitives import serialization

    return _load_private_key(home).public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
