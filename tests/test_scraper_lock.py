"""Heartbeat + wedged-run recovery.

Context: on 2026-07-27 a run worked for ~7h (local-LLM fallback) and then wedged for
~30h, silently blocking every later run. The guard therefore keys on PROGRESS, never on
elapsed time — a slow-but-working run must be left completely alone.
"""
import time

import config
import scraper


def _hb(tmp_path, monkeypatch):
    monkeypatch.setattr(scraper, "_HEARTBEAT_PATH", tmp_path / "scraper.heartbeat")


def test_beat_and_age(tmp_path, monkeypatch):
    _hb(tmp_path, monkeypatch)
    assert scraper.heartbeat_age() is None            # nothing recorded yet
    scraper.beat("working")
    age = scraper.heartbeat_age()
    assert age is not None and age < 5
    assert scraper.heartbeat_pid() == __import__("os").getpid()
    assert scraper.is_wedged() is False


def test_wedged_only_after_the_stall_threshold(tmp_path, monkeypatch):
    _hb(tmp_path, monkeypatch)
    path = tmp_path / "scraper.heartbeat"
    # a run that reported progress 5 minutes ago is healthy...
    path.write_text(f"4242 {time.time() - 5 * 60:.0f} x", encoding="utf-8")
    assert scraper.is_wedged() is False
    # ...and one silent for longer than STALL_MINUTES is wedged
    path.write_text(f"4242 {time.time() - (config.STALL_MINUTES + 1) * 60:.0f} x",
                    encoding="utf-8")
    assert scraper.is_wedged() is True


def test_slow_but_progressing_run_is_never_killed(tmp_path, monkeypatch):
    """THE regression this design exists to prevent: a local-LLM run legitimately ran
    268 minutes. It must NOT be treated as wedged just for being long."""
    _hb(tmp_path, monkeypatch)
    path = tmp_path / "scraper.heartbeat"
    # started ~4.5h ago but reported progress 30 seconds ago
    path.write_text(f"4242 {time.time() - 30:.0f} post 900", encoding="utf-8")
    assert scraper.is_wedged() is False

    killed = []
    monkeypatch.setattr(scraper.subprocess if hasattr(scraper, "subprocess") else scraper,
                        "run", lambda *a, **k: killed.append(a), raising=False)
    assert scraper._clear_wedged_holder() is False     # refuses to touch a live worker
    assert killed == []


def test_clear_wedged_holder_kills_a_stalled_run(tmp_path, monkeypatch):
    _hb(tmp_path, monkeypatch)
    (tmp_path / "scraper.heartbeat").write_text(
        f"4242 {time.time() - (config.STALL_MINUTES + 5) * 60:.0f} stuck", encoding="utf-8")
    cmds = []

    def fake_run(cmd, **k):
        cmds.append(cmd)

        class R:
            # tasklist finds nothing -> the pid really is gone
            stdout = "" if cmd[0] != "powershell" else ""
        return R()

    monkeypatch.setattr(scraper.subprocess, "run", fake_run)
    assert scraper._clear_wedged_holder() is True
    kill = next(c for c in cmds if c[0] == "taskkill")
    assert kill[:2] == ["taskkill", "/PID"] and "4242" in kill
    # NOT /T any more: walking a Chromium tree is what blew past the 30 s budget and
    # made the takeover fail, so the children are reaped separately instead.
    assert "/T" not in kill
    assert any(c[0] == "tasklist" for c in cmds)        # death is verified, not assumed


# --- is a run actually running? -----------------------------------------------------
def _fake_ps(monkeypatch, stdout, returncode=0):
    class R:
        pass

    def fake_run(cmd, **kw):
        R.stdout, R.returncode = stdout, returncode
        return R

    monkeypatch.setattr(scraper.subprocess, "run", fake_run)


def test_run_in_progress_reads_the_heartbeat_pids_own_command_line(tmp_path, monkeypatch):
    """The heartbeat file outlives its run, so its age can only say when a run last
    worked — never whether one is working now."""
    _hb(tmp_path, monkeypatch)
    (tmp_path / "scraper.heartbeat").write_text(f"4242 {time.time():.0f} x",
                                                encoding="utf-8")
    _fake_ps(monkeypatch, '"C:\\python.exe" -u main.py --live\n')
    assert scraper.run_in_progress() is True
    # the pid is gone: PowerShell finds no such process and prints nothing
    _fake_ps(monkeypatch, "")
    assert scraper.run_in_progress() is False
    # the pid was recycled by something that is not ours -> not a scrape
    _fake_ps(monkeypatch, '"C:\\chrome.exe" --profile-directory=Default\n')
    assert scraper.run_in_progress() is False


