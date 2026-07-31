"""
Push the dashboard snapshot to an always-on public URL (GitHub Pages).

    python dashboard.py --share --publish     # build + publish
    python publish.py                         # publish the current snapshot

WHY A SNAPSHOT AND NOT THE LIVE PAGE
------------------------------------
The scraper needs this PC (a real logged-in Chrome profile on the home IP) and OSRM
runs in local Docker, so nothing hosted can be live. What CAN be hosted is the
self-contained file `dashboard.build_share()` already produces: ~950 KB, no external
requests, every write control removed, and a dated banner so a stale copy is never
mistaken for current. Tailscale and every tunnel still need the machine awake; this
does not, which is the whole point.

WHY A SEPARATE REPO
-------------------
The code repo is public. Committing the snapshot there would write ~350 landlords'
phone numbers into public git history permanently — deleting the file later would not
remove them. So this refuses to publish into a checkout whose remote looks like the
code repo, and `_assert_not_code_repo` is the guard.

WHY --amend AND --force
-----------------------
Each push replaces a ~950 KB file. Keeping the history would add ~1 MB per push,
roughly 365 MB a year, for revisions of a page nobody wants to read twice. The site
repo is therefore kept at exactly ONE commit.

The page is deliberately public and unauthenticated (the user's decision, 2026-07-31):
anyone with the link can read every listing, contact and address. Set
PUBLISH_NOINDEX=1 to at least keep it out of search results.
"""
from __future__ import annotations
import os
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

import config

load_dotenv(config.ROOT / ".env")

_NOINDEX_META = '<meta name="robots" content="noindex,nofollow">'


def site_repo_url() -> str:
    """Clone URL of the DEDICATED site repo. Read at call time, not import time:
    .env is loaded per entry point, so a module-level getenv can run first and see
    nothing (the same reason notifier reads its token lazily)."""
    return (os.getenv("SITE_REPO_URL") or "").strip()


def noindex() -> bool:
    """The user chose a plain public URL (2026-07-31). Set PUBLISH_NOINDEX=1 to keep
    it out of search results; access is unchanged either way."""
    return (os.getenv("PUBLISH_NOINDEX") or "").lower() in ("1", "true", "yes")


def _git(args, cwd, check=True):
    """Run git and return its stdout. Kept behind one function so the tests can stub
    every git call at a single point and never touch the network."""
    out = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    if check and out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return (out.stdout or "").strip()


def _repo_name(url: str) -> str:
    return url.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1].lower()


def _assert_not_code_repo(site_url: str, code_url: str) -> None:
    """Refuse to publish into this project's own repo.

    A public git history holding the contacts of ~350 strangers cannot be undone by
    deleting the file, so this is a hard stop rather than a warning."""
    if not code_url:
        return
    if _repo_name(site_url) == _repo_name(code_url):
        raise SystemExit(
            "refusing to publish into the CODE repo — the snapshot contains phone "
            "numbers and addresses, and public git history is permanent.\n"
            "Create a separate repo for the site and put its URL in SITE_REPO_URL.")


def _ensure_checkout(url: str, path: Path) -> None:
    if (path / ".git").exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.rmtree(path)
    print(f"cloning {url} -> {path}")
    _git(["clone", "--depth", "1", url, str(path)], cwd=path.parent)
    _inherit_identity(path)


def _inherit_identity(path: Path) -> None:
    """Give the fresh clone the same git identity as this repo.

    Found the hard way: user.email is set LOCALLY in the code repo and not globally,
    so a brand-new clone cannot commit at all ("unable to auto-detect email address")
    and the very first scheduled publish would fail. Copy whatever the code repo uses;
    if that is unset too, git's own global config still applies."""
    for field in ("user.name", "user.email"):
        value = _git(["config", "--get", field], cwd=config.ROOT, check=False)
        if value:
            _git(["config", field, value], cwd=path)


def _latest_snapshot() -> Path | None:
    """The newest data/dashboard-YYYY-MM-DD.html, or None."""
    files = sorted(config.DATA_DIR.glob("dashboard-*.html"))
    return files[-1] if files else None


def publish(snapshot: Path | None = None) -> int:
    """Copy the snapshot to the site repo as index.html and force-push one commit.

    Returns a process exit code. Unconfigured is not an error — it prints one line
    and succeeds, the same way the optional Sheets sink stays quiet."""
    url = site_repo_url()
    if not url:
        print("SITE_REPO_URL not set — skipping publish "
              "(create a public repo for the site and put its clone URL in .env)")
        return 0

    snapshot = Path(snapshot) if snapshot else _latest_snapshot()
    if not snapshot or not snapshot.exists():
        print("no snapshot to publish — run `python dashboard.py --share` first")
        return 1

    site = Path(config.SITE_DIR)
    _ensure_checkout(url, site)
    _assert_not_code_repo(url,
                          _git(["remote", "get-url", "origin"],
                               cwd=config.ROOT, check=False))

    page = snapshot.read_text(encoding="utf-8")
    if noindex():
        page = page.replace("<meta charset=\"utf-8\">",
                            "<meta charset=\"utf-8\">" + _NOINDEX_META, 1)
        (site / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    elif (site / "robots.txt").exists():
        (site / "robots.txt").unlink()          # the switch works in both directions

    (site / "index.html").write_text(page, encoding="utf-8")
    # .nojekyll: GitHub Pages otherwise runs Jekyll, which ignores files starting
    # with an underscore and can mangle a hand-written page.
    (site / ".nojekyll").write_text("", encoding="utf-8")

    _git(["add", "-A"], cwd=site)
    if not _git(["status", "--porcelain"], cwd=site):
        print("published page is already up to date")
        return 0

    # ONE commit, always amended: the history of a 950 KB generated file is worth
    # nothing and would add ~1 MB per push.
    has_commit = bool(_git(["rev-parse", "--verify", "HEAD"], cwd=site, check=False))
    msg = f"dashboard snapshot {snapshot.stem.removeprefix('dashboard-')}"
    commit = ["commit", "-m", msg] + (["--amend"] if has_commit else [])
    _git(commit, cwd=site)
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=site, check=False) or "main"
    _git(["push", "--force", "origin", branch], cwd=site)

    name = _repo_name(url)
    owner = url.rstrip("/").removesuffix(".git").rsplit("/", 2)[-2]
    print(f"published {snapshot.name} ({len(page) // 1024} KB)")
    print(f"  https://{owner}.github.io/{name}/")
    if not noindex():
        print("  public and indexable — set PUBLISH_NOINDEX=1 to keep it out of search")
    return 0


if __name__ == "__main__":
    sys.exit(publish())
