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
    `tooltip` is the date text FB pops on hover (read via evaluate).

    `evaluate` returns a LIST because the page script is a `querySelectorAll(...).map(...)`
    collecting EVERY tooltip in the document, not just the first. It used to return a bare
    string, matching a `querySelector` that read only the first node — and a double that
    lags the code it doubles is part of how the stale-tooltip bug stayed invisible. See
    `test_a_stale_tooltip_does_not_swallow_the_date`."""
    def __init__(self, href_after_hover, tooltip=""):
        self._href = href_after_hover
        self._tip = tooltip
        self.hovered = False

    def hover(self, timeout=None):
        self.hovered = True

    def get_attribute(self, name):
        return self._href if name == "href" else ""

    def evaluate(self, _script):
        return [self._tip] if self._tip else []


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
    monkeypatch.setattr(scraper, "_permalink_and_age",
                        lambda s, g=None, allow_hover=True: (None, 2.0))  # fresh
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


# --- comment capture (locale) ----------------------------------------------------
class _CmtArt:
    def __init__(self, text, label=None):
        self._t, self._l = text, label

    def get_attribute(self, name):
        return self._l if name == "aria-label" else None

    def inner_text(self):
        return self._t


class _CmtStory(_CmtArt):
    def __init__(self, text, children):
        super().__init__(text, None)
        self._kids = children

    def query_selector_all(self, sel):
        return [self] + self._kids          # the story is its own [role=article]


def test_hebrew_comment_labels_are_read():
    """The bug: the browser runs locale='he-IL' but only an English 'Comment by …'
    aria-label was accepted, so comment coverage sat at 8% of posts — and comments
    are where Israeli housing ads put the price."""
    import scraper
    story = _CmtStory("גוף הפוסט", [
        _CmtArt("דנה כהן\n1500 לחודש", label="תגובה מאת דנה כהן"),
        _CmtArt("יוסי\nעדיין פנוי?", label="תגובה מאת יוסי"),
    ])
    got = scraper._comments(story)
    assert "1500 לחודש" in got and "עדיין פנוי?" in got
    assert "גוף הפוסט" not in got            # the post body is not a comment


def test_english_labels_still_work():
    import scraper
    story = _CmtStory("body", [_CmtArt("Dana\n1500", label="Comment by Dana")])
    assert "1500" in scraper._comments(story)


def test_unlabelled_articles_fall_back_to_position():
    """A future Facebook relabel must not silently zero coverage again — anything
    nested that isn't the story itself counts."""
    import scraper
    story = _CmtStory("body", [_CmtArt("דנה\n1500 שח", label=None)])
    got = scraper._comments(story)
    assert "1500 שח" in got and "body" not in got


def test_ui_chrome_is_filtered_in_both_languages():
    import scraper
    story = _CmtStory("body", [
        _CmtArt("אהבתי\nהגב\nשיתוף\n1500 שח", label="תגובה מאת דנה"),
        _CmtArt("Like\nReply\n1400", label="Comment by Dan")])
    got = scraper._comments(story)
    assert "1500 שח" in got and "1400" in got
    for junk in ("אהבתי", "הגב", "שיתוף", "Like", "Reply"):
        assert junk not in got


def test_a_story_with_no_comments_yields_nothing():
    import scraper
    assert scraper._comments(_CmtStory("body", [])) == ""


# --- one story per record: the block must not run on into the next post ------------

def test_clean_story_stops_at_the_next_post():
    """The case that surfaced this: a couple's "looking for a flat" post was followed
    in one scraped block by a stranger's OFFER, so the LLM read both, extracted the
    offer, and the listing pointed at the wanted-ad's permalink while showing its text.
    `_TAIL_MARKERS` never fired — the block had no "View more comments"."""
    out = scraper._clean_story("\n".join([
        "Noya Moyal",
        "זוג סטודנטים שנה ב׳, מחפשים דירת 3/3.5 חדרים",
        "שכירות עד 3,300",
        "Avidan Mandelman",
        "1h",
        "למי שמחפש דירה במחיר של פעם, דירה של 95 מטר",
        "מחיר - 2500₪",
        "איתי - 0522629429",
    ]))
    assert "מחפשים דירת" in out                    # the post the permalink belongs to
    assert "Avidan Mandelman" not in out
    assert "2500" not in out and "0522629429" not in out   # the other post's fields