def test_run_in_progress_says_unknown_rather_than_guessing(tmp_path, monkeypatch):
    """"couldn't ask" and "nothing is running" must not collapse into one answer — the
    caller spends a health verdict on the difference."""
    _hb(tmp_path, monkeypatch)
    (tmp_path / "scraper.heartbeat").write_text(f"4242 {time.time():.0f} x",
                                                encoding="utf-8")

    def boom(*a, **k):
        raise OSError("powershell missing")

    monkeypatch.setattr(scraper.subprocess, "run", boom)
    assert scraper.run_in_progress() is None
    _fake_ps(monkeypatch, "", returncode=1)          # the query itself failed
    assert scraper.run_in_progress() is None
    # no heartbeat at all -> nothing is running (doctor SKIPs before it ever asks)
    monkeypatch.setattr(scraper, "_HEARTBEAT_PATH", tmp_path / "gone.heartbeat")
    assert scraper.run_in_progress() is False


def test_a_live_run_is_matched_by_command_line_not_process_name(tmp_path, monkeypatch):
    """Same rule as _profile_browser_pids: "python.exe" says nothing about whose script
    it is running."""
    _hb(tmp_path, monkeypatch)
    (tmp_path / "scraper.heartbeat").write_text(f"4242 {time.time():.0f} x",
                                                encoding="utf-8")
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd

        class R:
            stdout, returncode = '"C:\\python.exe" -u main.py --live --hot\n', 0
        return R

    monkeypatch.setattr(scraper.subprocess, "run", fake_run)
    assert scraper.run_in_progress() is True          # a --hot run counts too
    joined = " ".join(seen["cmd"])
    assert "ProcessId=4242" in joined and "CommandLine" in joined


# --- recovering from a hung run ----------------------------------------------------
def test_browser_reaping_is_scoped_to_our_own_profile(monkeypatch):
    """The user's own Chrome is normally running — 36 of 39 chrome.exe processes when
    this was written. Reaping by process name would close their browser, so the query
    must filter on the scraper's profile path."""
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        class R:
            stdout = "111\n222\n"
        return R()

    monkeypatch.setattr(scraper.subprocess, "run", fake_run)
    assert scraper._profile_browser_pids() == [111, 222]
    joined = " ".join(seen["cmd"])
    assert str(config.SCRAPER_PROFILE_DIR) in joined      # scoped to OUR profile
    assert "CommandLine -like" in joined                  # …by command line, not name


def test_listing_browsers_never_guesses_on_failure(monkeypatch):
    """Returning a broader set on error would mean killing the user's Chrome."""
    def boom(*a, **k):
        raise OSError("powershell missing")

    monkeypatch.setattr(scraper.subprocess, "run", boom)
    assert scraper._profile_browser_pids() == []


