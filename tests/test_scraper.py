"""_permalink_and_age: read the post's permalink + age from its timestamp anchor.
Stub-based (no browser), mirroring the FakePage pattern used for block detection."""
import scraper


class _Anchor:
    def __init__(self, href="", text="", aria=""):
        self._href, self._text, self._aria = href, text, aria

    def get_attribute(self, name):
        return {"href": self._href, "aria-label": self._aria}.get(name, "")

    def inner_text(self):
        return self._text


class _Story:
    def __init__(self, anchors, role_link=None):
        self._all = anchors
        self._role = anchors if role_link is None else role_link

    def query_selector_all(self, sel):
        return self._role if 'role="link"' in sel else self._all


def test_link_and_age_from_timestamp_anchor():
    ts = _Anchor(href="/groups/1/posts/2/?__cft__=x&comment=no", text="5h")
    profile = _Anchor(href="/user/abc", text="דנה כהן")
    comment = _Anchor(href="/groups/1/posts/2/?comment_id=9", text="Reply")
    link, age = scraper._permalink_and_age(_Story([profile, comment, ts]))
    assert link == "https://www.facebook.com/groups/1/posts/2/"   # timestamp href, cleaned
    assert age == 5.0


def test_no_link_when_only_profile_anchor():
    story = _Story([_Anchor(href="/user/abc", text="דנה כהן")])
    assert scraper._permalink_and_age(story) == (None, None)


def test_falls_back_to_hint_anchor_when_no_timestamp():
    # a /posts/ link with no readable timestamp -> use it as the fallback, age None
    plink = _Anchor(href="/groups/1/posts/2/", text="")
    link, age = scraper._permalink_and_age(_Story([plink]))
    assert link == "https://www.facebook.com/groups/1/posts/2/"
    assert age is None


def test_comment_link_reconstructs_permalink():
    # a comment link carries THIS post's id — reconstruct the clean permalink from it
    # (the whole point of the fix), even with no plain permalink anchor on the post.
    ts = _Anchor(href="#", text="5h")                       # timestamp, JS-only href
    comment = _Anchor(href="/groups/1/posts/2/?comment_id=9", text="Reply")
    link, age = scraper._permalink_and_age(_Story([ts, comment]),
                                           "https://www.facebook.com/groups/1")
    assert link == "https://www.facebook.com/groups/1/posts/2/" and age == 5.0


def test_reconstructs_from_story_fbid_query():
    # permalink.php?story_fbid=… — the id is in the query that _clean_href strips,
    # so we must reconstruct rather than use the raw href. gid comes from the URL.
    ts = _Anchor(href="/permalink.php?story_fbid=555&id=1", text="3h")
    link, age = scraper._permalink_and_age(_Story([ts]),
                                           "https://www.facebook.com/groups/1")
    assert link == "https://www.facebook.com/groups/1/posts/555/" and age == 3.0


def test_keeps_stories_link_as_is():
    ts = _Anchor(href="/stories/999/AbC==/?src=x", text="2h")
    link, _ = scraper._permalink_and_age(_Story([ts]), "https://www.facebook.com/groups/1")
    assert link == "https://www.facebook.com/stories/999/AbC==/"


def test_no_link_when_no_post_id_anywhere():
    only_profile = _Anchor(href="/user/abc", text="דנה כהן")
    assert scraper._permalink_and_age(_Story([only_profile]),
                                      "https://www.facebook.com/groups/1") == (None, None)


class _HoverAnchor:
    """A timestamp anchor whose href only appears AFTER a hover (FB's lazy render);
    `tooltip` is the date text FB pops on hover (read via evaluate)."""
    def __init__(self, href_after_hover, tooltip=""):
        self._href = href_after_hover
        self._tip = tooltip
        self.hovered = False

    def hover(self, timeout=None):
        self.hovered = True

    def get_attribute(self, name):
        return self._href if name == "href" else ""

    def evaluate(self, _script):
        return self._tip


def test_hover_reveal_reconstructs_link(monkeypatch):
    monkeypatch.setattr(scraper.time, "sleep", lambda *a, **k: None)
    scraper._hover_used = 0
    a = _HoverAnchor("/groups/1/posts/2/?__cft__=x")     # href appears on hover
    assert scraper._hover_reveal([a], "1")[0] == "https://www.facebook.com/groups/1/posts/2/"
    assert a.hovered is True
    # story_fbid form reconstructs with the group id from the URL
    scraper._hover_used = 0
    assert scraper._hover_reveal([_HoverAnchor("/permalink.php?story_fbid=99&id=1")], "1")[0] \
        == "https://www.facebook.com/groups/1/posts/99/"


def test_hover_reveal_reads_age_from_tooltip(monkeypatch):
    monkeypatch.setattr(scraper.time, "sleep", lambda *a, **k: None)
    scraper._hover_used = 0
    a = _HoverAnchor("/groups/1/posts/2/", tooltip="Tuesday, July 21, 2026 at 12:56 PM")
    link, age = scraper._hover_reveal([a], "1")
    assert link == "https://www.facebook.com/groups/1/posts/2/"
    assert isinstance(age, float)                        # tooltip date parsed to an age


