"""A symlinked skill is the same skill to every reader, and skill_manage never writes through it.

skills_list / skill_view walk with ``os.walk(followlinks=True)``; skill_manage used ``rglob``,
which does not descend a symlinked dir. A linked skill was listed and viewable but "not found"
to patch/edit/delete, and invisible to create's duplicate check, so create wrote THROUGH the
link into its target. A link whose target can't be reached (Tally's shape: links into a 0700
home) vanished from every scan, and skill_view returned a raw Errno 13.

Rule under test: skill_manage resolves the same set skill_view does; a mutation whose resolved
path is outside every skills root is refused BY NAME (the link and what it points at named);
a link whose target can't be reached is reported as unreachable; a reachable link still lists,
views and loads exactly as before.
"""

import json
import os
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.linux_only,
    pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions"),
]

BODY = "---\nname: {n}\ndescription: {n} skill\n---\n\nBody ORIGINAL.\n"


def _skill(d: Path, name: str) -> Path:
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(BODY.format(n=name))
    return d


@pytest.fixture
def home(tmp_path, monkeypatch):
    """skills/ingest -> outside/ingest (a reachable linked skill, Tally's live layout);
    skills/fin -> outside/finance (a linked category holding 'taxes'); skills/local/ctl real."""
    home = tmp_path / ".hermes"
    skills = home / "skills"
    outside = tmp_path / "outside"
    _skill(outside / "ingest", "ingest")
    _skill(outside / "finance" / "taxes", "taxes")
    _skill(skills / "local" / "ctl", "ctl")
    (skills / "ingest").symlink_to(outside / "ingest", target_is_directory=True)
    (skills / "fin").symlink_to(outside / "finance", target_is_directory=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "_hermes_home_cache", None, raising=False)
    from agent import skill_utils as su
    su._external_dirs_cache_clear()
    import tools.skills_tool as skills_tool
    import tools.skill_manager_tool as smt
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", skills)
    monkeypatch.setattr(smt, "SKILLS_DIR", skills)
    skills_tool._SKILLS_CACHE.clear()
    from agent.prompt_builder import clear_skills_system_prompt_cache
    clear_skills_system_prompt_cache(clear_snapshot=True)
    yield {"home": home, "skills": skills, "outside": outside}
    su._external_dirs_cache_clear()
    clear_skills_system_prompt_cache(clear_snapshot=True)


@pytest.fixture
def unreachable(home):
    """skills/advise -> <locked>/Projects/advise, <locked> mode 000: the uid can't traverse it."""
    locked = home["home"].parent / "willhome"
    _skill(locked / "Projects" / "advise", "advise")
    (home["skills"] / "advise").symlink_to(locked / "Projects" / "advise", target_is_directory=True)
    os.chmod(locked, 0)
    yield {**home, "locked": locked}
    os.chmod(locked, 0o755)


def _manage(**kw):
    from tools.skill_manager_tool import skill_manage
    return json.loads(skill_manage(**kw))


def _body(p: Path) -> str:
    return (p / "SKILL.md").read_text()


# --- (a) a mutation through a link is refused by name, naming the link and its target ---------

@pytest.mark.parametrize("kw", [
    dict(action="patch", old_string="ORIGINAL", new_string="PATCHED"),
    dict(action="edit", content=BODY.format(n="ingest").replace("ORIGINAL", "EDITED")),
    dict(action="write_file", file_path="references/x.md", file_content="x"),
    dict(action="delete"),
], ids=["patch", "edit", "write_file", "delete"])
def test_mutation_through_a_link_is_refused_by_name(home, kw):
    out = _manage(name="ingest", **kw)
    assert out["success"] is False, out
    err = out["error"]
    assert "not found" not in err, err
    assert "'ingest'" in err and "outside the trusted skills directories" in err, err
    assert str(home["skills"] / "ingest") in err and str(home["outside"] / "ingest") in err, err
    assert _body(home["outside"] / "ingest") == BODY.format(n="ingest")
    assert not (home["outside"] / "ingest" / "references").exists()


# --- (b) the refused delete leaves both the link and its target ---------------------------------

def test_refused_delete_leaves_link_and_target(home):
    out = _manage(action="delete", name="ingest")
    assert out["success"] is False and "outside the trusted skills directories" in out["error"], out
    assert (home["skills"] / "ingest").is_symlink()
    assert _body(home["outside"] / "ingest") == BODY.format(n="ingest")


# --- (c) create counts a linked copy as existing, and never writes through the link -------------

