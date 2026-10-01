"""``hermes keypair``: the CLI door the agent signs through (``agent/profile_keypair.py``)."""

import base64

from cryptography.hazmat.primitives.serialization import load_pem_public_key

from agent import profile_keypair


def test_cli_signs_without_printing_the_private_key(capsys, monkeypatch, tmp_path):
    from hermes_cli.main import main

    assert profile_keypair.ensure_keypair()
    message = tmp_path / "message.txt"
    message.write_bytes(b"signed by the profile")
    monkeypatch.setattr("sys.argv", ["hermes", "keypair", "sign", str(message)])
    main()
    out = capsys.readouterr().out

    load_pem_public_key(profile_keypair.public_key_pem().encode()).verify(
        base64.b64decode(out.strip()), b"signed by the profile")
    assert "PRIVATE KEY" not in out
