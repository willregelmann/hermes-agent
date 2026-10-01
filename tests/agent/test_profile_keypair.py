"""Per-profile signing keypair (``agent/profile_keypair.py``): the agent signs, never reads."""

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from agent import profile_keypair
from agent.file_safety import get_read_block_error, is_write_denied


def test_signature_verifies_under_the_profile_public_key_and_keys_are_per_home(tmp_path):
    home_a, home_b = tmp_path / "a", tmp_path / "b"
    assert profile_keypair.ensure_keypair(home_a) and profile_keypair.ensure_keypair(home_b)
    pub_a = profile_keypair.public_key_pem(home_a)
    signature = profile_keypair.sign(b"hello", home_a)

    load_pem_public_key(pub_a.encode()).verify(signature, b"hello")
    with pytest.raises(InvalidSignature):
        load_pem_public_key(profile_keypair.public_key_pem(home_b).encode()).verify(signature, b"hello")
    # A later gateway start keeps the identity it finds.
    assert profile_keypair.ensure_keypair(home_a) is False
    assert profile_keypair.public_key_pem(home_a) == pub_a


def test_agent_file_tools_cannot_read_or_replace_the_private_key():
    assert profile_keypair.ensure_keypair()
    key_path = profile_keypair.private_key_path()

    assert get_read_block_error(str(key_path))
    assert is_write_denied(str(key_path))
    assert is_write_denied(str(key_path.parent / "replacement.pem"))

