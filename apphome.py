"""
apphome.py - where the bot keeps its files and keys, and how it starts its helper programs.

Two ways to run:
  * From the project folder with Python (python dashboard.py): code and data live together in the project folder,
    exactly as before. Keys come from environment variables, or from Settings in the dashboard.
  * As the packaged app (OrbBot.exe, no Python needed): the code is inside the app; everything that changes
    (journal, logs, settings, scorecard, research data) lives in  Documents\\OrbBot , like a game's save files.
    Keys entered in Settings are kept in Windows Credential Manager, never in a file.

ORB_HOME (environment variable) overrides the data folder in both cases.
"""
import json
import os
import shutil
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
CODE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))   # the program files (read-only in the app)
APP_NAME = "OrbBot"


def documents_folder():
    """The real Documents folder (it may have been moved, e.g. into OneDrive), falling back to ~/Documents."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]

            docs = GUID(0xFDD39AD0, 0x238F, 0x46AF, (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
            out = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(docs), 0, None, ctypes.byref(out)) == 0:
                path = Path(out.value)
                ctypes.windll.ole32.CoTaskMemFree(out)
                return path
        except Exception:
            pass
    return Path.home() / "Documents"


def _home():
    if os.environ.get("ORB_HOME"):
        return Path(os.environ["ORB_HOME"])
    return documents_folder() / APP_NAME if FROZEN else CODE


HOME = _home()                                    # everything that changes: journal, logs, state, settings, data


def runner():
    """In the app: OrbBotWorker.exe, the window-less twin of OrbBot.exe that runs the bot's programs in the
    background (OrbBot.exe itself is the window). Falls back to the running program."""
    me = Path(sys.executable)
    worker = me.with_name("OrbBotWorker" + me.suffix)
    return str(worker) if worker.exists() else str(me)


def script_cmd(script, *args):
    """The command that runs one of the bot's programs (orb_bot.py, watchdog.py, research/daily.py, a skill script).
    In the app there is no python.exe: the app runs the script itself (OrbBotWorker.exe --run <script> ...)."""
    path = Path(script)
    if not path.is_absolute():
        path = CODE / path
    if FROZEN:
        return [runner(), "--run", str(path), *map(str, args)]
    return [sys.executable, str(path), *map(str, args)]


# Files the app needs in its data folder before the first start, copied from the app if missing. Never overwritten.
SEED = ["config.json", "success_criteria.json", "success_criteria_gap.json", "criteria.lock", "criteria_gap.lock",
        "inputs.example.json", "research/forward_watch.json", "research/pick3.json", "research/pick3.lock"]


def seed_home(home=None, code=None):
    home, code = Path(home or HOME), Path(code or CODE)
    home.mkdir(parents=True, exist_ok=True)
    (home / "research").mkdir(exist_ok=True)
    copied = []
    if home.resolve() == code.resolve():
        return copied
    for rel in SEED:
        src, dst = code / rel, home / rel
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(rel)
    return copied


# ---------------------------------------------------------------- bringing history over from the old folder
SKIP_DIRS = {"__pycache__", ".pytest_cache", "skills", "deploy", "bt_cache", "build", "dist", "build_env", ".git",
             "train", "qqq2020"}
SKIP_EXT = {".py", ".pyc", ".bat", ".spec", ".sha256", ".jpg", ".png", ".tmp", ".zip", ".exe"}
SKIP_NAMES = {"requirements.txt", "dashboard_pids.json", "heartbeat.json", "watchdog.json", "STOP",
              "bot_console.log", "watchdog_console.log"}


def _wanted(rel):
    parts = rel.parts
    if any(p in SKIP_DIRS for p in parts[:-1]):
        return False
    name = parts[-1]
    if name in SKIP_NAMES or Path(name).suffix.lower() in SKIP_EXT:
        return False
    if name.startswith(("t_", "test_")):                       # files the tests leave behind
        return False
    if name == "README.md" or name.startswith("PROTOCOL"):      # documentation that ships with the app
        return False
    return True


def import_from(src, home=None):
    """Copy the journal, logs, state, settings and research data from an old bot folder into the data folder.
    Program files are not copied (the app has its own). Existing files are replaced by the old folder's copy."""
    src, home = Path(src), Path(home or HOME)
    if not src.is_dir():
        raise ValueError("Folder not found: %s" % src)
    if not ((src / "orb_bot.py").exists() or (src / "journal.csv").exists() or (src / "config.json").exists()):
        raise ValueError("That folder does not look like the bot's folder (no journal.csv, config.json or orb_bot.py).")
    if src.resolve() == home.resolve():
        raise ValueError("That is already the data folder.")
    n, size = 0, 0
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if path.is_dir() or not _wanted(rel):
            continue
        dst = home / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dst)
        n += 1
        size += path.stat().st_size
    return n, size