def test_hover_reveal_tries_candidates_until_one_reveals(monkeypatch):
    monkeypatch.setattr(scraper.time, "sleep", lambda *a, **k: None)
    scraper._hover_used = 0
    # first candidate stays a profile (no post id), second reveals the real permalink
    cands = [_HoverAnchor("/groups/1/user/9/"), _HoverAnchor("/groups/1/posts/2/?x=1")]
    assert scraper._hover_reveal(cands, "1")[0] == "https://www.facebook.com/groups/1/posts/2/"


def test_hover_reveal_respects_run_cap(monkeypatch):
    monkeypatch.setattr(scraper.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(scraper.config, "SCRAPER_MAX_HOVERS_PER_RUN", 0)
    scraper._hover_used = 0
    a = _HoverAnchor("/groups/1/posts/2/")
    assert scraper._hover_reveal([a], "1") == (None, None)   # cap reached -> no hover
    assert a.hovered is False


def test_hover_reveal_none_when_still_empty(monkeypatch):
    monkeypatch.setattr(scraper.time, "sleep", lambda *a, **k: None)
    scraper._hover_used = 0
    assert scraper._hover_reveal([_HoverAnchor("#")], "1")[0] is None
    assert scraper._hover_reveal([_HoverAnchor("")], "1")[0] is None


def test_post_age_hours_delegates():
    ts = _Anchor(href="/groups/1/posts/2/", text="3d")
    assert scraper._post_age_hours(_Story([ts])) == 72.0   # 3 * 24


# --- scrape_group early-stop (no browser): a static feed that stops turning up new
# fresh posts must break well before SCROLL_CAP, and skip already-seen posts. ----
class _FakePage:
    def __init__(self):
        self.url = "https://www.facebook.com/groups/1"
        self.mouse = self

    def goto(self, *a, **k): pass
    def wait_for_selector(self, *a, **k): pass
    def wheel(self, *a, **k): pass


class _FakeStory:
    def __init__(self, text): self._t = text
    def inner_text(self): return self._t


def _stub_scraper(monkeypatch, stories):
    """Patch out the browser/DOM helpers; return a pass-counter dict."""
    passes = {"n": 0}
    monkeypatch.setattr(scraper.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(scraper, "_blocked_reason", lambda page: None)
    monkeypatch.setattr(scraper, "_clean_story", lambda raw: raw)
    monkeypatch.setattr(scraper, "_images", lambda s, **k: [])
    monkeypatch.setattr(scraper, "_comments", lambda s: "")
    monkeypatch.setattr(scraper, "_permalink_and_age", lambda s, g=None: (None, 2.0))  # fresh
    monkeypatch.setattr(scraper, "_expand_see_more", lambda page: None)

    def fake_stories(page):
        passes["n"] += 1
        return stories
    monkeypatch.setattr(scraper, "_stories", fake_stories)
    return passes


_POSTS = [_FakeStory(f"דירה להשכרה שלושה שותפים חדר פנוי מיידי בשכונה ג מספר {i}")
          for i in range(3)]


def test_scrape_group_early_stops_on_stale(monkeypatch):
    passes = _stub_scraper(monkeypatch, _POSTS)
    posts, stats = scraper.scrape_group(_FakePage(), "https://www.facebook.com/groups/1")
    assert len(posts) == 3 and stats["read"] == 3
    # 2 warm-up passes + 2 stale passes ≈ 3 passes, far below SCROLL_CAP (25)
    assert passes["n"] <= 4


def test_scrape_group_skips_already_seen(monkeypatch):
    _stub_scraper(monkeypatch, _POSTS)
    seen = lambda text, url: text.endswith("מספר 1")     # one of the three is old news
    posts, stats = scraper.scrape_group(_FakePage(), "https://www.facebook.com/groups/1",
                                        already_seen=seen)
    assert stats["seen_skipped"] == 1
    assert len(posts) == 2
    assert all("מספר 1" not in p["text"] for p in posts)


def test_scrape_group_keeps_thin_text_with_image(monkeypatch):
    _stub_scraper(monkeypatch, [_FakeStory("דירה 📞")])          # ~7 chars = thin
    monkeypatch.setattr(scraper, "_images", lambda s, **k: ["http://img"])   # but has a photo
    posts, stats = scraper.scrape_group(_FakePage(), "https://www.facebook.com/groups/1")
    assert len(posts) == 1 and posts[0]["images"] == ["http://img"]


def test_scrape_group_drops_thin_text_without_image(monkeypatch):
    _stub_scraper(monkeypatch, [_FakeStory("דירה 📞")])          # thin, and _images -> []
    posts, stats = scraper.scrape_group(_FakePage(), "https://www.facebook.com/groups/1")
    assert posts == []
