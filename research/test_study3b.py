import hashlib, json, tempfile, shutil
from pathlib import Path
import study3, test_study3 as ts
tmp = Path(tempfile.mkdtemp()); ts.make(tmp)
study3.DATA, study3.HERE = tmp, tmp
i = [k for k, g in enumerate(study3.GRID) if g[0] == "F3_vwap_pullback"][0]
(tmp / "pick3.json").write_text(json.dumps({"pick": {"i": i, "name": study3.name(i)}}))
(tmp / "pick3.lock").write_text(hashlib.sha256((tmp / "pick3.json").read_bytes()).hexdigest())
study3.test(["AAA", "BBB", "SPY"], runs=100)
p = json.loads((tmp / "pick3.json").read_text()); p["pick"]["i"] = 0; (tmp / "pick3.json").write_text(json.dumps(p))
try:
    study3.test(["AAA"]); print("ERROR: tampering not caught")
except SystemExit as e:
    print("tampered pick refused:", e)
