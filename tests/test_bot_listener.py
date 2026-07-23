"""bot_listener DM command routing + the auto-pin callback. Network (_reply/_api) and
heavy data calls are mocked; asserts the command dispatches to the right handler."""
import bot_listener
import notifier


def _msg(text, chat_id="111", typ="private"):
    return {"chat": {"id": chat_id, "type": typ}, "text": text}


def _setup(monkeypatch):
    monkeypatch.setattr(notifier, "_recipients", lambda target: ["111"])   # owner = 111
    sent = []
    monkeypatch.setattr(bot_listener, "_reply", lambda cid, text: sent.append(text))
    monkeypatch.setattr(bot_listener, "_reply_kb", lambda cid, text, kb: sent.append(text))
    return sent


def test_non_owner_and_group_ignored(monkeypatch):
    sent = _setup(monkeypatch)
    bot_listener._handle_message(_msg("/top", chat_id="999"))   # not the owner
    bot_listener._handle_message(_msg("/top", typ="group"))     # a group, not a DM
    assert sent == []


def test_unknown_command_shows_help(monkeypatch):
    sent = _setup(monkeypatch)
    bot_listener._handle_message(_msg("/wat"))
    assert sent and "פקודות" in sent[0]


def test_top_routes(monkeypatch):
    sent = _setup(monkeypatch)
    monkeypatch.setattr(bot_listener.query, "search", lambda q, limit=10: [])
    monkeypatch.setattr(bot_listener.storage, "contacted_keys", lambda: set())
    bot_listener._handle_message(_msg("/top 3"))
    assert sent and "הכי טובות" in sent[0]


def test_classify_routes(monkeypatch):
    from models import ListingExtract, PipelineResult, Status
    sent = _setup(monkeypatch)
    res = PipelineResult(status=Status.MATCH, location_tier="GREEN", score=88,
                         extract=ListingExtract(is_apartment_ad=True,
                                                street_address_or_neighborhood="רגר 153",
                                                price_per_room_ils=1500, available_rooms_count=2))
    monkeypatch.setattr(bot_listener.pipeline, "process_post", lambda t, commit=False: res)
    bot_listener._handle_message(_msg("/classify דירה מהממת ברגר 153"))
    assert sent and "התאמה" in sent[0] and "88" in sent[0]


def test_pin_command(monkeypatch):
    sent = _setup(monkeypatch)
    pins = {}
    monkeypatch.setattr(bot_listener.geocode, "add_pin",
                        lambda n, la, lo: pins.setdefault(n, (la, lo)) or n)
    bot_listener._handle_message(_msg("/pin כיכר האבות 31.2618,34.7947"))
    assert "כיכר האבות" in pins and sent and "נקבע" in sent[0]


def test_sheet_command(monkeypatch):
    sent = _setup(monkeypatch)
    monkeypatch.setenv("GOOGLE_SHEET_ID", "ABC123")
    bot_listener._handle_message(_msg("/sheet"))
    assert "ABC123" in sent[0]


def test_why_callback(monkeypatch):
    got = {}
    monkeypatch.setattr(bot_listener, "_api", lambda m, **k: {})
    monkeypatch.setattr(bot_listener, "_reply", lambda cid, text: got.update(reply=text))
    monkeypatch.setattr(bot_listener, "_why_text", lambda key: f"ℹ️ {key}")
    bot_listener._handle({"id": "cb", "data": "why|k1", "message": {"chat": {"id": "111"}}})
    assert got["reply"] == "ℹ️ k1"


def test_contacted_callback(monkeypatch):
    got = {}
    monkeypatch.setattr(bot_listener, "_api", lambda m, **k: {})
    monkeypatch.setattr(bot_listener.storage, "set_contacted", lambda key: got.update(k=key))
    bot_listener._handle({"id": "cb", "data": "contacted|k9"})
    assert got["k"] == "k9"


def test_autopin_callback(monkeypatch):
    calls = {}
    monkeypatch.setattr(bot_listener, "_api", lambda m, **k: calls.update(k) or {})
    monkeypatch.setattr(bot_listener.geocode, "add_pin",
                        lambda n, la, lo: calls.setdefault("pinned", (n, la, lo)) or n)
    bot_listener._pending_pins["0"] = ("רחוב סיני", 31.25, 34.80)
    bot_listener._handle({"id": "cb1", "data": "pin|0"})
    assert calls.get("pinned") == ("רחוב סיני", 31.25, 34.80)
