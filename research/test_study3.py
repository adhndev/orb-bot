import gzip, json, shutil, tempfile
from pathlib import Path
import numpy as np, pandas as pd
import lab, study3

def make(tmp, syms=("SPY", "AAA", "BBB")):
    rng = np.random.default_rng(5)
    days = [d for y in range(2016, 2027) for d in pd.bdate_range(f"{y}-02-01", f"{y}-03-15")]
    for s in syms:
        rows, idx, p = [], [], 100.0
        for d in days:
            ts = pd.date_range(f"{d.date()} 09:30", periods=390, freq="1min", tz="America/New_York")
            for t in ts:
                o = p; p = p * (1 + rng.normal(0, 0.0012)); rows.append((o, max(o, p) * 1.0004, min(o, p) * 0.9996, p, 1000.0)); idx.append(t)
        df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=pd.DatetimeIndex(idx))
        with gzip.open(tmp / f"{s}.csv.gz", "wt") as f:
            df.to_csv(f, index_label="timestamp")

if __name__ == "__main__":
  tmp = Path(tempfile.mkdtemp())
  make(tmp)
  study3.DATA, study3.HERE = tmp, tmp
  shutil.copy(Path(__file__).parent / "PROTOCOL_3.md", tmp / "PROTOCOL_3.md")
  syms = ["AAA", "BBB", "SPY"]
  study3.MIN_DISC_TRADES, study3.MIN_VAL_TRADES = 30, 5
  study3.choose(syms)                                           # pure noise: must stop at the luck test
  pick = json.loads((tmp / "pick3.json").read_text())
  print("\nNO-EDGE DATA -> luck p =", round(pick["luck_p"], 3), "| pick:", pick["pick"])
  assert pick["pick"] is None
  try:
      study3.test(syms); raise AssertionError("test must refuse without a pick")
  except SystemExit as e:
      print("test refused:", e)
  real_rc = lab.reality_check
  lab.reality_check = lambda S, n_boot=1000, eligible=None: (0.01, 9.9, None)   # force past the gate to exercise the rest
  study3.MIN_VAL_TRADES = 0
  orig_stats = lab.stats_from_days
  study3.choose(syms)
  pick = json.loads((tmp / "pick3.json").read_text())
  print("forced path -> pick:", pick["pick"] and pick["pick"]["name"])
  if pick["pick"]:
      study3.test(syms, runs=50)
      print((tmp / "study3_test.txt").read_text())
      p = json.loads((tmp / "pick3.json").read_text()); p["pick"]["i"] = 0
      (tmp / "pick3.json").write_text(json.dumps(p))
      try:
          study3.test(syms); raise AssertionError("a changed pick must be refused")
      except SystemExit as e:
          print("tampered pick refused:", e)
  lab.reality_check = real_rc
