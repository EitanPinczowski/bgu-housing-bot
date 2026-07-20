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


def test_comment_anchor_is_not_taken_as_permalink():
    only_comment = _Anchor(href="/groups/1/posts/2/?comment_id=9", text="Reply")
    assert scraper._permalink_and_age(_Story([only_comment])) == (None, None)


def test_post_age_hours_delegates():
    ts = _Anchor(href="/groups/1/posts/2/", text="3d")
    assert scraper._post_age_hours(_Story([ts])) == 72.0   # 3 * 24
