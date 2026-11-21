"""随机审计采集器：无相机复刻 P2 主链路，输出 JSONL（meta / frame / event）。

为什么要复刻主链路而不是只跑 PresenceController：
  真正决定"每次启动像不像"的是 FSM(42) + Blink(7) + rng 单例 + presence 四者叠加，
  只测 presence 会漏掉前三个。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import platform
import random
import subprocess
import sys
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from p2_decision import rng as R                       # noqa: E402
from p2_decision.dynamics import BlinkMachine          # noqa: E402
from p2_decision.geometry import GeometrySolver        # noqa: E402
from p2_decision.presence import FAMILIES, SPEC_RANGES  # noqa: E402
from p2_decision.state_machine import GazeStateMachine  # noqa: E402

FPS = 30


def git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True,
            stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "nogit"


def _face(u: float, v: float):
    tracks = [{"track_id": 1, "conf": 0.9, "bbox_center": (u, v)}]
    anchors = {1: {"eye_L": (u - 20, v), "eye_R": (u + 20, v),
                   "nose": (u, v + 10), "mouth": (u, v + 30)}}
    return tracks, anchors


def scenario(name: str):
    """返回 f(t_sec) -> (tracks, anchors)。全部只依赖 t，不引入额外随机。"""
    if name == "idle":
        return lambda t: ([], {})

    if name == "social":
        def f(t):
            cyc = t % 30.0                       # 20s 有人 / 10s 无人
            if cyc >= 20.0:
                return [], {}
            u = 320 + 70 * math.sin(t * 0.45)
            v = 240 + 25 * math.cos(t * 0.31)
            return _face(u, v)
        return f

    # mixed（默认）：长短交替，覆盖 SEEKING/THINKING/RECOVERING/DOZING
    def f(t):
        cyc = t % 120.0
        if cyc < 25.0:                           # 有人
            u = 320 + 80 * math.sin(t * 0.4)
            v = 240 + 20 * math.sin(t * 0.23)
            return _face(u, v)
        return [], {}                            # 无人 95s
    return f


def main(argv=None):
    ap = argparse.ArgumentParser(prog="random_audit.collect")
    ap.add_argument("--duration", type=float, default=60.0, help="秒")
    ap.add_argument("--seed", type=int, default=None, help="不填=OS 熵（跨启动应不同）")
    ap.add_argument("--scenario", default="mixed", choices=["idle", "social", "mixed"])
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    run_id = uuid.uuid4().hex[:8]
    scen = scenario(args.scenario)

    # --- 种子注入（不修改被审计的模块）------------------------------
    if args.seed is not None:
        os.environ["GAZE_SEED"] = str(args.seed)  # 构造前设env，让_mode_until用正确seed
        R._rng.seed(args.seed)                   # 模块级单例
    fsm = GazeStateMachine()
    geom = GeometrySolver()
    blink = BlinkMachine()
    if args.seed is not None:
        # 构造后再reseed其他组件（_mode_until已在构造时正确计算）
        blink._rng.seed(args.seed)               # 眨眼间隔随机源
        geom.presence._rng.seed(args.seed)
        # seed 所有 family 的 _think_rng（THINKING 轨迹随机源）
        for fam in geom.presence.fams.values():
            fam["dims"]._think_rng = random.Random(args.seed)
        geom.presence._preset = None
        geom.presence._active_ranges = None
        geom.presence.fams["IDLE"]["dims"].resample(SPEC_RANGES["IDLE"], geom.presence._rng)

    meta = {
        "run_id": run_id,
        "seed": args.seed,
        "scenario": args.scenario,
        "duration": args.duration,
        "fps": FPS,
        "git_sha": git_sha(),
        "python": platform.python_version(),
        "platform": platform.platform(terse=True),
        "timestamp": None,                       # 不写墙钟，避免影响 M22 逐字节比较
    }

    n = int(args.duration * FPS)
    prev_fam = prev_state = prev_src = None
    frames = 0
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "meta", **meta}, ensure_ascii=False) + "\n")
        for i in range(n):
            ts = int(i * 1000 / FPS)
            tracks, anchors = scen(ts / 1000.0)
            intent = fsm.update(tracks, ts)
            g = geom.solve(intent, anchors, ts)
            lid_open = blink.update(ts)
            fam = geom.presence.current

            for kind, new, old in (("family", fam, prev_fam),
                                   ("state", intent["state"], prev_state),
                                   ("source", g.get("source"), prev_src)):
                if new != old:
                    fh.write(json.dumps({"type": "event", "ts": ts, "kind": kind,
                                         "from": old, "to": new}) + "\n")
            prev_fam, prev_state, prev_src = fam, intent["state"], g.get("source")

            dims = g.get("dims") or FAMILIES[fam]["dims"].as_dict()
            fh.write(json.dumps({
                "type": "frame", "ts": ts,
                "family": fam, "state": intent["state"], "source": g.get("source"),
                "neck_pan": round(float(g["body_pan_deg"]), 3),
                "neck_tilt": round(float(g["body_tilt_deg"]), 3),
                "eye_pan": round(float(g["eye_pan_deg"]), 3),
                "eye_tilt": round(float(g["eye_tilt_deg"]), 3),
                "lid": round(1.0 - float(g.get("lid_closure_deg", 0.0)) / 95.0, 3),
                "blink_open": round(float(lid_open), 3),
                "dims": dims,
            }) + "\n")
            frames += 1

    print(f"[audit] {args.out}  run={run_id}  seed={args.seed}  frames={frames}  "
          f"scenario={args.scenario}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
