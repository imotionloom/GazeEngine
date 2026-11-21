#!/usr/bin/env python
"""10 分钟连跑分析：读 runtime_log.csv，按阈值判定 PASS/FAIL。

配合 tools/accept_10min.ps1 使用；阈值集中在本文件前面的常量里。

用法：
    python tools/accept_analyze.py [--csv runtime_log.csv]
"""
import csv, sys, os, argparse, statistics

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=os.path.join(os.path.dirname(__file__), "..", "runtime_log.csv"))
    args = ap.parse_args()

    path = os.path.normpath(args.csv)
    if not os.path.exists(path):
        print(f"FAIL: {path} 不存在")
        sys.exit(1)

    rows = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                rows.append({
                    "ts": float(r["ts_ms"]),
                    "dt": float(r["dt_ms"]),
                    "state": r["state"],
                    "neck_pan": float(r["neck_pan"]),
                    "neck_tilt": float(r["neck_tilt"]),
                    "eye_pan": float(r["eye_pan"]),
                    "eye_tilt": float(r["eye_tilt"]),
                    "lid": float(r["lid"]),
                })
            except (ValueError, KeyError):
                continue

    if len(rows) < 100:
        print(f"FAIL: 只有 {len(rows)} 行数据，太少")
        sys.exit(1)

    duration_s = (rows[-1]["ts"] - rows[0]["ts"]) / 1000.0
    fps = len(rows) / max(1, duration_s)

    # dt percentiles
    dts = sorted(r["dt"] for r in rows)
    dt_p50 = statistics.median(dts)
    dt_p95 = dts[int(len(dts)*0.95)]
    dt_p99 = dts[int(len(dts) * 0.99)]

    # single-frame deltas
    d_pan = [abs(rows[i+1]["neck_pan"] - rows[i]["neck_pan"]) for i in range(len(rows)-1)]
    d_lid = [abs(rows[i+1]["lid"] - rows[i]["lid"]) for i in range(len(rows)-1)]
    max_d_pan = max(d_pan) if d_pan else 0
    max_d_lid = max(d_lid) if d_lid else 0

    # short-lived states: 进入某状态后 <1s 就切走 = 抖动
    # RETURN 是刻意的瞬时标记态（1帧），不计入抖动
    flips = 0
    cur_state = None
    enter_ts = 0
    for r in rows:
        if r["state"] != cur_state:
            if (cur_state is not None
                    and cur_state != "RETURN"
                    and (r["ts"] - enter_ts) < 1000):
                flips += 1
            cur_state = r["state"]
            enter_ts = r["ts"]

    print(f"=== 10 分钟连跑（soak）===")
    print(f"数据行数     : {len(rows)}")
    print(f"运行时长     : {duration_s:.1f}s")
    print(f"fps 平均     : {fps:.1f}")
    print(f"dt p50/p95/p99: {dt_p50:.0f}/{dt_p95:.0f}/{dt_p99:.0f}ms")
    print(f"max Δneck_pan: {max_d_pan:.2f}°")
    print(f"max Δlid     : {max_d_lid:.2f}°")
    print(f"<1s 状态翻转 : {flips} 次")
    print()

    checks = [
        ("fps >= 22",          fps >= 22,           f"{fps:.1f}"),
        ("dt p95 <= 80ms",      dt_p95 <= 80,        f"{dt_p95:.1f}"),
        ("dt p99 <= 200ms",     dt_p99 <= 200,       f"{dt_p99:.1f}"),
        ("max Δpan <= 8°",     max_d_pan <= 8.0,    f"{max_d_pan:.2f}"),
        ("max Δlid <= 15°",    max_d_lid <= 15.0,   f"{max_d_lid:.2f}"),
        ("状态抖动 <= 10次",   flips <= 10,         f"{flips}"),
    ]

    all_pass = True
    for name, ok, val in checks:
        mark = "PASS" if ok else "FAIL"
        if not ok: all_pass = False
        print(f"  [{mark}] {name:25s} = {val}")

    print()
    if all_pass:
        print("=== 总评: PASS ===")
        sys.exit(0)
    else:
        print("=== 总评: FAIL ===")
        sys.exit(1)

if __name__ == "__main__":
    main()
