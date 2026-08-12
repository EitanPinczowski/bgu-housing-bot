"""dm_digest._core_token: pick the distinctive Hebrew word to geocode a messy
location by, and _suggest's cache/format wiring (network stubbed)."""
import dm_digest as d
import geocode
import storage


def test_core_token_drops_generic_and_takes_longest():
    assert d._core_token("רחוב סיני") == "סיני"
    assert d._core_token("רחוב מגידו, שכונה ט") == "מגידו"        # generics + 1-letter dropped
    assert d._core_token("הנרי קנדל 14, שכונת רמב\"ם") == "הנרי"   # longest of הנרי/קנדל (tie→first max)
    assert d._core_token("באר שבע") is None                        # nothing distinctive left
    assert d._core_token("13/6") is None                           # digits/punct only


def test_suggest_caches_and_formats(monkeypatch):
    calls = {"n": 0}

    def fake_post(ep, data=None, timeout=None, headers=None):
        calls["n"] += 1

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"elements": [{"tags": {"name": "טור סיני"},
                                      "center": {"lat": 31.27032, "lon": 34.80668}}]}
        return R()

    import time
    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(time, "sleep", lambda *_: None)   # _suggest's `import time` is this module
    d._sugg_cache.clear()

    assert d._suggest("רחוב סיני") == ("טור סיני", 31.27032, 34.80668)
    d._suggest("רחוב סיני")                     # same token -> served from cache
    assert calls["n"] == 1


# --- doctor's failures ride the digest (a check nobody reads is not a check) --------

def test_health_section_lists_each_failure_with_its_remedy(monkeypatch):
    import doctor
    monkeypatch.setattr(doctor, "checks", lambda: [
        ("osrm", doctor.FAIL, "connection refused", "start the container"),
        ("db", doctor.PASS, "346 listings", ""),
    ])
    out = "\n".join(d._health_section())
    assert "osrm" in out and "connection refused" in out
    assert "start the container" in out          # doctor's fix line is what makes it useful
    assert "db" not in out                       # passing rows say nothing


def test_health_section_is_silent_when_everything_passes(monkeypatch):
    import doctor
    monkeypatch.setattr(doctor, "checks", lambda: [("db", doctor.PASS, "ok", "")])
    assert d._health_section() == [], "silence must mean healthy, not a daily cry"


def test_only_hard_failures_are_reported(monkeypatch):
    """A digest that complains every day is one you stop opening — which is the failure
    this exists to fix, not repeat. WARN stays in `doctor.py`."""
    import doctor
    monkeypatch.setattr(doctor, "checks", lambda: [("sheets", doctor.WARN, "partial", "fix it")])
    assert d._health_section() == []


