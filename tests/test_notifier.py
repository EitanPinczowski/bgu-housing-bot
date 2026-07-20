"""notifier recipient routing — listings to the group, ops/digest to the DM.
Telegram group ids are negative, DMs positive; routing is by sign."""
import notifier
from models import PipelineResult, Status


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
