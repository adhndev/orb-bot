# OrbBot.spec - how build_app.bat packs the bot into OrbBot.exe (PyInstaller, one folder).
# The bot's own programs are packed as source files and run by the app itself (OrbBot.exe --run <script>),
# so every library they use is listed here.
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

ROOT = Path(SPECPATH)

# The bot's programs and the files it needs on first start (copied into Documents\OrbBot if missing).
CODE = ["apphome.py", "orb_bot.py", "advisor.py", "dashboard.py", "journal.py", "evaluate.py", "engine.py",
        "notify.py", "skills.py", "stream.py", "strategies.py", "watchdog.py",
        "research/daily.py", "research/lab.py"]
SEED = ["config.json", "success_criteria.json", "success_criteria_gap.json", "criteria.lock", "criteria_gap.lock",
        "inputs.example.json", "research/forward_watch.json", "research/pick3.json", "research/pick3.lock"]

datas = []
for rel in CODE + SEED:
    if (ROOT / rel).exists():
        datas.append((str(ROOT / rel), str(Path(rel).parent)))
for path in (ROOT / "skills").rglob("*"):
    if path.is_file() and "__pycache__" not in path.parts and path.suffix in (".py", ".json"):
        datas.append((str(path), str(path.parent.relative_to(ROOT))))
datas += collect_data_files("tzdata") + collect_data_files("certifi") + collect_data_files("anthropic")

hidden = (collect_submodules("alpaca") + collect_submodules("anthropic") + collect_submodules("truststore")
          + ["keyring", "keyring.backends", "requests", "numpy", "pandas",
             "msgpack", "websockets", "pydantic", "email.utils", "xml.etree.ElementTree", "statistics", "queue",
             "http.server", "secrets", "webbrowser", "zoneinfo", "gzip", "csv", "hashlib", "random", "argparse"])
if os.name == "nt":
    hidden += ["keyring.backends.Windows", "win32ctypes", "win32ctypes.core"]

a = Analysis(
    [str(ROOT / "orbbot_app.py")],
    pathex=[str(ROOT), str(ROOT / "research")],
    datas=datas,
    hiddenimports=hidden,
    excludes=["numba", "llvmlite", "pytest", "tkinter", "matplotlib", "IPython", "notebook", "scipy"],
    noarchive=False,
)
pyz = PYZ(a.pure)
# OrbBot.exe: the app, in its own window (no console). OrbBotWorker.exe: the same program without a window, which
# runs the bot, the watchdog, the nightly learning and the skills in the background (and prints --selftest results).
app = EXE(pyz, a.scripts, [], exclude_binaries=True, name="OrbBot", console=False, upx=False)
worker = EXE(pyz, a.scripts, [], exclude_binaries=True, name="OrbBotWorker", console=True, upx=False)
coll = COLLECT(app, worker, a.binaries, a.datas, name="OrbBot", upx=False)