def test_a_broken_doctor_cannot_suppress_the_rest_of_the_digest(monkeypatch):
    """The unmapped-locations report has always been this digest's job; a health check
    that throws must not take it down with it."""
    import doctor
    monkeypatch.setattr(doctor, "checks", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    out = "\n".join(d._health_section())
    assert "boom" in out and out.startswith("⚠")


def test_a_failure_alone_is_enough_to_send(monkeypatch):
    """Nothing unmapped and nothing low-confidence used to mean `build` returned None —
    so a wedged scraper would have been reported by nobody."""
    import doctor
    monkeypatch.setattr(storage, "unknown_locations", lambda *a, **k: [])
    monkeypatch.setattr(d, "_low_confidence_section", list)
    monkeypatch.setattr(doctor, "checks", lambda: [("osrm", doctor.FAIL, "down", "")])
    text = d.build(days=1, suggest=False)
    assert text and "osrm" in text


# --- the log records what failed ONCE; nothing re-checked it ------------------------
#
# Mirrors tests/test_stats.py, which fixed the same bug in the same shape. The filters
# are stubbed rather than exercised so these assert THIS module's wiring; the filters
# themselves are tested in test_geocode.py.

def _build(monkeypatch, rows, unplaceable=(), bearings=(), **kw) -> str:
    monkeypatch.setattr(storage, "unknown_locations", lambda *a, **k: rows)
    monkeypatch.setattr(geocode, "still_unplaceable", lambda n: n in unplaceable)
    monkeypatch.setattr(geocode, "names_only_a_landmark", lambda n: n in bearings)
    monkeypatch.setattr(d, "_low_confidence_section", list)
    monkeypatch.setattr(d, "_health_section", list)
    return d.build(suggest=False, **kw) or ""


def test_a_location_that_resolves_today_is_not_reported(monkeypatch):
    """`unknown_locations` logs what failed ONCE and nothing expires an entry, so this
    digest asked for pins on names that resolve perfectly well now."""
    out = _build(monkeypatch, [("שכונת הפארק", 5, "2026-07-23"), ("הרקנוס 37", 1, "2026-08-10")],
                 unplaceable={"הרקנוס 37"})
    assert "שכונת הפארק" not in out, out
    assert "הרקנוס 37" in out, out


def test_a_bearing_off_a_landmark_is_never_reported(monkeypatch):
    """THE FILTER THAT EARNS AT days=1. Measured 2026-08-12 through
    `storage.unknown_locations(1)`: of the 4 names logged, the staleness filter removed
    **0** — a no-op on a one-day window, exactly as expected, since a name resolves only
    once somebody ACTS on this digest — while this one removed **3**, the most frequent
    among them. "near the university" is not pinnable today and will not be in a year."""
    out = _build(monkeypatch, [("ליד האוניברסיטה", 3, "2026-08-11"), ("הרקנוס 37", 1, "2026-08-10")],
                 unplaceable={"ליד האוניברסיטה", "הרקנוס 37"}, bearings={"ליד האוניברסיטה"})
    assert "האוניברסיטה" not in out, out
    assert "הרקנוס 37" in out, out


def test_the_staleness_filter_still_applies_at_a_wider_window(monkeypatch):
    """`days` comes from argv — `python dm_digest.py 30` lands in the regime where 98 of
    182 logged names resolve. The default window is not the only window it runs in, which
    is why the near-no-op filter stays."""
    out = _build(monkeypatch, [("שכונת הפארק", 5, "2026-07-23")], days=30)
    assert "שכונת הפארק" not in out, out


def test_both_exclusions_stay_visible(monkeypatch):
    """A silent filter hides a real gap, and on a Telegram digest that would be invisible:
    nobody diffs a message against the DB. The header carries n-of-N and each removed
    group prints its own count."""
    out = _build(monkeypatch,
                 [("שכונת הפארק", 5, "2026-07-23"), ("ליד האוניברסיטה", 3, "2026-08-11"),
                  ("הרקנוס 37", 1, "2026-08-10")],
                 unplaceable={"ליד האוניברסיטה", "הרקנוס 37"}, bearings={"ליד האוניברסיטה"})
    assert "1" in out and "3" in out, out          # "1 מתוך 3"
    assert "כבר נפתרו מאז" in out, out
    assert "לא ניתנים לקיבוע לעולם" in out, out


def test_the_report_is_escaped_exactly_once(monkeypatch):
    """`_excluded_lines` returns RAW text because its three callers escape differently —
    this one escapes per line and `main` sends MarkdownV2, `weekly_digest` escapes the
    whole string in `main`, and `bot_listener` sends with no parse_mode at all. Escaping
    inside the helper would double-escape here and show backslashes there."""
    out = _build(monkeypatch, [("הרקנוס 37", 1, "2026-08-10"), ("ליד האוניברסיטה", 3, "2026-08-11")],
                 unplaceable={"הרקנוס 37", "ליד האוניברסיטה"}, bearings={"ליד האוניברסיטה"})
    assert "\\(" in out, "the excluded-count line must be escaped for MarkdownV2"
    assert "\\\\" not in out, f"escaped twice: {out}"


def test_nothing_pinnable_reports_nothing(monkeypatch):
    """Every logged name already resolves -> no section at all, not an empty heading."""
    assert _build(monkeypatch, [("שכונת הפארק", 5, "2026-07-23")]) == ""
