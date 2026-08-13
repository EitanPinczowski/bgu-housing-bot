"""doctor health checks: each dependency reports the right status, and every FAIL
carries a remediation hint (the point of the command). All deps mocked, no network."""
import config
import doctor
import pytest


@pytest.fixture(autouse=True)
def _this_module_tests_try_fix_itself(monkeypatch):
    """Undo the conftest stub for THIS file only.

    `_no_test_may_try_to_heal_osrm` neuters `doctor.try_fix` everywhere, because
    `main.run()` now calls it and every test exercising a run would otherwise shell out to
    Docker (measured: 0.33s -> 128s). These tests are the exception it was written for —
    they are the ones that check `try_fix` does the right thing, and they stub `subprocess`
    themselves."""
    import doctor
    monkeypatch.setattr(doctor, "try_fix", doctor._real_try_fix)

def test_osrm_down_has_docker_remediation(monkeypatch):
    monkeypatch.setattr(doctor, "_osrm_ok", lambda: False)
    name, status, detail, rem = doctor._check_osrm()
    assert name == "osrm" and status == doctor.WARN      # bot still works via straight-line
    assert "docker start osrm_bgu" in rem                 # the exact fix this session needed


def test_osrm_up_passes(monkeypatch):
    monkeypatch.setattr(doctor, "_osrm_ok", lambda: True)
    assert doctor._check_osrm()[1] == doctor.PASS


def test_config_check_fails_on_bad_config(monkeypatch):
    def boom():
        raise SystemExit("config error — fix config.py:\n  - TARGET > MAX")
    monkeypatch.setattr(doctor.config, "validate", boom)
    name, status, detail, rem = doctor._check_config()
    assert status == doctor.FAIL and rem