def test_clean_story_also_drops_a_trailing_comment():
    """Same header shape, and the flat itself must survive — the comment's own text is
    captured separately in `comments`."""
    out = scraper._clean_story("\n".join([
        "Noga Erlich",
        "מתפנה דירה במצדה 10!",
        "דירה ל3 שותפים בקומה 4, 1400 ש״ח לשותף",
        "LivelyDeer99901",
        "13h",
        "יקר ברמות",
    ]))
    assert "מצדה 10" in out and "1400" in out
    assert "יקר ברמות" not in out


def test_clean_story_leaves_an_ordinary_single_post_alone():
    body = "\n".join([
        "Shaked Avikzer",
        "מתפנה דירת 4 חדרים ברגר 133!",
        "קומה 2, מחיר 3200₪ לחודש",
        "050-1234567",
    ])
    out = scraper._clean_story(body)
    assert "רגר 133" in out and "3200" in out and "050-1234567" in out


def test_the_cut_never_eats_the_post_it_belongs_to():
    """The post's own author header must not be mistaken for a next-story header —
    cutting at index 0/1 would leave an empty body."""
    out = scraper._clean_story("Noga Erlich\n3h\nמתפנה דירה במצדה 10, 1400 לשותף")
    assert "מצדה 10" in out, out


# --- the hover loop must not stop at the link and leave the age behind -----------------

def test_hovering_continues_until_the_age_is_found_not_just_the_link(monkeypatch):
    """IT USED TO BREAK ON THE LINK ALONE, on a comment claiming the timestamp anchor gives
    link and tooltip together. It does not: `_age_from_aria` returns None for a
    profile-name tooltip or one that has not rendered, and the loop gave up with no age.

    Measured 2026-08-13 on the 14:00 full run: **36 of 101 posts (35%) carried an age while
    the hover budget was only 305/800 used** — so the cap was never the constraint and
    raising it bought nothing. This exit condition is what was losing them."""
    monkeypatch.setattr(scraper.time, "sleep", lambda *a, **k: None)
    scraper._hover_used = 0
    # a real permalink, but a NAME tooltip -> link found, age still None
    first = _HoverAnchor("/groups/1/posts/2/?__cft__=x", tooltip="דנה כהן")
    second = _HoverAnchor("/groups/1/user/9/", tooltip="Tuesday, July 21, 2026 at 12:56 PM")
    link, age = scraper._hover_reveal([first, second], "1")
    assert link == "https://www.facebook.com/groups/1/posts/2/"
    assert age is not None, "stopped at the link and left the age behind"
    assert second.hovered is True, "never tried the anchor carrying the age"


def test_hovering_stops_as_soon_as_both_are_known(monkeypatch):
    """Bounded, or a post whose tooltip never parses would burn every allowed hover. The
    first anchor carrying BOTH must still end the loop."""
    monkeypatch.setattr(scraper.time, "sleep", lambda *a, **k: None)
    scraper._hover_used = 0
    both = _HoverAnchor("/groups/1/posts/2/", tooltip="Tuesday, July 21, 2026 at 12:56 PM")
    extra = _HoverAnchor("/groups/1/posts/3/", tooltip="Tuesday, July 21, 2026 at 1:56 PM")
    link, age = scraper._hover_reveal([both, extra], "1")
    assert link == "https://www.facebook.com/groups/1/posts/2/" and age is not None
    assert extra.hovered is False, "kept hovering after both were known"


# --- the tooltip read must not be answered by a stale node ----------------------------