def test_a_kill_is_judged_by_whether_the_process_is_gone(monkeypatch):
    """taskkill /T on a Chromium tree exceeded its 30 s budget, and the timeout was
    treated as total failure — so the lock was never taken over and every later run
    skipped. What matters is whether the pid still exists afterwards."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd[0])
        if cmd[0] == "taskkill":
            raise scraper.subprocess.TimeoutExpired(cmd, 20)   # the old fatal case
        class R:
            stdout = "INFO: No tasks are running which match the specified criteria."
        return R()

    monkeypatch.setattr(scraper.subprocess, "run", fake_run)
    assert scraper._kill(4242) is True          # timed out, but the process IS gone
    assert "tasklist" in calls                  # …because we verified rather than assumed
    assert "/T" not in " ".join(str(c) for c in calls)


def test_a_kill_that_really_failed_reports_false(monkeypatch):
    def fake_run(cmd, **kw):
        class R:
            stdout = "chrome.exe   4242 Console   1   120,000 K" if cmd[0] == "tasklist" else ""
        return R()

    monkeypatch.setattr(scraper.subprocess, "run", fake_run)
    assert scraper._kill(4242) is False


def test_the_wedge_takeover_does_not_depend_on_taskkills_exit_code(monkeypatch):
    monkeypatch.setattr(scraper, "is_wedged", lambda: True)
    monkeypatch.setattr(scraper, "heartbeat_pid", lambda: 9999)
    monkeypatch.setattr(scraper, "heartbeat_age", lambda: 3600.0)
    monkeypatch.setattr(scraper, "reap_orphan_browsers", lambda: 0)
    monkeypatch.setattr(scraper, "_kill", lambda pid, timeout=20: True)
    assert scraper._clear_wedged_holder() is True
    monkeypatch.setattr(scraper, "_kill", lambda pid, timeout=20: False)
    assert scraper._clear_wedged_holder() is False    # still alive -> leave the lock


# --- teardown must never keep the lock (2026-08-04) ---------------------------------

def test_a_hanging_close_does_not_hold_the_lock(monkeypatch):
    """Playwright's node subprocess died with EPIPE mid-run and `context.close()` never
    returned, so the python process sat alive holding the OS file lock: the 17:00 and
    00:46 runs both skipped with "another scraper session is running". A hang is not
    catchable, so each teardown step gets its own deadline."""
    import threading

    import main
    monkeypatch.setattr(main, "TEARDOWN_TIMEOUT_SEC", 0.2)
    never = threading.Event()

    class Hanging:
        def close(self):
            never.wait()          # exactly what a dead pipe does

    class P:
        stopped = False

        def stop(self):
            P.stopped = True

    main._bounded_teardown(Hanging(), P())     # must RETURN, not block
    assert P.stopped, "a hang in close() must not skip the next teardown step"


def test_a_raising_close_does_not_skip_the_rest(monkeypatch):
    import main
    monkeypatch.setattr(main, "TEARDOWN_TIMEOUT_SEC", 5)

    class Boom:
        def close(self):
            raise RuntimeError("EPIPE")

    class P:
        stopped = False

        def stop(self):
            P.stopped = True

    main._bounded_teardown(Boom(), P())
    assert P.stopped


def test_the_lock_is_released_even_when_teardown_explodes(monkeypatch, tmp_path):
    """The whole point: release_lock() runs after _bounded_teardown, and nothing the
    browser does can stop it being reached."""
    import inspect

    import main
    src = inspect.getsource(main.run)
    tail = src[src.rindex("finally:"):]
    # CODE ONLY — the comment above the call also says "release_lock", and matching that
    # made the order check pass on prose instead of on what actually runs.
    code = "\n".join(ln for ln in tail.splitlines() if not ln.strip().startswith("#"))
    assert "_bounded_teardown(" in code
    assert "scraper.release_lock()" in code
    assert code.index("_bounded_teardown(") < code.index("scraper.release_lock()")


# --- keep-awake, but only on mains (user's rule, 2026-08-05) ------------------------

def test_keep_awake_only_holds_the_machine_when_plugged_in(monkeypatch):
    """The 00:46 hot run slept through the night mid-flight: 8.5 h wall clock for ~23 min
    of work, holding the lock, so the 08:58 full run skipped. But the guard must not
    drain a battery — on battery, and when the power state is UNKNOWN, it stays off."""
    import scraper
    calls = []
    monkeypatch.setattr(scraper, "_set_awake", lambda on: calls.append(on))

    for power, want_hold in ((True, True), (False, False), (None, False)):
        calls.clear()
        monkeypatch.setattr(scraper, "on_ac_power", lambda p=power: p)
        stop = scraper.start_keep_awake(poll_seconds=0.05)
        import time
        time.sleep(0.15)
        stop()
        time.sleep(0.15)
        assert (True in calls) is want_hold, f"power={power} calls={calls}"


def test_the_keep_awake_guard_is_released_with_the_lock():
    """A guard that outlives the run would keep the machine awake forever."""
    import inspect

    import main
    src = inspect.getsource(main.run)
    tail = src[src.rindex("finally:"):]
    code = "\n".join(ln for ln in tail.splitlines() if not ln.strip().startswith("#"))
    assert "stop_keep_awake()" in code
    assert code.index("stop_keep_awake()") < code.index("scraper.release_lock()")
