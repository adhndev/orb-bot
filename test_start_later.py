"""start_later.py: the time maths, lifting today's hold, and clearing a leftover STOP only when no bot runs."""
import json, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import start_later as sl


def test_target_time():
    now = datetime(2026, 10, 29, 13, 45)
    assert sl.target_time("18:00", now) == datetime(2026, 10, 29, 18, 0)
    assert sl.target_time("09:00", now) == datetime(2026, 10, 30, 9, 0)          # already past: tomorrow
    assert sl.target_time("13:45", now.replace(second=20)) == datetime(2026, 10, 29, 13, 45)   # just now: now


def test_hold_and_heartbeat():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "automation.json").write_text(json.dumps({"autostart": True, "user_stopped_on": "2026-10-29"}))
    assert sl.lift_todays_hold(tmp / "automation.json") is True
    assert json.loads((tmp / "automation.json").read_text())["user_stopped_on"] is None
    assert sl.lift_todays_hold(tmp / "automation.json") is False
    old = sl.HERE
    sl.HERE = tmp
    try:
        now = datetime.now(timezone.utc)
        (tmp / "heartbeat.json").write_text(json.dumps({"time": now.isoformat(), "stopped": None}))
        assert sl.bot_running()
        (tmp / "heartbeat.json").write_text(json.dumps({"time": now.isoformat(), "stopped": "user (STOP file)"}))
        assert not sl.bot_running()
        (tmp / "heartbeat.json").write_text(json.dumps({"time": (now - timedelta(minutes=10)).isoformat(), "stopped": None}))
        assert not sl.bot_running()
        assert sl.dashboard_running(port=1) is False
    finally:
        sl.HERE = old
