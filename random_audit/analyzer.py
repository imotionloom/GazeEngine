"""M13–M22 指标实现（基于帧序列，不依赖事件流）。

为什么不用事件流：本项目的 micro/climax 是 sin(2πft) 连续函数，
没有离散事件；从信号里做频谱/自相关反而更敏感。
"""
from __future__ import annotations

import json
import math
import os
import re
import statistics
from typing import Dict, List, Tuple

import numpy as np

FPS = 30

# 只有"无人"族才承载随机性；SOCIAL_* 是 FSM 跟着确定性人脸轨迹走，
# 混进来会让 M13/M14/M18 全部退化成"场景是否相同"。
IDLE_FAMILIES = {"IDLE", "SCANNING", "SEEKING", "THINKING", "RECOVERING"}


def idle_only(frames):
    return [f for f in frames if f.get("family") in IDLE_FAMILIES]


# ---------------------------------------------------------------- IO
def load(path: str):
    """返回 (meta, frames)。frames 是按 ts 排序的 dict 列表。"""
    meta = {}
    frames = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            o = json.loads(line)
            if o.get("type") == "meta":
                meta = o
            elif o.get("type") == "frame":
                frames.append(o)
    frames.sort(key=lambda f: f["ts"])
    return meta, frames


def series(frames: List[dict], key: str) -> np.ndarray:
    return np.array([float(f.get(key, 0.0)) for f in frames], dtype=float)


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    if n < 8 or a.std() < 1e-9 or b.std() < 1e-9:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


