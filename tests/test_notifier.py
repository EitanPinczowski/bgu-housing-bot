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


def test_alert_keyboard_has_why_and_contacted():
    res = PipelineResult(status=Status.MATCH, dedup_key="k1",
                         extract=ListingExtract(is_apartment_ad=True))
    kb = notifier._alert_keyboard(res)
    data = [b["callback_data"] for row in kb["inline_keyboard"] for b in row if "callback_data" in b]
    assert {"save|k1", "dismiss|k1", "why|k1", "contacted|k1"} <= set(data)


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
