"""诊断报告 + 决策树。

    python -m random_audit.report cross_*.jsonl              # 跨启动
    python -m random_audit.report --pair a.jsonl b.jsonl     # 可复现性(M22)
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from typing import List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from random_audit import analyzer as A  # noqa: E402

# 阈值默认值；可用 random_audit/thresholds.json 或 --config 覆盖。
# 基线（理想随机）实测：M13 0.11 / M14 0.03 / M18 1.9 / M21 0.80
TH = {
    "m13_max": 0.60,
    "m14_abs": 0.50,
    "m15_cv_min": 0.15,
    "m16_max": 0.85,
    "m18_ratio": 20.0,
    "m20_ratio": 1.5,
    "m21_cv": [0.6, 1.6],
}


def load_thresholds(path=None):
    """合并默认阈值与外部 JSON（后写覆盖先写）。"""
    cfg = dict(TH)
    if path is None:
        path = os.path.join(HERE, "thresholds.json")
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8-sig") as fh:   # 容忍 PowerShell 写入的 BOM
                data = json.load(fh)
            for k, v in data.items():
                if k.startswith("_"):
                    continue
                if k in cfg:
                    cfg[k] = tuple(v) if isinstance(v, list) else v
        except Exception as exc:  # 配置坏了不要静默
            print(f"[warn] 读取阈值配置失败 {path}: {exc}")
    return cfg


def _load_many(paths: List[str]) -> List[Tuple[dict, List[dict]]]:
    out = []
    for p in paths:
        meta, frames = A.load(p)
        if frames:
            out.append(({**meta, "_path": p}, frames))
    return out


def report(paths: List[str], pair: Tuple[str, str] | None = None,
           th: dict | None = None) -> int:
    TH = th or load_thresholds()
    runs = _load_many(paths)
    if not runs:
        print("没有可用数据"); return 2
    fails: List[str] = []

    print("=" * 74)
    print(f"随机审计报告  runs={len(runs)}")
    for m, _ in runs:
        print(f"  {os.path.basename(m['_path']):28s} seed={m.get('seed')} "
              f"scn={m.get('scenario')} dur={m.get('duration')}s sha={m.get('git_sha')}")
    print("=" * 74)

    # M13 跨启动；若全部 run 用同一个 seed，则这是"可复现性"检查，期望 ≈1.0
    seeds = {m.get("seed") for m, _ in runs}
    same_seed = len(seeds) == 1 and None not in seeds
    m13 = A.m13_cross_run(runs)
    if m13["n_pairs"] < 2:
        print(f"M13 跨启动相似度   pairs={m13['n_pairs']}  N/A（有效run不足2个，跳过判定）")
    elif same_seed:
        ok = m13["mean"] > 0.98
        print(f"M13 同 seed 复现   pairs={m13['n_pairs']} mean={m13['mean']:.2f} "
              f"max={m13['max']:.2f}  目标>0.98  {'PASS' if ok else 'FAIL'}")
        tag = "M13(同seed)"
        if not ok:
            fails.append(tag)
    else:
        ok = m13["max"] < TH["m13_max"]
        print(f"M13 跨启动相似度   pairs={m13['n_pairs']} mean={m13['mean']:.2f} "
              f"max={m13['max']:.2f}  目标<{TH['m13_max']}  {'PASS' if ok else 'FAIL'}")
        tag = "M13"
        if not ok:
            fails.append(tag)
    print()

    # 逐 run 的指标
    all_m14, all_m16, all_m18, all_m20, all_m21, all_m15 = [], [], [], [], [], []
    for meta, frames in runs:
        name = os.path.basename(meta["_path"])
        m14 = A.m14_autocorr(frames)
        m16 = A.m16_long_window(frames)
        m18 = A.m18_spectrum(frames)
        m20 = A.m20_family_separability(frames)
        m21 = A.m21_turning_interval(frames)
        m15 = A.m15_round_structure(frames)

        if m14:
            mx = max(abs(v) for v in m14.values())
            all_m14.append(mx)
            print(f"  {name:26s} M14 |acf|max={mx:.2f} {m14}")
        if m18:
            all_m18.append(m18["peak_over_baseline"])
            print(f"  {name:26s} M18 主峰={m18['peak_hz']}Hz 峰/基线={m18['peak_over_baseline']}")
        if m15:
            all_m15.append(m15)
            cvs = [v["cv"] for v in m15.values() if "cv" in v]
            if cvs:
                print(f"  {name:26s} M15 族驻留 CV 最小={min(cvs)}  {m15}")
            else:
                print(f"  {name:26s} M15 样本不足（各族段数 <3，需更长 run）")
        if m20:
            all_m20.append(m20["ratio"])
            print(f"  {name:26s} M20 族间/族内={m20['ratio']}  {m20}")
        if m21:
            all_m21.append(m21["cv"])
            print(f"  {name:26s} M21 拐点间隔 CV={m21['cv']} (n={m21['n']})")
        if any(v is not None for v in m16.values()):
            got = {k: v for k, v in m16.items() if v is not None}
            all_m16.append(max(got.values()))
            print(f"  {name:26s} M16 长窗最大相关 {got}")

    print()
    # 汇总判定
    if all_m14:
        mx = max(all_m14)
        ok = mx < TH["m14_abs"]
        print(f"M14 自相关        max={mx:.2f}  目标<{TH['m14_abs']}  {'PASS' if ok else 'FAIL'}")
        if not ok:
            fails.append("M14")
    if all_m18:
        mx = max(all_m18)
        ok = mx < TH["m18_ratio"]
        print(f"M18 频谱峰        峰/基线 max={mx:.1f}  目标<{TH['m18_ratio']}  {'PASS' if ok else 'FAIL'}")
        if not ok:
            fails.append("M18")
    if all_m16:
        mx = max(all_m16)
        ok = mx < TH["m16_max"]
        print(f"M16 长窗相似      max={mx:.2f}  目标<{TH['m16_max']}  {'PASS' if ok else 'FAIL'}")
        if not ok:
            fails.append("M16")
    if all_m21:
        lo, hi = TH["m21_cv"]
        ok = all(lo <= v <= hi for v in all_m21)
        print(f"M21 拐点间隔 CV   {[round(v,2) for v in all_m21]}  目标 {lo}–{hi}  {'PASS' if ok else 'FAIL'}")
        if not ok:
            fails.append("M21")
    if all_m20:
        mx = min(all_m20)
        ok = mx > TH["m20_ratio"]
        print(f"M20 族间分离度   min={mx:.2f}  目标>{TH['m20_ratio']}  {'PASS' if ok else 'FAIL'}")
        if not ok:
            fails.append("M20")

    # M15 明细判定
    timer_fams = set()
    for m15 in all_m15:
        for fam, v in m15.items():
            if "cv" in v and v["cv"] < TH["m15_cv_min"]:
                timer_fams.add(fam)
    if timer_fams:
        print(f"M15 定时器化族    {sorted(timer_fams)}  (CV<{TH['m15_cv_min']})  FAIL")
        fails.append("M15")

    # M22 可复现
    if pair:
        r = A.m22_reproducible(pair[0], pair[1])
        print(f"M22 同 seed 复现   {r}  {'PASS' if r.get('identical') else 'FAIL'}")
        if not r.get("identical"):
            fails.append("M22")

    # 种子审计
    seeds = A.seed_audit(ROOT)
    if seeds:
        print("\n固定种子（跨启动相似度偏高的一号原因）：")
        for s in seeds:
            print(f"  {s['file']}:{s['line']}  seed={s['seed']}  {s['text']}")
    else:
        print("\n固定种子：未发现")

    # 决策树
    print("\n" + "-" * 74)
    if "M13" in fails:
        print("决策树：M13 高")
        print("  ├─ 固定 seed 跑两次是否逐字节一致？(用 --pair)")
        print("  │   ├─ 是 → 正常可复现；检查是否每次启动都用了同一个 debugSeed")
        print("  │   └─ 否 → 有隐藏随机源（时间/顺序依赖），见上方固定种子清单")
        print("  └─ 若 seed 每次不同仍相似 → 生成器本身是确定性的（sin(2πft) 类），")
        print("     不是 RNG 问题，要改成事件驱动")
    if "M18" in fails:
        print("决策树：M18 有孤立尖峰 → 信号含固定频率正弦，改事件驱动/随机相位")
    if "M15" in fails:
        print("决策树：M15 驻留时长 CV 太低 → 定时器驱动，改成按分布采样时长")
    if "M21" in fails:
        print("决策树：M21 拐点间隔 CV 偏离 1 → 动作周期固定，加随机抖动")
    if "M22" in fails:
        print("决策树：M22 同 seed 不一致 → 存在非 seed 随机（时间戳/dict 顺序/浮点累积）")
    print("-" * 74)
    print("结论：", "全部通过" if not fails else f"{len(fails)} 项未通过 {sorted(set(fails))}")
    return 0 if not fails else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="random_audit.report")
    ap.add_argument("files", nargs="*", help="JSONL 路径，支持通配")
    ap.add_argument("--pair", nargs=2, metavar=("A", "B"), help="同 seed 两次，做 M22")
    ap.add_argument("--config", help="阈值 JSON（默认 random_audit/thresholds.json）")
    args = ap.parse_args(argv)
    paths = []
    for pat in args.files:
        paths.extend(sorted(glob.glob(pat)) or [pat])
    if not paths:
        print(__doc__); return 2
    return report(paths, tuple(args.pair) if args.pair else None,
                  load_thresholds(args.config))


if __name__ == "__main__":
    raise SystemExit(main())
