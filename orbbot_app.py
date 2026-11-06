"""
orbbot_app.py - the entry point of the packaged app (OrbBot.exe). Not needed when you run the bot with Python.

    OrbBot.exe                       open the app in its own window - this is what a double-click does
                                     (OrbBotWorker.exe is its window-less twin that runs the bot in the background)
    OrbBot.exe --import "C:\\...\\Orb_bot"   copy the journal, logs, scorecard and research data from the old folder
    OrbBot.exe --selftest            check the app is complete (used by build_app.bat)
    OrbBot.exe --run <script> ...    internal: how the dashboard starts the bot, the watchdog and the nightly
                                     learning without a separate Python

Everything that changes is kept in Documents\\OrbBot. Keys are entered in Settings (bottom of the dashboard) and
kept in Windows Credential Manager.
"""
import os
import runpy
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

import apphome

# The libraries the bot's programs need are listed in OrbBot.spec (they run from source, so the builder cannot
# see their imports by itself).


def run_script(argv):
    """Run one of the bot's programs (from the app's own copy of the source) as if it were 'python <script> ...'."""
    if not argv:
        sys.exit("--run needs a script")
    script = Path(argv[0])
    if not script.is_absolute():
        script = apphome.CODE / script
    sys.argv = [str(script), *argv[1:]]
    for p in (str(apphome.CODE / "research"), str(apphome.CODE), str(script.parent)):
        if p not in sys.path:
            sys.path.insert(0, p)
    runpy.run_path(str(script), run_name="__main__")


def selftest():
    """Import everything the bot uses and check its files are inside the app. Prints OK or what is missing."""
    problems = []
    for name in ["apphome", "orb_bot", "advisor", "dashboard", "journal", "evaluate", "engine", "notify", "skills",
                 "stream", "strategies", "watchdog", "alpaca.trading.client", "alpaca.data.historical",
                 "alpaca.data.historical.news", "alpaca.data.live", "alpaca.trading.stream", "anthropic", "numpy",
                 "pandas", "requests", "truststore", "keyring"]:
        try:
            __import__(name)
        except Exception as e:
            problems.append("import %s: %s: %s" % (name, type(e).__name__, e))
    sys.path.insert(0, str(apphome.CODE / "research"))
    for name in ["lab", "daily"]:
        try:
            __import__(name)
        except Exception as e:
            problems.append("import research/%s: %s: %s" % (name, type(e).__name__, e))
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("America/New_York")
    except Exception as e:
        problems.append("time zones: %s" % e)
    import notify
    if notify.tls() is None:
        problems.append("truststore: Telegram certificate checks would not use Windows' list")
    if os.name == "nt" and not apphome.VAULT.available():
        problems.append("Windows Credential Manager not reachable (keyring)")
    needed = ["orb_bot.py", "watchdog.py", "research/daily.py", "research/lab.py", "config.json",
              "success_criteria_gap.json"]
    if (apphome.CODE / "skills").is_dir():          # the trader-* skills are optional (not in the public repository)
        needed += ["skills/trader-risk/check.py", "skills/trader-news/news.py"]
    for rel in needed:
        if not (apphome.CODE / rel).exists():
            problems.append("missing in the app: %s" % rel)
    import subprocess
    try:   # the dispatch the dashboard uses: start a program through the app itself
        p = subprocess.run(apphome.script_cmd("orb_bot.py", "--help"), capture_output=True, text=True, timeout=120)
        if p.returncode != 0 or "--submit" not in p.stdout:
            problems.append("starting orb_bot.py through the app failed: %s" % (p.stderr or p.stdout)[-300:])
    except Exception as e:
        problems.append("starting orb_bot.py through the app failed: %s" % e)
    print("Data folder:", apphome.HOME)
    if problems:
        print("SELFTEST FAILED:\n  " + "\n  ".join(problems))
        return 1
    print("SELFTEST OK")
    return 0


def dashboard_running(port):
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/api/status" % port, timeout=2).read()
        return True
    except Exception:
        return False


EDGE = [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        r"%LocalAppData%\Google\Chrome\Application\chrome.exe"]


def find_window_browser():
    """Microsoft Edge (part of Windows 10/11) or Chrome, used in 'app mode': a plain window, no tabs or address bar."""
    for p in EDGE:
        path = os.path.expandvars(p)
        if "%" not in path and Path(path).exists():
            return path
    for name in ("msedge", "microsoft-edge", "google-chrome", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def window_cmd(browser, url, profile):
    return [browser, "--app=%s" % url, "--user-data-dir=%s" % profile, "--no-first-run", "--no-default-browser-check",
            "--window-size=1280,900", "--disable-features=Translate"]


def open_window(url, wait=True):
    """Show the dashboard in its own window. Returns when that window is closed (or at once if wait is False).
    The window keeps its own small profile in the app's data folder, so it never mixes with your normal browsing."""
    browser = find_window_browser()
    if not browser:
        webbrowser.open(url)
        return None
    profile = apphome.HOME / ".window"
    flags = 0x08000000 if os.name == "nt" else 0
    p = subprocess.Popen(window_cmd(browser, url, profile), creationflags=flags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if wait:
        p.wait()
    return p


def quiet_output():
    """The window app has no console: keep anything it prints in Documents\\OrbBot\\app.log instead."""
    if sys.stdout is None or sys.stderr is None:
        f = open(apphome.HOME / "app.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or f
        sys.stderr = sys.stderr or f


def main():
    args = sys.argv[1:]
    if args[:1] == ["--run"]:
        return run_script(args[1:])
    if args[:1] == ["--selftest"]:
        sys.exit(selftest())
    apphome.seed_home()
    if args[:1] == ["--import"]:
        if len(args) < 2:
            sys.exit('Usage: OrbBot.exe --import "C:\\path\\to\\Orb_bot"')
        n, size = apphome.import_from(args[1])
        print("Imported %d files (%.1f MB) into %s" % (n, size / 1e6, apphome.HOME))
        return
    os.chdir(apphome.HOME)
    quiet_output()
    import dashboard
    url = "http://127.0.0.1:%d" % dashboard.PORT
    if dashboard_running(dashboard.PORT):       # already open: just show another window onto it
        open_window(url, wait=False)
        return
    if "--console" in args:                      # the old way: dashboard in this console, page in your browser
        return dashboard.main()
    print("%s ORB bot app started. Data folder: %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), apphome.HOME))
    dashboard.start_in_background()
    while not dashboard_running(dashboard.PORT):
        time.sleep(0.2)
    win, opened = open_window(url, wait=False), time.time()
    while not dashboard.QUIT.is_set():           # until the window is closed or "Close app" is pressed
        if win is not None and win.poll() is not None:
            if time.time() - opened < 5:         # the browser handed the window to one already open: we cannot see
                win = None                       # it close, so the app stays until "Close app" is pressed
                continue
            break
        time.sleep(0.5)
    if win is not None and win.poll() is None:
        win.terminate()
    tidy_up(dashboard)
    print("%s ORB bot app closed." % time.strftime("%Y-%m-%d %H:%M:%S"))


def tidy_up(dashboard):
    """On closing: if no bot is running, its watchdog has nothing to guard, so stop it too (a leftover one would
    also keep the app's files locked). A running bot keeps its watchdog."""
    try:
        if not dashboard.running("bot") and dashboard.running("watchdog"):
            dashboard.stop("watchdog", force=True, by_user=False)
            print("Stopped the watchdog (no bot running).")
    except Exception as e:
        print("Could not stop the watchdog: %s" % e)


if __name__ == "__main__":
    main()