def test_telegram_missing_env(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    name, status, detail, rem = doctor._check_telegram()
    assert status == doctor.FAIL and ".env" in rem


def test_data_file_missing_names_the_loader(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor.config, "NEIGHBORHOODS_PATH", tmp_path / "nope.json")
    row = next(r for r in doctor._check_data_files() if r[0].endswith("nope.json"))
    assert row[1] == doctor.FAIL and "load_neighborhoods.py" in row[3]


def test_every_failure_carries_remediation(monkeypatch):
    monkeypatch.setattr(doctor, "_osrm_ok", lambda: False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    for name, status, detail, rem in doctor.checks():
        if status == doctor.FAIL:
            assert rem, f"{name} FAILed without a remediation hint"


def test_last_run_check(monkeypatch, tmp_path):
    from datetime import datetime, timedelta

    import scraper
    log = tmp_path / "search_log.txt"
    monkeypatch.setattr(doctor.config, "DATA_DIR", tmp_path)
    # STUB THE LIVE-RUN PROBE. Without this the test reads the real machine: it passed
    # for months only because no scrape happened to be running, and failed the moment
    # one was (2026-08-04). A health-check test must not depend on the health of the
    # machine it runs on.
    monkeypatch.setattr(scraper, "run_in_progress", lambda: False)

    def write(when):
        log.write_text(f"{when:%Y-%m-%d %H:%M:%S}  END    LIVE  10s posts=5\n", encoding="utf-8")

    write(datetime.now() - timedelta(hours=1))
    assert doctor._check_last_run()[1] == doctor.PASS
    # a long silence during active hours is a FAIL that names the likely cause
    write(datetime.now() - timedelta(hours=12))
    name, status, detail, rem = doctor._check_last_run()
    if 8 <= datetime.now().hour <= 20:            # the check is quiet outside those hours
        assert status == doctor.FAIL and ("asleep" in rem or "Task Scheduler" in rem)


def test_a_run_in_flight_is_not_a_missed_run(monkeypatch, tmp_path):
    """This row measures time since the last COMPLETION, so a long run makes it climb
    while the scraper is working perfectly. Observed 2026-08-04: it said "none for 5.6h"
    while `scraper progress` in the same report said "last progress 1 min ago"."""
    from datetime import datetime, timedelta

    import scraper
    log = tmp_path / "search_log.txt"
    monkeypatch.setattr(doctor.config, "DATA_DIR", tmp_path)
    log.write_text(f"{datetime.now() - timedelta(hours=12):%Y-%m-%d %H:%M:%S}"
                   "  END    LIVE  10s posts=5\n", encoding="utf-8")
    monkeypatch.setattr(scraper, "run_in_progress", lambda: True)
    name, status, detail, _rem = doctor._check_last_run()
    assert status == doctor.PASS and "in progress" in detail
    # no log at all -> a warning with a starting point, never a crash
    monkeypatch.setattr(doctor.config, "DATA_DIR", tmp_path / "empty")
    assert doctor._check_last_run()[1] == doctor.WARN


def test_listener_check(monkeypatch):
    monkeypatch.setattr(doctor, "_listener_running", lambda: True)
    assert doctor._check_listener()[1] == doctor.PASS
    monkeypatch.setattr(doctor, "_listener_running", lambda: False)
    name, status, detail, rem = doctor._check_listener()
    assert status == doctor.FAIL and "run_listener" in rem


def test_wedged_scraper_check(monkeypatch):
    import scraper
    monkeypatch.setattr(scraper, "run_in_progress", lambda: True)
    # a run that reported progress recently is healthy even if it started hours ago
    monkeypatch.setattr(scraper, "heartbeat_age", lambda: 45.0)
    assert doctor._check_wedged_scraper()[1] == doctor.PASS
    # silence beyond the stall threshold is a wedged run
    monkeypatch.setattr(scraper, "heartbeat_age",
                        lambda: (doctor.config.STALL_MINUTES + 10) * 60)
    name, status, detail, rem = doctor._check_wedged_scraper()
    assert status == doctor.FAIL and "wedged" in rem
    # never run -> SKIP, not a false alarm
    monkeypatch.setattr(scraper, "heartbeat_age", lambda: None)
    assert doctor._check_wedged_scraper()[1] == doctor.SKIP


def test_stale_heartbeat_is_only_a_wedge_while_a_run_is_live(monkeypatch):
    """The heartbeat file outlives the run that wrote it, so between scheduled runs its
    age only grows. On 2026-08-03 that FAILed at 13:30 ("no progress for 31 min") with
    no main.py process anywhere and the 08:00 run finished cleanly at 13:11 — the same
    doctor run's `last run` row said PASS 0.5h ago."""
    import scraper
    monkeypatch.setattr(scraper, "heartbeat_age",
                        lambda: (doctor.config.STALL_MINUTES + 10) * 60)
    # stale heartbeat + a live run = the genuine wedge this check exists for
    monkeypatch.setattr(scraper, "run_in_progress", lambda: True)
    assert doctor._check_wedged_scraper()[1] == doctor.FAIL
    # stale heartbeat + nothing running = an idle machine, which is the normal state
    monkeypatch.setattr(scraper, "run_in_progress", lambda: False)
    name, status, detail, rem = doctor._check_wedged_scraper()
    assert status == doctor.PASS and "idle" in detail
    # can't tell -> say so; a failed process query is not evidence of a hang
    monkeypatch.setattr(scraper, "run_in_progress", lambda: None)
    assert doctor._check_wedged_scraper()[1] == doctor.WARN


def test_fix_starts_osrm_when_down(monkeypatch):
    # engine fine, CONTAINER stopped -> --fix runs `docker start <container>`, re-probes
    states = iter([False, True])          # down at check, up after start
    monkeypatch.setattr(doctor, "_osrm_ok", lambda: next(states))
    monkeypatch.setattr(doctor, "_osrm_ok_retry", lambda tries=6: True)
    monkeypatch.setattr(doctor, "_daemon_ok", lambda timeout=12: True)
    calls = {}
    import subprocess

    class _R:
        returncode = 0
        stdout = stderr = ""

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        return _R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    done = doctor.try_fix()
    assert calls["cmd"][:2] == ["docker", "start"]
    assert calls["cmd"][2] == doctor.config.OSRM_DOCKER_CONTAINER
    assert done and done[0][1] is True     # reported as fixed


def test_fix_starts_the_engine_before_the_container(monkeypatch):
    """`run_scraper.cmd` ran `docker start osrm_bgu` before every scrape and 14 of 88
    completed runs still logged OSRM DOWN: a container command cannot help when the
    ENGINE is down. The repair has to happen in that order."""
    monkeypatch.setattr(doctor, "_osrm_ok", lambda: False)
    monkeypatch.setattr(doctor, "_daemon_ok", lambda timeout=12: False)
    monkeypatch.setattr(doctor, "_osrm_ok_retry", lambda tries=6: True)
    monkeypatch.setattr(doctor, "_start_docker_desktop", lambda: (True, "launched"))
    import subprocess

    class _R:
        returncode = 0
        stdout = stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _R())
    done = doctor.try_fix()
    assert [what for what, *_ in done] == ["start Docker Desktop", "start OSRM"]


def test_fix_does_not_ask_a_dead_engine_to_start_a_container(monkeypatch):
    """If Docker Desktop will not come up, `docker start` can only fail confusingly.
    Stop, and report the reason that actually matters."""
    monkeypatch.setattr(doctor, "_osrm_ok", lambda: False)
    monkeypatch.setattr(doctor, "_daemon_ok", lambda timeout=12: False)
    monkeypatch.setattr(doctor, "_start_docker_desktop",
                        lambda: (False, "Docker Desktop.exe not found"))
    done = doctor.try_fix()
    assert [what for what, *_ in done] == ["start Docker Desktop"]
    assert done[0][1] is False


def test_try_fix_never_runs_a_destructive_docker_command(monkeypatch):
    """try_fix only ever STARTS things. `osrm_bgu` is a multi-GB rebuild from the Israel
    PBF, and guard.py blocks prune/rmi from a shell — this must not become the way
    around that."""
    monkeypatch.setattr(doctor, "_osrm_ok", lambda: False)
    monkeypatch.setattr(doctor, "_daemon_ok", lambda timeout=12: True)
    monkeypatch.setattr(doctor, "_osrm_ok_retry", lambda tries=6: True)
    seen = []
    import subprocess

    class _R:
        returncode = 0
        stdout = stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: (seen.append(cmd), _R())[1])
    doctor.try_fix()
    flat = " ".join(" ".join(c) for c in seen)
    for forbidden in ("prune", "rmi", "rm ", "kill", "down"):
        assert forbidden not in flat, f"try_fix ran a destructive docker verb: {flat}"


