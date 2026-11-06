"""Offline tests for apphome.py: data folder, first-run files, importing the old folder, keys, launching scripts."""
import sys
import tempfile
from pathlib import Path

import apphome as A


class FakeVault:
    def __init__(self):
        self.d = {}

    def available(self):
        return True

    def get(self, name):
        return self.d.get(name)

    def set(self, name, value):
        if value:
            self.d[name] = value
        else:
            self.d.pop(name, None)


def tmp():
    return Path(tempfile.mkdtemp())


def test_dev_mode_keeps_everything_in_the_project_folder():
    assert not A.FROZEN and A.HOME == A.CODE and (A.CODE / "orb_bot.py").exists()
    cmd = A.script_cmd("orb_bot.py", "--submit")
    assert cmd == [sys.executable, str(A.CODE / "orb_bot.py"), "--submit"]
    assert A.script_cmd("research/daily.py")[1] == str(A.CODE / "research" / "daily.py")


def test_app_mode_runs_scripts_through_the_app_itself():
    old = A.FROZEN
    A.FROZEN = True
    try:
        cmd = A.script_cmd("watchdog.py", "--flatten")
        assert cmd == [sys.executable, "--run", str(A.CODE / "watchdog.py"), "--flatten"]
        skill = A.CODE / "skills" / "trader-risk" / "check.py"
        assert A.script_cmd(skill, "x")[1:3] == ["--run", str(skill)]       # absolute paths are kept
    finally:
        A.FROZEN = old


def test_first_run_copies_defaults_once_and_never_overwrites():
    code, home = tmp(), tmp() / "OrbBot"
    (code / "config.json").write_text('{"strategy": "gap_fade"}')
    (code / "research").mkdir()
    (code / "research" / "forward_watch.json").write_text("{}")
    assert sorted(A.seed_home(home, code)) == ["config.json", "research/forward_watch.json"]
    (home / "config.json").write_text('{"mine": 1}')
    assert A.seed_home(home, code) == [] and (home / "config.json").read_text() == '{"mine": 1}'
    assert A.seed_home(code, code) == []                                    # dev mode: nothing to copy


def test_import_brings_history_but_not_program_files():
    old, home = tmp(), tmp()
    files = {"journal.csv": "j", "config.json": "{}", "bot.log": "l", "automation.json": "{}",
             "daily_inputs/inputs_2026-10-29.json": "{}", "state_history/2026-10-29.json": "{}",
             "research/data/AAPL.csv.gz": "x" * 100, "research/lessons.json": "[]", "research/notes/d.md": "n",
             "trader/config.json": "{}", "success_criteria_gap.json": "{}", "criteria_gap.lock": "abc",
             # not copied:
             "orb_bot.py": "code", "dashboard.py": "code", "open_dashboard.bat": "bat", "research/lab.py": "c",
             "__pycache__/x.pyc": "c", "skills/trader-risk/check.py": "c", "t_journal.csv": "test",
             "test_state.json": "test", "heartbeat.json": "{}", "dashboard_pids.json": "{}", "STOP": "x",
             "bt_cache/a.pkl": "c", "research/README.md": "r"}
    for rel, text in files.items():
        (old / rel).parent.mkdir(parents=True, exist_ok=True)
        (old / rel).write_text(text)
    n, size = A.import_from(old, home)
    got = sorted(str(p.relative_to(home)).replace("\\", "/") for p in home.rglob("*") if p.is_file())
    assert got == sorted(["journal.csv", "config.json", "bot.log", "automation.json", "daily_inputs/inputs_2026-10-29.json",
                          "state_history/2026-10-29.json", "research/data/AAPL.csv.gz", "research/lessons.json",
                          "research/notes/d.md", "trader/config.json", "success_criteria_gap.json", "criteria_gap.lock"])
    assert n == 12 and size >= 100
    for bad in (tmp(), home):                                               # not a bot folder / the data folder itself
        try:
            A.import_from(bad, home)
            assert False
        except ValueError:
            pass


