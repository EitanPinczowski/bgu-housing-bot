"""config.validate: passes on the real config, and fails fast on a broken value."""
import config


def test_validate_passes_on_real_config():
    config.validate()          # the shipped config must be valid


def test_validate_catches_bad_price(monkeypatch):
    monkeypatch.setattr(config, "TARGET_PRICE_PER_ROOM_ILS", config.MAX_PRICE_PER_ROOM_ILS + 500)
    try:
        config.validate()
    except SystemExit as e:
        assert "TARGET_PRICE" in str(e)
    else:
        raise AssertionError("expected SystemExit on TARGET > MAX price")


def test_validate_catches_bad_viewbox(monkeypatch):
    monkeypatch.setattr(config, "BEER_SHEVA_VIEWBOX", "34.7,31.3,oops")
    try:
        config.validate()
    except SystemExit as e:
        assert "VIEWBOX" in str(e)
    else:
        raise AssertionError("expected SystemExit on a bad viewbox")