# ---------------------------------------------------------------- keys
KEYS = [("ALPACA_API_KEY", "Alpaca paper key ID"), ("ALPACA_SECRET_KEY", "Alpaca paper secret"),
        ("ANTHROPIC_API_KEY", "Claude (Anthropic) API key"), ("TELEGRAM_BOT_TOKEN", "Telegram bot token"),
        ("TELEGRAM_CHAT_ID", "Telegram chat ID")]
KEY_NAMES = [k for k, _ in KEYS]
SERVICE = APP_NAME


class Vault:
    """Windows Credential Manager (the locked store Windows uses for saved passwords), through the 'keyring' package.
    Elsewhere: whatever keyring finds. If keyring is missing, saving is refused (nothing is ever written to a file)."""

    def __init__(self, backend=None):
        self._backend = backend

    def backend(self):
        if self._backend is None:
            try:
                if os.name == "nt":
                    from keyring.backends.Windows import WinVaultKeyring   # direct: no plugin discovery in the app
                    self._backend = WinVaultKeyring()
                else:
                    import keyring
                    self._backend = keyring.get_keyring()
            except Exception:
                self._backend = False
        return self._backend or None

    def available(self):
        return self.backend() is not None

    def get(self, name):
        b = self.backend()
        try:
            return b.get_password(SERVICE, name) if b else None
        except Exception:
            return None

    def set(self, name, value):
        b = self.backend()
        if not b:
            raise RuntimeError("No safe key store on this computer (install the 'keyring' package).")
        if value:
            b.set_password(SERVICE, name, value)
        else:
            try:
                b.delete_password(SERVICE, name)
            except Exception:
                pass


VAULT = Vault()
_SOURCE = {}                    # key name -> "environment" | "saved in Windows" (what Settings shows)


def load_keys(vault=None, env=None):
    """Fill any key that is not already in the environment from the vault. Environment variables win, so the
    Python version keeps working exactly as before."""
    vault, env = vault or VAULT, os.environ if env is None else env
    for name in KEY_NAMES:
        if (env.get(name) or "").strip():
            _SOURCE.setdefault(name, "environment")
            continue
        value = vault.get(name)
        if value:
            env[name] = value
            _SOURCE[name] = "saved in Windows"


def save_keys(values, vault=None, env=None):
    """Save the keys you typed (blank fields are left alone) and use them from now on in this app."""
    vault, env = vault or VAULT, os.environ if env is None else env
    saved = []
    for name in KEY_NAMES:
        value = (values.get(name) or "").strip().strip('"').strip("'").strip()
        if not value:
            continue
        vault.set(name, value)
        env[name] = value
        _SOURCE[name] = "saved in Windows"
        saved.append(name)
    return saved


def key_status(env=None):
    """For Settings: which keys are set and where they come from. Never the values."""
    env = os.environ if env is None else env
    out = []
    for name, label in KEYS:
        v = (env.get(name) or "").strip()
        out.append({"name": name, "label": label, "set": bool(v),
                    "source": _SOURCE.get(name, "environment") if v else None,
                    "hint": ("…" + v[-4:]) if v and name in ("ALPACA_API_KEY", "TELEGRAM_CHAT_ID") else None})
    return out


def info():
    return {"frozen": FROZEN, "home": str(HOME), "code": str(CODE), "vault": VAULT.available()}


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")