def test_keys_environment_wins_vault_fills_the_rest_and_values_are_never_shown():
    v, env = FakeVault(), {"ALPACA_API_KEY": "PKFROMENV1234"}
    A._SOURCE.clear()
    v.d.update(ALPACA_API_KEY="PKVAULT", ALPACA_SECRET_KEY="S3CR3TVALUE", TELEGRAM_CHAT_ID="5551234")
    A.load_keys(v, env)
    assert env["ALPACA_API_KEY"] == "PKFROMENV1234" and env["ALPACA_SECRET_KEY"] == "S3CR3TVALUE"
    st = {k["name"]: k for k in A.key_status(env)}
    assert st["ALPACA_API_KEY"]["source"] == "environment" and st["ALPACA_SECRET_KEY"]["source"] == "saved in Windows"
    assert st["ANTHROPIC_API_KEY"]["set"] is False and st["ALPACA_API_KEY"]["hint"] == "…1234"
    assert st["ALPACA_SECRET_KEY"]["hint"] is None and "S3CR3TVALUE" not in repr(st)            # secrets never echoed
    saved = A.save_keys({"ANTHROPIC_API_KEY": '  "sk-ant-xyz" ', "ALPACA_SECRET_KEY": "", "BOGUS": "x"}, v, env)
    assert saved == ["ANTHROPIC_API_KEY"] and v.d["ANTHROPIC_API_KEY"] == "sk-ant-xyz" == env["ANTHROPIC_API_KEY"]
    assert v.d["ALPACA_SECRET_KEY"] == "S3CR3TVALUE"                                # a blank field leaves the key alone
    assert "BOGUS" not in v.d


def test_vault_without_keyring_refuses_to_save():
    v = A.Vault(backend=False)
    v._backend = False
    assert v.available() is False and v.get("X") is None
    try:
        v.set("X", "y")
        assert False
    except RuntimeError as e:
        assert "keyring" in str(e)


def test_app_runs_background_programs_through_the_windowless_twin():
    import os
    d = tmp()
    me, worker = d / "OrbBot", d / "OrbBotWorker"
    me.write_text(""); worker.write_text("")
    old_exe, old_frozen = sys.executable, A.FROZEN
    sys.executable, A.FROZEN = str(me), True
    try:
        assert A.runner() == str(worker)
        assert A.script_cmd("orb_bot.py", "--submit") == [str(worker), "--run", str(A.CODE / "orb_bot.py"), "--submit"]
        os.remove(worker)
        assert A.runner() == str(me)                                        # no twin: run through itself
    finally:
        sys.executable, A.FROZEN = old_exe, old_frozen


def test_app_window_is_a_plain_browser_window_with_its_own_profile():
    import orbbot_app as app
    cmd = app.window_cmd("msedge.exe", "http://127.0.0.1:8765", Path("C:/x/.window"))
    assert cmd[0] == "msedge.exe" and "--app=http://127.0.0.1:8765" in cmd
    assert any(c.startswith("--user-data-dir=") for c in cmd) and "--no-first-run" in cmd
    opened = []
    old_find, old_open = app.find_window_browser, app.webbrowser.open
    app.find_window_browser, app.webbrowser.open = (lambda: None), opened.append
    try:
        assert app.open_window("http://127.0.0.1:8765") is None and opened == ["http://127.0.0.1:8765"]
    finally:
        app.find_window_browser, app.webbrowser.open = old_find, old_open


def test_closing_the_app_stops_an_idle_watchdog_but_never_a_guarding_one():
    import orbbot_app as app
    calls = []
    class D:
        def __init__(self, bot): self.bot = bot
        def running(self, name): return {"bot": self.bot, "watchdog": 77}[name]
        def stop(self, name, force=False, by_user=True): calls.append((name, force, by_user))
    app.tidy_up(D(bot=None))
    assert calls == [("watchdog", True, False)]
    calls.clear()
    app.tidy_up(D(bot=1234))                                               # a bot is running: leave its guard alone
    assert calls == []