def test_fix_noop_when_osrm_up(monkeypatch):
    monkeypatch.setattr(doctor, "_osrm_ok", lambda: True)
    assert doctor.try_fix() == []          # nothing to heal


def test_chains_report_backends(monkeypatch):
    monkeypatch.setattr(doctor, "_http_ok", lambda *a, **k: True)
    monkeypatch.setattr(doctor, "_ollama_ok", lambda: True)
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    ch = dict(doctor.chains())
    assert set(ch) >= {"geocode", "llm", "overpass mirrors"}
    assert any(b[0] == "overpass" for b in ch["geocode"])   # geocode chain lists overpass
    assert ch["llm"][0][0] == "gemini"                       # gemini is the primary LLM


def test_wake_timer_check(monkeypatch):
    """The silent failure behind "why didn't it run": a task that can't wake the PC is
    skipped entirely when the machine is asleep, with no error anywhere."""
    monkeypatch.setattr(doctor, "_task_wake_flags",
                        lambda: {"BGU Housing Scraper": True, "BGU Morning": True})
    assert doctor._check_wake_timers()[1] == doctor.PASS
    monkeypatch.setattr(doctor, "_task_wake_flags",
                        lambda: {"BGU Housing Scraper": False, "BGU Morning": True})
    name, status, detail, rem = doctor._check_wake_timers()
    assert status == doctor.WARN
    assert "BGU Housing Scraper" in detail
    assert "setup_always_on" in rem
    # not Windows / no Task Scheduler -> SKIP, never a false alarm
    monkeypatch.setattr(doctor, "_task_wake_flags", lambda: None)
    assert doctor._check_wake_timers()[1] == doctor.SKIP


def test_hot_pass_scheduled_check(monkeypatch):
    """The --hot pass was built, documented and budgeted, but never scheduled — for
    days nothing failed, detection was just quietly 8.4 h slow."""
    monkeypatch.setattr(doctor, "_task_wake_flags",
                        lambda: {"BGU Housing Scraper": True})
    name, status, detail, rem = doctor._check_hot_scheduled()
    assert status == doctor.WARN and "update_schedule" in rem
    monkeypatch.setattr(doctor, "_task_wake_flags",
                        lambda: {"BGU Housing Scraper": True,
                                 "BGU Housing Scraper Hot": True})
    assert doctor._check_hot_scheduled()[1] == doctor.PASS
    monkeypatch.setattr(doctor, "_task_wake_flags", lambda: None)
    assert doctor._check_hot_scheduled()[1] == doctor.SKIP


def test_a_budget_above_googles_stated_limit_is_a_hard_failure(tmp_path, monkeypatch):
    """LLM_DAILY_BUDGET was 900 against a real limit of 500, so it could never bind and
    Google refused first — which is exactly what sends a run to the local model. The
    number Google states in its refusal is the one to trust."""
    import doctor
    import llm
    monkeypatch.setattr(llm, "_BUDGET_PATH", tmp_path / "b.json")
    monkeypatch.setattr(config, "LLM_DAILY_BUDGET", 900)
    llm._spend_budget(506)
    llm.record_quota_refusal(
        "429 RESOURCE_EXHAUSTED ... limit: 500, model: gemini-3.5-flash-lite "
        "quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier")
    name, status, detail, remedy = doctor._check_llm_budget()
    assert name == "llm budget" and status == doctor.FAIL, (status, detail)
    assert "500" in detail and "500" in remedy
    assert "900" in remedy                      # says what it currently is


