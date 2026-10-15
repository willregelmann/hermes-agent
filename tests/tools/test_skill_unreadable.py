"""An unreadable skill is reported as unreadable, never as missing.

A skill dir the process can't enter, or a SKILL.md it can't read, used to vanish
from every scan: skills_list didn't list it, the prompt index didn't name it,
skill_view and skill_manage said "not found". An agent then believes the skill
was never written. Each reader below must name it instead. Both shapes are
covered: an untraversable skill dir, and an unreadable SKILL.md in a readable dir.
"""

import json
import os
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.linux_only,
    pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions"),
]


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    skills = home / "skills"
    for name in ("readable", "lockeddir", "lockedfile"):
        d = skills / "finance" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} skill\n---\n\nBody.\n")
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
    os.chmod(skills / "finance" / "lockedfile" / "SKILL.md", 0)
    os.chmod(skills / "finance" / "lockeddir", 0)
    yield home
    os.chmod(skills / "finance" / "lockeddir", 0o755)
    os.chmod(skills / "finance" / "lockedfile" / "SKILL.md", 0o644)
    su._external_dirs_cache_clear()
    clear_skills_system_prompt_cache(clear_snapshot=True)


@pytest.mark.parametrize("name", ["lockeddir", "lockedfile"])
def test_skills_list_names_the_unreadable_skill(home, name):
    from tools.skills_tool import skills_list
    out = json.loads(skills_list())
    assert [s["name"] for s in out["skills"]] == ["readable"]  # precondition: the readable one still lists
    assert any(Path(u["path"]).parts[-2:] in ((("finance", name)), (name, "SKILL.md"))
               for u in out.get("unreadable", [])), out


@pytest.mark.parametrize("name", ["lockeddir", "lockedfile"])
def test_prompt_index_names_the_unreadable_skill(home, name):
    from agent.prompt_builder import build_skills_system_prompt
    index = build_skills_system_prompt()
    assert "readable" in index  # precondition: the index was built at all
    tail = index.split("Some skill paths can't be read", 1)
    assert len(tail) == 2 and name in tail[1], index


def test_skill_view_points_at_an_untraversable_dir_instead_of_saying_not_found(home):
    from tools.skills_tool import skill_view
    out = json.loads(skill_view("lockeddir"))
    assert out["success"] is False
    assert "can't be read" in out["error"] and str(home / "skills" / "finance" / "lockeddir") in out["error"], out


def test_skill_manage_points_at_an_untraversable_dir_instead_of_saying_not_found(home):
    from tools.skill_manager_tool import skill_manage
    out = json.loads(skill_manage(action="patch", name="lockeddir", old_string="Body.", new_string="New."))
    assert out["success"] is False
    assert "can't be read" in out["error"] and str(home / "skills" / "finance" / "lockeddir") in out["error"], out


def test_a_skill_inside_a_locked_category_points_at_the_category(home):
    # Ash's probe on #99: with the whole category folder locked, the skill's own name never
    # appears in any path, so an exact-name match finds nothing and "not found" came back.
    from tools.skills_tool import skill_view
    cat = home / "skills" / "finance"
    os.chmod(cat, 0)
    try:
        out = json.loads(skill_view("readable"))
    finally:
        os.chmod(cat, 0o755)
    assert out["success"] is False
    assert "can't be read" in out["error"] and str(cat) in out["error"], out


def test_a_locked_folder_is_not_claimed_to_be_a_skill(home):
    # Ash's probe on #99: a locked folder with no skill in it was reported as
    # "Skill 'stray' exists". A folder that can't be entered is only ever named as a folder.
    # (skill_view's direct-path probe already fails on the EACCES itself; skill_manage reaches
    # the not-found path, so it is the one that can make the false claim.)
    from tools.skill_manager_tool import skill_manage
    from tools.skills_tool import skills_list
    stray = home / "skills" / "stray"
    stray.mkdir()
    os.chmod(stray, 0)
    try:
        out = json.loads(skill_manage(action="patch", name="stray", old_string="a", new_string="b"))
        listed = json.loads(skills_list())["unreadable"]
    finally:
        os.chmod(stray, 0o755)
    assert "may be inside one" in out["error"] and str(stray) in out["error"], out
    assert "exists" not in out["error"], out
    assert {"path": str(stray), "kind": "folder", "error": "Permission denied"} in listed, listed
    assert [u["kind"] for u in listed if u["path"].endswith("lockedfile/SKILL.md")] == ["skill_file"], listed


def test_a_skill_locked_after_the_first_build_is_named_in_the_next(home):
    # The in-process index cache is shared by every session a long-lived gateway serves. A
    # permission change after the first build must still reach the next session's prompt.
    from agent.prompt_builder import build_skills_system_prompt
    late = home / "skills" / "finance" / "readable"
    first = build_skills_system_prompt()
    assert str(late) not in first  # precondition: not named while readable
    os.chmod(late, 0)
    try:
        second = build_skills_system_prompt()
    finally:
        os.chmod(late, 0o755)
    assert str(late) in second.split("Some skill paths can't be read", 1)[1], second


def test_a_readable_skill_is_not_reported(home):
    from agent.skill_utils import find_unreadable_skills
    hits = find_unreadable_skills([home / "skills"])
    assert hits and not any("readable" == Path(p).name or "/readable/" in p for p, _ in hits), hits


def test_a_missing_skills_dir_is_not_unreadable(tmp_path):
    from agent.skill_utils import find_unreadable_skills
    assert find_unreadable_skills([tmp_path / "nope"]) == []
