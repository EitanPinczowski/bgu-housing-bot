"""doctor health checks: each dependency reports the right status, and every FAIL
carries a remediation hint (the point of the command). All deps mocked, no network."""
import doctor


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
    log = tmp_path / "search_log.txt"
    monkeypatch.setattr(doctor.config, "DATA_DIR", tmp_path)

    def write(when):
        log.write_text(f"{when:%Y-%m-%d %H:%M:%S}  END    LIVE  10s posts=5\n", encoding="utf-8")

    write(datetime.now() - timedelta(hours=1))
    assert doctor._check_last_run()[1] == doctor.PASS
    # a long silence during active hours is a FAIL that names the likely cause
    write(datetime.now() - timedelta(hours=12))
    name, status, detail, rem = doctor._check_last_run()
    if 8 <= datetime.now().hour <= 20:            # the check is quiet outside those hours
        assert status == doctor.FAIL and ("asleep" in rem or "Task Scheduler" in rem)
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


def test_fix_starts_osrm_when_down(monkeypatch):
    # OSRM down -> --fix runs `docker start <container>` then re-probes
    states = iter([False, True])          # down at check, up after start
    monkeypatch.setattr(doctor, "_osrm_ok", lambda: next(states))
    monkeypatch.setattr(doctor, "_osrm_ok_retry", lambda tries=6: True)
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
