"""理想随机基线：用合成数据跑同一套指标，得到阈值参照。

    python -m random_audit.baseline --runs 6 --duration 60

输出每个指标的基线值；阈值取基线 x1.5，避免拍脑袋。
"""
from __future__ import annotations

import argparse
import math
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from random_audit import analyzer as A  # noqa: E402

FPS = 30


def synth(seed: int, duration: float = 60.0, fps: int = FPS):
    """理想随机：族驻留指数分布、neck 为 OU+白噪、无任何固定频率。"""
    rng = random.Random(seed)
    n = int(duration * fps)
    frames = []
    fams = ["IDLE", "SCANNING", "SEEKING", "THINKING", "RECOVERING"]
    fam = fams[0]
    left = rng.expovariate(1 / 8.0)
    neck = tilt = eye = 0.0
    tau = 10.0
    for i in range(n):
        dt = 1.0 / fps
        if left <= 0:
            fam = fams[rng.randrange(len(fams))]
            left = rng.expovariate(1 / 8.0)
        left -= dt
        a = math.exp(-dt / tau)
        neck = a * neck + rng.gauss(0, 1.2) * math.sqrt(1 - a * a)
        tilt = a * tilt + rng.gauss(0, 0.6) * math.sqrt(1 - a * a)
        eye = a * eye + rng.gauss(0, 2.0) * math.sqrt(1 - a * a)
        frames.append({"ts": int(i * 1000 / fps), "family": fam,
                       "state": "SCANNING", "source": "idle",
                       "neck_pan": neck, "neck_tilt": tilt,
                       "eye_pan": eye, "eye_tilt": 0.0,
                       "lid": 0.9 + 0.05 * math.sin(i * 0.01)})
    return {"seed": seed, "duration": duration}, frames


def main(argv=None):
    ap = argparse.ArgumentParser(prog="random_audit.baseline")
    ap.add_argument("--runs", type=int, default=6)
    ap.add_argument("--duration", type=float, default=60.0)
    args = ap.parse_args(argv)

    runs = [synth(1000 + i, args.duration) for i in range(args.runs)]
    m13 = A.m13_cross_run(runs)
    print(f"基线 M13  mean={m13['mean']:.2f} max={m13['max']:.2f}")

    m14 = [max(abs(v) for v in A.m14_autocorr(f).values() or [0]) for _, f in runs]
    if m14:
        print(f"基线 M14  max|acf|={max(m14):.2f}")
    else:
        print("基线 M14  样本不足")

    m16 = []
    for _, f in runs:
        got = {k: v for k, v in A.m16_long_window(f).items() if v is not None}
        if got:
            m16.append(max(got.values()))
    print(f"基线 M16  max={max(m16):.2f}" if m16 else "基线 M16  样本不足")

    m18 = [A.m18_spectrum(f).get("peak_over_baseline", 0) for _, f in runs]
    m18 = [v for v in m18 if v]
    print(f"基线 M18  峰/基线 max={max(m18):.1f}" if m18 else "基线 M18  样本不足")

    m21 = [A.m21_turning_interval(f).get("cv") for _, f in runs]
    m21 = [v for v in m21 if v is not None]
    print(f"基线 M21  拐点 CV 均值={sum(m21)/len(m21):.2f}" if m21 else "基线 M21  样本不足")

    print("\n建议阈值 = 基线 x1.5（M15/M21 取基线的 [0.7, 1.4] 区间）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