# ---------------------------------------------------------------- M13 / M17
def m13_cross_run(runs: List[Tuple[dict, List[dict]]], dur_s: float = 60.0,
                  bin_s: float = 2.0) -> Dict[str, float]:
    """跨启动相似度：2 秒桶均值向量的相关系数。

    改成"信号桶"而不是"事件类型字符串"：
    - 事件流在本项目不存在；
    - 字符串 SequenceMatcher 会做字符级对齐，产生假相似。
    """
    all_parts = []
    k = max(1, int(bin_s * FPS))
    for _, frames in runs:
        f0 = idle_only(frames)
        n = min(len(f0), int(dur_s * FPS))
        f = f0[:n]
        if n < k * 4:
            continue
        m = (n // k) * k
        parts = []
        for key in ("neck_pan", "eye_pan", "neck_tilt", "lid"):
            x = series(f, key)[:m].reshape(-1, k).mean(axis=1)
            parts.append(x)
        all_parts.append(parts)
    # 按各run共同的最少桶数截断每一段，避免拼接后块边界错位
    if not all_parts:
        return {"n_pairs": 0, "mean": 0.0, "max": 0.0}
    min_bins = min(len(p[0]) for p in all_parts)
    vecs = [np.concatenate([x[:min_bins] for x in p]) for p in all_parts]
    sims = []
    for i in range(len(vecs)):
        for j in range(i + 1, len(vecs)):
            sims.append(_corr(vecs[i], vecs[j]))
    if not sims:
        return {"n_pairs": 0, "mean": 0.0, "max": 0.0}
    return {"n_pairs": len(sims), "mean": float(np.mean(sims)), "max": float(np.max(sims))}


# ---------------------------------------------------------------- M14
def m14_autocorr(frames: List[dict], lags_s=(1, 2, 3, 4, 5)) -> Dict[str, float]:
    """一阶差分后的自相关：先去掉 OU 慢漂移，剩下的才是"节奏"。

    原来对 raw 抽签做自相关：MT19937 本来就不相关，恒为 0，测不出东西。
    """
    x = series(idle_only(frames), "neck_pan")
    if len(x) < FPS * 12:
        return {}
    d = np.diff(x)
    d = d - d.mean()
    out = {}
    for lag_s in lags_s:
        k = int(lag_s * FPS)
        if len(d) <= k + 8:
            continue
        out[f"lag{lag_s}s"] = round(_corr(d[:-k], d[k:]), 3)
    return out


# ---------------------------------------------------------------- M15 / M19
def _segments(frames: List[dict], key: str = "family"):
    segs = []
    if not frames:
        return segs
    cur = frames[0].get(key)
    start = frames[0]["ts"]
    for f in frames[1:]:
        v = f.get(key)
        if v != cur:
            segs.append((cur, (f["ts"] - start) / 1000.0))
            cur, start = v, f["ts"]
    segs.append((cur, (frames[-1]["ts"] - start) / 1000.0))
    return segs


def m15_round_structure(frames: List[dict]) -> Dict[str, dict]:
    """族驻留时长的变异系数：定时器驱动 → CV≈0；行为驱动 → CV 应显著 >0。"""
    by: Dict[str, List[float]] = {}
    for fam, d in _segments(frames):
        by.setdefault(fam, []).append(d)
    out = {}
    for fam, ds in by.items():
        if len(ds) < 3:
            out[fam] = {"n": len(ds)}
            continue
        m = statistics.mean(ds)
        s = statistics.pstdev(ds)
        out[fam] = {"n": len(ds), "mean_s": round(m, 2),
                    "cv": round(s / m, 3) if m > 1e-6 else 0.0}
    return out


def m19_dwell_shape(frames: List[dict]) -> Dict[str, float]:
    """全部驻留时长合在一起的分布形状（期望接近指数：CV≈1、偏度≈2）。"""
    ds = [d for _, d in _segments(frames) if d > 0.05]
    if len(ds) < 6:
        return {"n": len(ds)}
    m = statistics.mean(ds)
    s = statistics.pstdev(ds)
    sk = 0.0
    if s > 1e-9 and len(ds) > 2:
        sk = statistics.mean([((d - m) / s) ** 3 for d in ds])
    return {"n": len(ds), "mean_s": round(m, 2),
            "cv": round(s / m, 3) if m > 1e-6 else 0.0, "skew": round(sk, 2)}


# ---------------------------------------------------------------- M16
def m16_long_window(frames: List[dict], wins_s=(30, 120, 300), step_s: float = 10.0) -> Dict[str, float]:
    """多尺度长窗相似度。

    修正两点：
    1) 原始实现用非负计数率算 cosine —— 天然接近 1，必然误报；这里先 z-score 再算相关。
    2) 单尺度 30s 看不到 5 分钟周期，改成 30/120/300 三个尺度。
    """
    out = {}
    for win in wins_s:
        W = int(win * FPS)
        S = max(1, int(step_s * FPS))
        if len(frames) < W + S:
            out[f"{win}s"] = None
            continue
        feats = []
        for s in range(0, len(frames) - W, S):
            seg = frames[s:s + W]
            fams = [f.get("family") for f in seg]
            p = {k: fams.count(k) / len(fams) for k in set(fams)}
            ent = -sum(v * math.log2(v) for v in p.values() if v > 0)
            feats.append([series(seg, "neck_pan").std(),
                          series(seg, "eye_pan").std(),
                          series(seg, "lid").mean(),
                          ent,
                          len(set(f.get("state") for f in seg))])
        F = np.array(feats, dtype=float)
        if len(F) < 3:
            out[f"{win}s"] = None
            continue
        sd = F.std(axis=0)
        sd[sd < 1e-9] = 1.0
        Z = (F - F.mean(axis=0)) / sd
        best = -1.0
        for i in range(len(Z)):
            for j in range(i + 1, len(Z)):
                if (j - i) * step_s < win:          # 只比不重叠的窗
                    continue
                c = float(np.dot(Z[i], Z[j]) / len(Z[i]))
                best = max(best, c)
        out[f"{win}s"] = round(best, 3) if best > -0.99 else None
    return out


# ---------------------------------------------------------------- M18
def m18_spectrum(frames: List[dict], seg_s: float = 30.0,
                 band=(0.05, 2.0), smooth_bins: int = 15) -> Dict[str, float]:
    """Welch PSD 找孤立尖峰：用"相对局部趋势的突起"而不是"相对中位数"。

    第一版直接用 peak/median，OU 的 1/f 斜率会让低频永远赢（基线实测 3.6e4 的假峰）。
    这里先对 PSD 做滑动平均得到趋势，再取 band 内 psd/trend 的最大值：
      - 纯 1/f 或白噪 → ≈1
      - sin(2πft)     → 几十到上千
    """
    x = series(idle_only(frames), "neck_pan")
    seg = int(seg_s * FPS)
    if len(x) < seg * 2:
        return {}
    x = x - x.mean()
    win = np.hanning(seg)
    hop = seg // 2
    psd = np.zeros(seg // 2 + 1)
    cnt = 0
    for s in range(0, len(x) - seg + 1, hop):
        psd += np.abs(np.fft.rfft(win * x[s:s + seg])) ** 2
        cnt += 1
    psd /= max(cnt, 1)
    freqs = np.fft.rfftfreq(seg, 1.0 / FPS)

    k = max(3, smooth_bins)
    kernel = np.ones(k) / k
    trend = np.convolve(psd, kernel, mode="same")
    trend = np.maximum(trend, 1e-12)

    lo = int(np.searchsorted(freqs, band[0]))
    hi = int(np.searchsorted(freqs, band[1]))
    if hi <= lo + 2:
        return {}
    prom = psd[lo:hi] / trend[lo:hi]
    i = int(np.argmax(prom))
    return {"peak_hz": round(float(freqs[lo + i]), 4),
            "peak_over_baseline": round(float(prom[i]), 1)}


# ---------------------------------------------------------------- M20
def m20_family_separability(frames: List[dict]) -> Dict[str, float]:
    """跨族可分性：族内距离应显著小于族间距离。"""
    by: Dict[str, List[List[float]]] = {}
    for f in frames:
        fam = f.get("family")
        by.setdefault(fam, []).append([f.get("neck_pan", 0), f.get("eye_pan", 0), f.get("lid", 0)])
    fams = [k for k, v in by.items() if len(v) >= FPS * 2]
    if len(fams) < 2:
        return {}
    cent = {k: np.mean(np.array(by[k]), axis=0) for k in fams}
    scale = np.std(np.array([cent[k] for k in fams]), axis=0) + 1e-9
    intra = []
    for k in fams:
        a = np.array(by[k])
        intra.append(float(np.mean(np.linalg.norm((a - cent[k]) / scale, axis=1))))
    inter = []
    for i in range(len(fams)):
        for j in range(i + 1, len(fams)):
            inter.append(float(np.linalg.norm((cent[fams[i]] - cent[fams[j]]) / scale)))
    return {"families": len(fams),
            "intra": round(float(np.mean(intra)), 3),
            "inter_min": round(float(np.min(inter)), 3),
            "ratio": round(float(np.min(inter) / (np.mean(intra) + 1e-9)), 2)}


# ---------------------------------------------------------------- M21
def m21_turning_interval(frames: List[dict], deadband: float = 1.5) -> Dict[str, float]:
    """把信号拐点当"事件"，测间隔 CV。理想随机 ≈1，正弦/定时器 ≈0。

    deadband：两次拐点之间信号至少要走这么多度才算一次，否则平段每帧都会被计成拐点。
    """
    x = series(idle_only(frames), "neck_pan")
    if len(x) < FPS * 5:
        return {}
    idx, last_v = [], None
    for i in range(1, len(x) - 1):
        if (x[i] - x[i - 1]) * (x[i + 1] - x[i]) > 0:
            continue
        if last_v is not None and abs(x[i] - last_v) < deadband:
            continue
        idx.append(i); last_v = x[i]
    if len(idx) < 5:
        return {}
    d = np.diff(idx) / FPS
    m = float(d.mean())
    return {"n": int(len(d)), "cv": round(float(d.std() / m), 3) if m > 1e-6 else 0.0}


# ---------------------------------------------------------------- M22
def m22_reproducible(a_path: str, b_path: str) -> Dict[str, object]:
    """同 seed 两次，除 run_id/seed 外帧内容应完全一致。"""
    _, fa = load(a_path)
    _, fb = load(b_path)
    if len(fa) != len(fb):
        return {"identical": False, "reason": f"frame count {len(fa)} != {len(fb)}"}
    skip = {"ts", "run_id", "seed"}
    for i, (x, y) in enumerate(zip(fa, fb)):
        sx = {k: v for k, v in x.items() if k not in skip}
        sy = {k: v for k, v in y.items() if k not in skip}
        if sx != sy:
            diff = [k for k in sx if sx.get(k) != sy.get(k)]
            return {"identical": False, "first_diff_frame": i, "keys": diff[:5]}
    return {"identical": True, "frames": len(fa)}


# ---------------------------------------------------------------- 种子审计
_SEED_PAT = re.compile(r"random\.Random\(\s*(\d+)\s*\)|random\.seed\(\s*(\d+)\s*\)")


def seed_audit(root: str) -> List[dict]:
    """静态扫描固定种子 —— 这是跨启动相似度偏高的头号原因。"""
    hits = []
    for dirpath, _, files in os.walk(os.path.join(root, "p2_decision")):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dirpath, fn)
            with open(p, encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    for m in _SEED_PAT.finditer(line):
                        seed = m.group(1) or m.group(2)
                        hits.append({"file": os.path.relpath(p, root),
                                     "line": lineno, "seed": int(seed),
                                     "text": line.strip()[:90]})
    return hits

