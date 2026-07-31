"""publish.py — pushing the snapshot to an always-on public URL.

Every git call goes through publish._git, so these stub that one function and never
touch the network or a real repository.

The test that matters most is the code-repo guard: the code repo is public, and a
snapshot committed there would put ~350 landlords' phone numbers into public git
history permanently, where deleting the file would not remove them.
"""
import pytest

import config
import publish


@pytest.fixture
def site(tmp_path, monkeypatch):
    """A configured, already-cloned site repo with the git calls recorded."""
    d = tmp_path / "site"
    (d / ".git").mkdir(parents=True)
    monkeypatch.setattr(config, "SITE_DIR", d)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setenv("SITE_REPO_URL", "https://github.com/me/bgu-housing-dashboard.git")
    monkeypatch.delenv("PUBLISH_NOINDEX", raising=False)
    calls = []

    def fake_git(args, cwd, check=True):
        calls.append(list(args))
        if args[0] == "remote":
            return "https://github.com/EitanPinczowski/bgu-housing-bot.git"
        if args[0] == "status":
            return " M index.html"                  # always something to publish
        if args[:2] == ["rev-parse", "--verify"]:
            return "abc1234"                        # a commit already exists
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return "main"
        return ""

    monkeypatch.setattr(publish, "_git", fake_git)
    return d, calls, tmp_path


def _snapshot(dirpath, body="<html><meta charset=\"utf-8\">hi</html>"):
    f = dirpath / "dashboard-2026-07-31.html"
    f.write_text(body, encoding="utf-8")
    return f


def test_unconfigured_is_a_no_op_not_a_failure(tmp_path, monkeypatch):
    """Same contract as the optional Sheets sink: silent until it's set up, so the
    scheduled task doesn't start reporting failures for a feature nobody enabled."""
    monkeypatch.delenv("SITE_REPO_URL", raising=False)
    assert publish.publish() == 0


def test_it_refuses_to_publish_into_the_code_repo(site, monkeypatch):
    """The one mistake that cannot be undone. Deleting the file later would not
    remove the phone numbers from a public git history."""
    d, _calls, data = site
    _snapshot(data)
    monkeypatch.setenv("SITE_REPO_URL",
                       "https://github.com/EitanPinczowski/bgu-housing-bot.git")
    with pytest.raises(SystemExit) as exc:
        publish.publish()
    assert "refusing to publish into the CODE repo" in str(exc.value)


def test_it_writes_the_snapshot_as_index_html(site):
    d, calls, data = site
    snap = _snapshot(data, "<html><meta charset=\"utf-8\">SNAPSHOT BODY</html>")
    assert publish.publish(snap) == 0
    assert (d / "index.html").read_text(encoding="utf-8") == snap.read_text(encoding="utf-8")
    assert (d / ".nojekyll").exists()          # or Jekyll mangles a hand-written page
    assert ["add", "-A"] in calls and any(c[0] == "push" for c in calls)


def test_history_stays_at_one_commit(site):
    """A ~950 KB generated file pushed several times a day would add ~365 MB a year
    of history nobody will ever read."""
    d, calls, data = site
    publish.publish(_snapshot(data))
    commit = next(c for c in calls if c[0] == "commit")
    assert "--amend" in commit
    push = next(c for c in calls if c[0] == "push")
    assert "--force" in push and push[-1] == "main"


def test_the_first_publish_creates_a_commit_rather_than_amending(site, monkeypatch):
    d, calls, data = site
    real = publish._git

    def no_head(args, cwd, check=True):
        if args[:2] == ["rev-parse", "--verify"]:
            return ""                          # empty repo: nothing to amend
        return real(args, cwd, check)

    monkeypatch.setattr(publish, "_git", no_head)
    publish.publish(_snapshot(data))
    commit = next(c for c in calls if c[0] == "commit")
    assert "--amend" not in commit


def test_noindex_is_off_by_default_and_reversible(site, monkeypatch):
    """The user chose a plain public URL. The switch exists, defaults off, and
    removes robots.txt again when turned back off."""
    d, _calls, data = site
    snap = _snapshot(data)
    publish.publish(snap)
    assert not (d / "robots.txt").exists()
    assert "noindex" not in (d / "index.html").read_text(encoding="utf-8")

    monkeypatch.setenv("PUBLISH_NOINDEX", "1")
    publish.publish(snap)
    assert (d / "robots.txt").exists()
    assert "noindex,nofollow" in (d / "index.html").read_text(encoding="utf-8")

    monkeypatch.setenv("PUBLISH_NOINDEX", "0")
    publish.publish(snap)
    assert not (d / "robots.txt").exists()


def test_missing_snapshot_is_reported_not_guessed(site):
    d, _calls, data = site
    assert publish.publish() == 1              # nothing built yet


def test_it_publishes_the_newest_snapshot_when_not_told_which(site):
    d, _calls, data = site
    (data / "dashboard-2026-07-29.html").write_text("old", encoding="utf-8")
    _snapshot(data, "NEWEST")
    publish.publish()
    assert (d / "index.html").read_text(encoding="utf-8") == "NEWEST"
