import csv, os
d = os.path.dirname(os.path.abspath(__file__))
traj = os.path.join(d, "trajectories")

for fname in sorted(os.listdir(traj)):
    if not fname.endswith(".csv"): continue
    rows = list(csv.DictReader(open(os.path.join(traj, fname), encoding="utf-8")))
    if not rows: continue
    name = fname[:12]
    print(f"\n{'='*60}")
    print(f"视频: {name}  ({len(rows)} frames)")
    print(f"{'='*60}")
    # 状态切换
    prev = rows[0]["state"]
    for i, r in enumerate(rows):
        if r["state"] != prev:
            print(f"  f{i:>3} t={r['ts_ms']:>6}ms  {prev:20s} -> {r['state']:20s}  neck_pan={r['neck_pan']:>6} eye_pan={r['eye_pan']:>6} faces={r['n_faces']}")
            prev = r["state"]
    # 眼颈分配统计
    look = [r for r in rows if r["state"] == "LOOKING_AT_PERSON"]
    if look:
        eye_only = sum(1 for r in look if abs(float(r["eye_pan"])) > 10 and abs(float(r["neck_pan"])) < 3)
        neck_big = sum(1 for r in look if abs(float(r["neck_pan"])) > 5)
        print(f"  LOOKING帧={len(look)} 眼-only(>10°眼/<3°颈)={eye_only} 颈动(>5°)={neck_big}")
