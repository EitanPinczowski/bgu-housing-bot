"""notifier recipient routing — listings to the group, ops/digest to the DM.
Telegram group ids are negative, DMs positive; routing is by sign."""
import notifier


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
