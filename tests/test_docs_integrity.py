"""The notes corpus must stay whole and reachable.

CLAUDE.md used to be one 1,170-line file. Its reference half now lives in
`.claude/skills/`, which is cheaper per session — a skill loads only when the work
matches it. The cost of that is a NEW failure mode this project had not had before: a
hard-won rule can stop being loaded without anything looking broken. No test fails, no
run breaks, the note is simply never seen again and the mistake it prevents gets made a
second time.

These four tests are what make the split safe to have done:

  1. nothing was lost in the move          (split_check against the frozen baseline)
  2. every skill is POINTED AT by CLAUDE.md — a skill nothing points at is invisible,
     because triggering would then rest entirely on description matching
  3. every pointer resolves — the reverse, so a renamed skill can't leave a dead link
  4. every SKILL.md has the frontmatter that makes it loadable at all

2 and 3 are deliberately a matched pair. Checking only one direction leaves the other
free to rot, and each direction fails in a way the other cannot see.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKILLS_DIR = ROOT / ".claude" / "skills"
CLAUDE_MD = ROOT / "CLAUDE.md"


def _load_split_check():
    path = ROOT / ".claude" / "tools" / "split_check.py"
    if not path.exists():
        pytest.skip("split_check.py not present")
    spec = importlib.util.spec_from_file_location("split_check", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _skill_dirs() -> list[pathlib.Path]:
    if not SKILLS_DIR.exists():
        return []
    return sorted(p for p in SKILLS_DIR.iterdir() if (p / "SKILL.md").exists())


def _frontmatter(text: str) -> dict:
    """The leading --- ... --- block, parsed just enough. Avoids a yaml dependency:
    only `name` and `description` matter here and both are simple scalars."""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if not m:
        return {}
    out, key = {}, None
    for line in m.group(1).splitlines():
        km = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if km:
            key = km.group(1)
            out[key] = km.group(2).strip().lstrip(">|").strip()
        elif key and line.strip():
            out[key] = (out[key] + " " + line.strip()).strip()
    return out


def test_nothing_was_lost_in_the_split():
    """Every claim in the frozen baseline is still reachable from CLAUDE.md or a skill.

    This is the whole guarantee of the migration. If it fails, a rule was deleted or
    quietly reworded — put it back verbatim rather than updating the baseline, which
    would launder the loss into the new normal."""
    sc = _load_split_check()
    if not sc.BASELINE.exists():
        pytest.skip("no frozen baseline")
    want, missing = sc.missing_claims()
    assert not missing, (
        f"{len(missing)} of {len(want)} notes are no longer reachable from CLAUDE.md or "
        f"any skill. First few:\n  " + "\n  ".join(c[:100] for c in missing[:5])
    )


def test_every_skill_is_pointed_at_by_claude_md():
    """A skill CLAUDE.md never names is one that only fires if its description happens
    to match — which is exactly the silent-drift risk the split introduced. The index
    makes the trigger explicit as well."""
    skills = _skill_dirs()
    if not skills:
        pytest.skip("no skills yet")
    md = CLAUDE_MD.read_text(encoding="utf-8")
    unpointed = [p.name for p in skills if p.name not in md]
    assert not unpointed, (
        "these skills exist but CLAUDE.md never names them, so nothing tells a session "
        f"they are there: {unpointed}. Add a line under '## Where the rest lives'."
    )


def test_every_pointer_resolves_to_a_real_skill():
    """The reverse of the above: a renamed or deleted skill must not leave CLAUDE.md
    pointing at nothing."""
    md = CLAUDE_MD.read_text(encoding="utf-8")
    m = re.search(r"^## Where the rest lives\s*$(.*?)(?=^## |\Z)", md, re.S | re.M)
    if not m:
        pytest.skip("no '## Where the rest lives' index yet")
    named = set(re.findall(r"`([a-z][a-z0-9-]+)`\s+skill", m.group(1)))
    have = {p.name for p in _skill_dirs()}
    dangling = sorted(named - have)
    assert not dangling, f"CLAUDE.md points at skills that do not exist: {dangling}"


@pytest.mark.parametrize("skill_dir", _skill_dirs(), ids=lambda p: p.name)
def test_skill_frontmatter_is_loadable(skill_dir):
    """No name or no description means the skill cannot be selected — it is dead weight
    on disk that reads, from the index, as though it were covering its topic."""
    fm = _frontmatter((skill_dir / "SKILL.md").read_text(encoding="utf-8"))
    assert fm.get("name"), f"{skill_dir.name}/SKILL.md has no `name:` in its frontmatter"
    assert fm.get("description"), f"{skill_dir.name}/SKILL.md has no `description:`"
    assert fm["name"] == skill_dir.name, (
        f"frontmatter name {fm['name']!r} != directory {skill_dir.name!r}; they must "
        f"match or the skill is referred to by two different names"
    )
    assert len(fm["description"]) >= 40, (
        f"{skill_dir.name}: the description is the TRIGGER — a terse one will not fire "
        f"on the words someone actually uses. Name the task and the phrasing."
    )