class _TipsAnchor:
    """An anchor whose hover leaves the document holding SEVERAL [role="tooltip"] nodes —
    what FB actually does, because a tooltip lingers in the DOM while it fades. `tips` is
    the list in document order; `label` is the anchor's own lazily-rendered aria-label."""
    def __init__(self, href="/groups/1/posts/2/", tips=(), label=""):
        self._href, self._tips, self._label = href, list(tips), label
        self.hovered = False

    def hover(self, timeout=None):
        self.hovered = True

    def get_attribute(self, name):
        if name == "href":
            return self._href
        return self._label if name == "aria-label" else ""

    def evaluate(self, script):
        self.script = script          # kept so a test can assert WHAT the page was asked
        return list(self._tips)


def test_a_stale_tooltip_does_not_swallow_the_date(monkeypatch):
    """`document.querySelector('[role="tooltip"]')` returns whichever tooltip comes first
    in DOCUMENT ORDER — not the one this hover popped. A profile-name tooltip parses to
    None, so one lingering node answered every read and no amount of extra hovering could
    help.

    Measured 2026-08-13 on the 16:00 run: **2.8 hovers per post (233 of 800) and 6 ages
    out of 58 posts (10%)**, against 90% that morning. Spend without yield."""
    monkeypatch.setattr(scraper.time, "sleep", lambda *a, **k: None)
    scraper._hover_used = 0
    a = _TipsAnchor(tips=["דנה כהן", "Tuesday, July 21, 2026 at 12:56 PM"])
    link, age = scraper._hover_reveal([a], "1")
    assert link == "https://www.facebook.com/groups/1/posts/2/"
    assert age is not None, "the first tooltip was not a date, so the date was never read"


def test_the_anchors_own_label_is_read_after_the_hover(monkeypatch):
    """FB renders `aria-label` lazily, exactly like the href. It was only ever read BEFORE
    the hover — when FB has not filled it in — so a post whose date lives there and not in
    a tooltip lost its age."""
    monkeypatch.setattr(scraper.time, "sleep", lambda *a, **k: None)
    scraper._hover_used = 0
    a = _TipsAnchor(tips=[], label="Tuesday, July 21, 2026 at 12:56 PM")
    _, age = scraper._hover_reveal([a], "1")
    assert age is not None, "the date on the anchor's own label was ignored"


def test_age_sources_records_what_the_hovering_yielded(monkeypatch):
    """The tally that would have settled this in one run instead of three: `hovers=N/CAP`
    reports SPEND, and nothing reported YIELD."""
    scraper._age_sources.update(page=0, hover=0, none=0)
    monkeypatch.setattr(scraper.config, "SCRAPER_HOVER_FOR_LINK", False)
    scraper._permalink_and_age(_Story([_Anchor(href="/groups/1/posts/2/", text="5h")]))
    scraper._permalink_and_age(_Story([_Anchor(href="/groups/1/posts/3/", text="")]))
    assert scraper.age_sources() == {"page": 1, "hover": 0, "none": 1}
    # A RE-READ MUST NOT COUNT. The same story is passed again on every scroll pass, with
    # allow_hover=False after the first — counting those buried the signal under reads
    # that could never produce an age: the 18:00 run on 2026-08-13 reported 67/231 that
    # way while its archived rows were 27 of 40.
    scraper._permalink_and_age(_Story([_Anchor(href="/groups/1/posts/3/", text="")]),
                               allow_hover=False)
    assert scraper.age_sources() == {"page": 1, "hover": 0, "none": 1}


def test_the_page_is_asked_for_every_tooltip_not_just_the_first(monkeypatch):
    """The one part of this fix a test double CANNOT exercise: the double returns whatever
    list it was built with, no matter what script it is handed, so reverting
    `querySelectorAll` to `querySelector` leaves every other test in this file green —
    verified by reverting it.

    So assert the request itself. This is weaker than running the JS, and it is the
    strongest check available without a real browser: it fails if someone narrows the
    query back to a single node, which is the exact regression that cost 90% of the age
    capture on 2026-08-13."""
    monkeypatch.setattr(scraper.time, "sleep", lambda *a, **k: None)
    scraper._hover_used = 0
    a = _TipsAnchor(tips=["Tuesday, July 21, 2026 at 12:56 PM"])
    scraper._hover_reveal([a], "1")
    assert "querySelectorAll" in a.script, "the page was asked for one tooltip, not all"
    assert "role=" in a.script and "tooltip" in a.script

