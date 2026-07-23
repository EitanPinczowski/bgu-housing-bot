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