def test_create_refuses_a_name_taken_by_a_linked_copy(home):
    """In ANOTHER category, so the path itself is free: only the duplicate-name lookup can refuse.
    Without it a second 'ingest' is created and skill_view goes ambiguous."""
    out = _manage(action="create", name="ingest", category="other",
                  content=BODY.format(n="ingest").replace("ORIGINAL", "NEW"))
    assert out["success"] is False and "already exists" in out["error"], out
    assert not (home["skills"] / "other" / "ingest").exists()
    assert _body(home["outside"] / "ingest") == BODY.format(n="ingest")


# --- (d) a skill under a linked category: same refusal --------------------------------------------

def test_patch_under_a_linked_category_is_refused_by_name(home):
    out = _manage(action="patch", name="taxes", old_string="ORIGINAL", new_string="PATCHED")
    assert out["success"] is False, out
    assert "'taxes'" in out["error"] and "outside the trusted skills directories" in out["error"], out
    assert str(home["skills"] / "fin") in out["error"], out  # the link is the category, not the skill
    assert _body(home["outside"] / "finance" / "taxes") == BODY.format(n="taxes")


# --- the rule is about where the skill resolves, not about links ---------------------------------

def test_real_local_skill_still_patches(home):
    out = _manage(action="patch", name="ctl", old_string="ORIGINAL", new_string="PATCHED")
    assert out["success"] is True, out
    assert "PATCHED" in _body(home["skills"] / "local" / "ctl")


def test_link_into_an_external_dir_still_patches(home, tmp_path):
    """A link that resolves under skills.external_dirs writes where skill_manage can already write
    directly, so refusing it would protect nothing."""
    ext = tmp_path / "ext"
    _skill(ext / "shared", "shared")
    (home["skills"] / "shared").symlink_to(ext / "shared", target_is_directory=True)
    (home["home"] / "config.yaml").write_text(f"skills:\n  external_dirs:\n    - {ext}\n")
    from agent import skill_utils as su
    su._external_dirs_cache_clear()
    out = _manage(action="patch", name="shared", old_string="ORIGINAL", new_string="PATCHED")
    assert out["success"] is True, out
    assert "PATCHED" in _body(ext / "shared")


# --- (e) an unreachable link is named on every surface, never silently dropped -------------------

def test_unreachable_link_is_reported_by_the_scan(unreachable):
    from agent.skill_utils import find_unreadable_skills, get_all_skills_dirs
    found = find_unreadable_skills(get_all_skills_dirs())
    link = str(unreachable["skills"] / "advise")
    hits = [r for p, r in found if p == link]
    assert len(hits) == 1, found  # one entry per link: a second emitter would report it twice
    assert "symlink to " + str(unreachable["locked"] / "Projects" / "advise") in hits[0], hits


def test_unreachable_link_is_listed_as_unreadable(unreachable):
    from tools.skills_tool import skills_list
    out = json.loads(skills_list())
    assert "advise" not in [s["name"] for s in out["skills"]]
    entries = [u for u in out.get("unreadable", []) if u["path"] == str(unreachable["skills"] / "advise")]
    assert entries and entries[0]["kind"] == "link", out.get("unreadable")


def test_unreachable_link_is_named_in_the_prompt_index(unreachable):
    from agent.prompt_builder import build_skills_system_prompt
    tail = build_skills_system_prompt().split("Some skill paths can't be read", 1)
    assert len(tail) == 2 and str(unreachable["skills"] / "advise") in tail[1], tail


def test_skill_view_says_the_link_is_unreachable_not_errno(unreachable):
    from tools.skills_tool import skill_view
    out = json.loads(skill_view("advise"))
    assert out["success"] is False
    err = out["error"]
    assert not err.startswith("[Errno"), err
    assert "'advise'" in err and "can't be reached" in err, err
    assert str(unreachable["locked"] / "Projects" / "advise") in err, err


def test_skill_manage_says_the_link_is_unreachable(unreachable):
    out = _manage(action="patch", name="advise", old_string="ORIGINAL", new_string="X")
    assert out["success"] is False and "can't be reached" in out["error"], out


def test_create_does_not_write_into_an_unreachable_link(unreachable):
    out = _manage(action="create", name="advise", content=BODY.format(n="advise"))
    assert out["success"] is False, out
    assert (unreachable["skills"] / "advise").is_symlink()


# --- CONTROL: a reachable link reads exactly as before -------------------------------------------

def test_control_reachable_link_lists_views_and_indexes(home):
    from agent.skill_utils import find_unreadable_skills, get_all_skills_dirs
    from agent.prompt_builder import build_skills_system_prompt
    from tools.skills_tool import skill_view, skills_list
    assert "ingest" in [s["name"] for s in json.loads(skills_list())["skills"]]
    view = json.loads(skill_view("ingest"))
    assert view["success"] is True and "Body ORIGINAL." in view["content"], view
    assert find_unreadable_skills(get_all_skills_dirs()) == []
    index = build_skills_system_prompt()
    assert "ingest" in index and "Some skill paths can't be read" not in index
