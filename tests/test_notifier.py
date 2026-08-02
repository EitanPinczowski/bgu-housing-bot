"""notifier recipient routing — listings to the group, ops/digest to the DM.
Telegram group ids are negative, DMs positive; routing is by sign."""
import notifier
from models import ListingExtract, PipelineResult, Status


def test_routing_splits_dm_and_group(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111, -222")
    assert notifier._recipients("primary") == ["111"]
    assert notifier._recipients("group") == ["-222"]
    assert set(notifier._recipients("all")) == {"111", "-222"}


def test_routing_falls_back_when_role_missing(monkeypatch):
    # only a DM configured -> 'group' must not send nowhere; it falls back to all
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    assert notifier._recipients("group") == ["111"]
    assert notifier._recipients("primary") == ["111"]


def test_no_ids(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    assert notifier._recipients("group") == []


def _callback_data(kb):
    return [b["callback_data"] for row in kb["inline_keyboard"]
            for b in row if "callback_data" in b]


def test_alert_keyboard_has_why_and_contacted(temp_db):
    import storage
    res = PipelineResult(status=Status.MATCH, dedup_key="k1",
                         extract=ListingExtract(is_apartment_ad=True))
    tok = storage.callback_token("k1")
    assert set(_callback_data(notifier._alert_keyboard(res))) >= {
        f"save|{tok}", f"dismiss|{tok}", f"why|{tok}", f"contacted|{tok}"}


def test_a_hebrew_address_still_fits_in_a_telegram_button(temp_db):
    """The bug that cost 12 of 16 alerts on 2026-08-02.

    A dedup_key is `phone|address` and Hebrew is 2 bytes a character, so a descriptive
    address blew past Telegram's 64-BYTE callback_data cap. Telegram answers
    BUTTON_DATA_INVALID and discards the WHOLE MESSAGE — and alerts are batched, so one
    long address took the whole batch down with it."""
    key = "phone:508220245|רגר 93, גבול בין שכונה ב ל-שכונה ד, הבלוק"
    assert len(f"dismiss|{key}".encode()) > notifier.CALLBACK_DATA_MAX_BYTES, \
        "this key must be one that used to fail, or the test proves nothing"
    res = PipelineResult(status=Status.MATCH, dedup_key=key,
                         extract=ListingExtract(is_apartment_ad=True))
    data = _callback_data(notifier._alert_keyboard(res))
    assert len(data) == 4, "all four vote buttons must survive"
    for d in data:
        assert len(d.encode()) <= notifier.CALLBACK_DATA_MAX_BYTES


def test_an_oversized_button_costs_the_button_not_the_alert():
    """Belt and braces behind the tokens: if callback_data is ever too long again, drop
    that button so the alert still sends. Losing a button is a bad day; losing the alert
    is the product failing."""
    rows = [[{"text": "ok", "callback_data": "save|short"},
             {"text": "huge", "callback_data": "save|" + "א" * 40}],
            [{"text": "url only", "url": "https://example.com"}]]
    out = notifier._fit_callbacks(rows)
    assert [b["text"] for row in out for b in row] == ["ok", "url only"]


def test_a_token_is_stable_and_reversible(temp_db):
    """Deterministic, so re-alerting the same flat reuses its token instead of growing a
    row every run, and so a button posted last week still resolves today."""
    import storage
    key = "phone:1|רגר 5"
    first = storage.callback_token(key)
    assert storage.callback_token(key) == first
    assert storage.key_for_token(first) == key
    assert storage.key_for_token("nosuchtoken") is None
    assert storage.callback_token("phone:2|רגר 5") != first


def _match(**kw):
    return PipelineResult(status=Status.MATCH, dedup_key="k1", location_tier="GREEN",
                          extract=ListingExtract(is_apartment_ad=True), **kw)


def test_amenity_line_is_rendered_and_escaped():
    res = _match(amenities={"bus669": {
        "label": "669 מרגר", "icon": "🚌", "kind": "bus_route",
        "options": [{"minutes": 6.0, "headway_min": 20, "direction_id": "0"},
                    {"minutes": 8.0, "headway_min": 30, "direction_id": "1"}]}})
    body = notifier.format_alert(res)
    assert "🚌 669 מרגר" in body
    assert "↔" in body                                # both directions on one line
    assert "\\(כל \\~20 דק׳\\)" in body                # MarkdownV2-escaped, not raw


def test_no_amenities_prints_no_line():
    body = notifier.format_alert(_match())
    assert "🚌" not in body and "🏋️" not in body       # silence, not "unknown"


def test_send_batch_ranks_and_caps(monkeypatch):
    sent = []
    monkeypatch.setattr(notifier, "_send_alert",
                        lambda res, target="group": sent.append(res.score))
    headers = []
    monkeypatch.setattr(notifier, "send",
                        lambda text, reply_markup=None, target="all": headers.append(text) or True)

    def mk(score, status=Status.MATCH):
        return PipelineResult(status=status, score=score)

    results = [mk(75), mk(95), mk(60), mk(88), mk(None), mk(50, Status.DROP)]
    n = notifier.send_batch(results, top_k=2)
    assert n == 2                       # capped at top_k
    assert sent == [95, 88]             # ranked by score, descending
    assert len(headers) == 1            # one header, not one-per-match


def test_send_batch_empty_sends_nothing(monkeypatch):
    monkeypatch.setattr(notifier, "_send_alert", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(notifier, "send", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    assert notifier.send_batch([PipelineResult(status=Status.DROP, score=90)], top_k=5) == 0


def test_unesc_strips_markdownv2_escapes():
    assert notifier._unesc(notifier._esc("050-1234567 (מרפסת).")) == "050-1234567 (מרפסת)."
    assert notifier._plain_payload({"text": notifier._esc("a-b."), "parse_mode": "MarkdownV2"}) \
        == {"text": "a-b."}


def test_plain_text_fallback_on_400(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    seen = []

    class _R:
        def __init__(self, code):
            self.status_code = code

        def raise_for_status(self):
            if self.status_code != 200:
                import requests as rq
                e = rq.exceptions.HTTPError("bad")
                e.response = self
                raise e

        def json(self):
            return {"ok": True, "result": {}}

    def fake_post(url, json=None, timeout=None):
        seen.append(json)
        # first attempt (MarkdownV2) 400s; the plain-text resend (no parse_mode) succeeds
        return _R(400 if json.get("parse_mode") else 200)

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    ok = notifier._post_to_all("sendMessage", {"text": notifier._esc("bad-text."),
                                               "parse_mode": "MarkdownV2"}, 15, target="primary")
    assert ok == {"ok": True, "result": {}}      # the alert still went out
    assert len(seen) == 2                          # formatted attempt, then plain retry
    assert "parse_mode" not in seen[1] and seen[1]["text"] == "bad-text."  # de-escaped plain text


# --- sending the dashboard snapshot ------------------------------------------------
def test_send_document_posts_multipart_to_the_group(monkeypatch, tmp_path):
    """_post_to_all sends json=, which cannot carry a file; sendDocument needs
    multipart. And the file lists phone numbers, so it defaults to the group — the
    same place the alerts already go — never to whatever chat id is lying around."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100123,55501")   # a group and a DM
    f = tmp_path / "dashboard-2026-07-30.html"
    f.write_text("<html>hi</html>", encoding="utf-8")
    calls = []

    class _Resp:
        def raise_for_status(self):
            pass

    def fake_post(url, data=None, files=None, timeout=None, **kw):
        calls.append({"url": url, "data": data, "files": files})
        return _Resp()

    monkeypatch.setattr(notifier.requests, "post", fake_post)
    assert notifier.send_document(f, caption="צילום מצב") is True
    assert len(calls) == 1                                    # the group only
    assert calls[0]["url"].endswith("/sendDocument")
    assert calls[0]["data"]["chat_id"] == "-100123"
    assert calls[0]["files"]["document"][0] == f.name
    assert calls[0]["data"]["caption"] == "צילום מצב"


def test_send_document_returns_false_without_a_token(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    f = tmp_path / "d.html"
    f.write_text("x", encoding="utf-8")
    assert notifier.send_document(f) is False                 # no raise


def test_send_document_returns_false_for_a_missing_file(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100123")
    assert notifier.send_document(tmp_path / "never-built.html") is False
