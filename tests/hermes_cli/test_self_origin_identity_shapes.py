"""_self_origin() must return None for an identity.json that PARSES but is not
an object -- the class the existing coverage cannot see.

Issue #66. The try/except in _self_origin() guards the open() and the
json.load() only, so a file that fails to parse is handled correctly while one
that parses into a list, string, number, bool or null reached `data.get(...)`
and raised AttributeError out of a function whose docstring promises None.

The existing jailed-home case (no identity.json at all) and the malformed cases
all fail json.loads, so every one of them is caught by the try. `parses` in the
table below is the discriminator: only the rows where json.loads SUCCEEDS
exercise this contract.
"""

import json
import os

import pytest

from hermes_cli.subcommands.peer import _self_origin

# (identity.json bytes, parses-as-json)
PARSES_BUT_NOT_AN_OBJECT = [
    pytest.param('["ash"]', id="json-list"),
    pytest.param("[]", id="json-empty-list"),
    pytest.param('"ash"', id="json-string"),
    pytest.param("42", id="json-number"),
    pytest.param("null", id="json-null"),
    pytest.param("true", id="json-bool"),
]


def _home_with_identity(tmp_path, monkeypatch, body):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "identity.json").write_text(body, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.mark.parametrize("body", PARSES_BUT_NOT_AN_OBJECT)
def test_parses_but_not_an_object_yields_none_not_an_exception(
    tmp_path, monkeypatch, body
):
    """The whole failing class: json.loads succeeds, the result is not a dict."""
    # Pin BOTH halves of the premise: the body must PARSE (otherwise the
    # open/parse try/except handles it and the arm proves nothing) AND must
    # not be an object (a dict body returns None on unfixed code, so it would
    # pass green while exercising nothing).
    assert not isinstance(json.loads(body), dict)

    _home_with_identity(tmp_path, monkeypatch, body)
    assert _self_origin() is None


def test_well_formed_object_still_yields_an_address(tmp_path, monkeypatch):
    """Control: the guard must not turn every answer into None."""
    _home_with_identity(
        tmp_path, monkeypatch, json.dumps({"agent": "probe", "host": "probe-host"})
    )
    origin = _self_origin()
    assert origin == {"agent": "probe", "host": "probe-host"}


def test_unparseable_file_still_yields_none(tmp_path, monkeypatch):
    """The other half of the contract, already honoured: parse failure -> None."""
    _home_with_identity(tmp_path, monkeypatch, '{"agent": "as')
    assert _self_origin() is None


def test_object_without_an_agent_still_yields_none(tmp_path, monkeypatch):
    """A dict with no usable agent was already correct; the guard must not
    change it, and this arm fails if the guard is written as a blanket
    `return None` rather than a type check."""
    _home_with_identity(tmp_path, monkeypatch, json.dumps({"host": "h"}))
    assert _self_origin() is None


def test_nested_agent_name_is_still_unwrapped(tmp_path, monkeypatch):
    """Deliberate behaviour at peer.py:65-66, not a malformed shape. An early
    probe misread this as a defect; pinning it stops the guard from being
    'fixed' into rejecting it."""
    _home_with_identity(
        tmp_path, monkeypatch, json.dumps({"agent": {"name": "probe"}, "host": "h"})
    )
    assert (_self_origin() or {}).get("agent") == "probe"
