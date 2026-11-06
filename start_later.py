"""
start_later.py - leave this running; at the chosen time it opens the dashboard, and the dashboard starts the bot.

    python start_later.py                 # opens the dashboard at 18:00 (this PC's clock)
    python start_later.py --at 17:30      # another time

While it waits it keeps the PC from sleeping (the screen may still turn off). At the chosen time it:
  1. lifts today's "you stopped the bot" hold (you are scheduling a start, so the hold no longer applies),
  2. opens the dashboard in its own minimized window (unless one is already running),
  3. stays until the bot's heartbeat shows it running (auto-start begins at 08:45 New York = 18:15 India time until
     Nov 1, 19:15 after), keeping the PC awake until then; then it closes by itself.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent


def keep_awake(on):
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | (0x00000001 if on else 0))
    except Exception:
        pass


def target_time(at, now=None):
    now = now or datetime.now()
    h, m = (int(x) for x in at.split(":"))
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return t if t > now else (t if (now - t) < timedelta(minutes=1) else t + timedelta(days=1))


def lift_todays_hold(path=None):
    """Remove the 'stopped by you today' note so the dashboard's auto-start may start the bot."""
    path = Path(path or HERE / "automation.json")
    try:
        a = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not a.get("user_stopped_on"):
        return False
    a["user_stopped_on"] = None
    path.write_text(json.dumps(a, indent=2), encoding="utf-8")
    return True


def dashboard_running(port=8765):
    try:
        urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:%d/api/status" % port,
                                                      headers={"Host": "localhost"}), timeout=3).read()
        return True
    except Exception:
        return False


def open_dashboard():
    if os.name == "nt":
        subprocess.Popen('start "ORB dashboard" /min cmd /k "%s" dashboard.py --no-browser' % sys.executable,
                         cwd=str(HERE), shell=True)
    else:
        subprocess.Popen([sys.executable, "dashboard.py", "--no-browser"], cwd=str(HERE))


def bot_running(max_age=180):
    try:
        hb = json.loads((HERE / "heartbeat.json").read_text())
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(hb["time"])).total_seconds()
        return age < max_age and not hb.get("stopped")
    except Exception:
        return False


def main():
    at = sys.argv[sys.argv.index("--at") + 1] if "--at" in sys.argv else "18:00"
    t = target_time(at)
    print("Will open the dashboard at %s (this PC's time). Keeping the PC awake until then. Leave this window open."
          % t.strftime("%a %H:%M"))
    keep_awake(True)
    try:
        while datetime.now() < t:
            left = t - datetime.now()
            print("\r  %s to go   " % str(left).split(".")[0], end="", flush=True)
            time.sleep(min(30, max(1, left.total_seconds())))
        print("\n%s  It's time." % datetime.now().strftime("%H:%M"))
        if lift_todays_hold():
            print("  Lifted today's 'stopped by you' hold, so auto-start may start the bot.")
        if not bot_running() and (HERE / "STOP").exists():
            (HERE / "STOP").unlink()                 # a leftover from the last stop; it only matters to a running bot
            print("  Removed a leftover STOP file.")
        if dashboard_running():
            print("  The dashboard is already running.")
        else:
            open_dashboard()
            print("  Dashboard opened (minimized window 'ORB dashboard').")
        print("  Waiting for the bot to start (auto-start: 08:45 New York). Up to 2 hours.")
        deadline = datetime.now() + timedelta(hours=2)
        while datetime.now() < deadline:
            if bot_running():
                print("  %s  The bot is running. This window can close; the dashboard keeps the PC awake now."
                      % datetime.now().strftime("%H:%M"))
                break
            time.sleep(30)
        else:
            print("  The bot did not start within 2 hours. Check the dashboard window, or send /start on Telegram.")
    finally:
        keep_awake(False)
    time.sleep(10)


if __name__ == "__main__":
    main()