def test_a_budget_under_the_stated_limit_is_not_a_failure(tmp_path, monkeypatch):
    import doctor
    import llm
    monkeypatch.setattr(llm, "_BUDGET_PATH", tmp_path / "b.json")
    monkeypatch.setattr(config, "LLM_DAILY_BUDGET", 480)
    llm._spend_budget(490)
    llm.record_quota_refusal("429 ... limit: 500 ... quotaId: GenerateRequestsPerDay")
    _name, status, _detail, _remedy = doctor._check_llm_budget()
    assert status != doctor.FAIL


def _backup(tmp_path, name, age_hours):
    import os
    import time as _t
    d = tmp_path / "backups"
    d.mkdir(exist_ok=True)
    f = d / name
    f.write_bytes(b"x")
    t = _t.time() - age_hours * 3600
    os.utime(f, (t, t))
    return f


def test_backups_pass_when_recent(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.config, "DATA_DIR", tmp_path)
    _backup(tmp_path, "listings-20260809-213000.sqlite", 3)
    name, status, detail, _ = doctor._check_backups()
    assert status == doctor.PASS and "3h old" in detail


def test_backups_fail_when_the_schedule_has_stopped(tmp_path, monkeypatch):
    """The whole point. `_check_db`'s remediation says "restore from data/backups/",
    which is advice worth exactly as much as the newest file in there — so a stopped
    backup job has to be able to make this row RED. A check that can only say PASS is
    not a check."""
    monkeypatch.setattr(doctor.config, "DATA_DIR", tmp_path)
    _backup(tmp_path, "listings-20260801-213000.sqlite", 72)
    name, status, detail, remediation = doctor._check_backups()
    assert status == doctor.FAIL
    assert "72h old" in detail
    assert "BGU Backup" in remediation      # names the task to go and look at


def test_backups_fail_when_there_are_none(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.config, "DATA_DIR", tmp_path)
    assert doctor._check_backups()[1] == doctor.FAIL


def test_backups_read_the_newest_not_the_last_alphabetically(tmp_path, monkeypatch):
    """Age comes from mtime, not from the name. A restored or re-copied file sorts by
    its filename but is not the freshest thing on disk."""
    monkeypatch.setattr(doctor.config, "DATA_DIR", tmp_path)
    _backup(tmp_path, "listings-20260809-213000.sqlite", 99)   # newest NAME, stale file
    _backup(tmp_path, "listings-20260101-213000.sqlite", 2)    # oldest name, fresh file
    assert doctor._check_backups()[1] == doctor.PASS


def test_the_overpass_probe_sends_what_geocode_sends(monkeypatch):
    """A health check that does not send what the CODE sends is testing a request nobody
    makes. This probe was a GET with no User-Agent while `geocode` POSTs with
    `config.NOMINATIM_USER_AGENT` — and measured 2026-08-10 against a demonstrably
    healthy overpass-api.de, the default `python-requests` agent is refused outright with
    406 Not Acceptable. The row could never pass, on any mirror, at any time."""
    seen = {}

    class _R:
        status_code = 200

    def fake_post(url, **kw):
        seen.update(kw)
        seen["url"] = url
        return _R()

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    assert doctor._overpass_ok(doctor.config.OVERPASS_URLS[0]) is True
    assert seen["headers"]["User-Agent"] == doctor.config.NOMINATIM_USER_AGENT
    assert "data" in seen["data"]


def test_the_overpass_probe_query_is_bounded():
    """`[out:json];out;` has no filter — it asks the server to serialise the ENTIRE
    dataset, and timed out after 8.3s against the same mirror that answered a bounded
    query in 815ms. A check that can only FAIL is as useless as one that can only PASS,
    and worse here: it trains you to ignore the row."""
    q = doctor._OVERPASS_PROBE
    assert "out;" not in q.replace("out 1;", ""), "unbounded `out;` — this cannot succeed"
    assert "node(" in q and "timeout:" in q          # a real, bounded, cheap query


def test_a_dead_mirror_is_still_reported(monkeypatch):
    """The fix must not make the row unable to fail either."""
    def boom(*a, **k):
        raise OSError("connection refused")

    import requests
    monkeypatch.setattr(requests, "post", boom)
    rows = doctor._overpass_live()
    assert rows and all(s == doctor.FAIL for _, s, _ in rows)
