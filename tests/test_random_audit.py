"""随机审计工具自身的冒烟测试。

目的：保证分析器在合成数据上不崩、指标落在合理区间，
以及"固定种子清单"能真的扫出来（这是跨启动相似度的头号原因）。
"""
import os

from random_audit import analyzer as A
from random_audit.baseline import synth

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_synth_runs_and_metrics():
    meta, frames = synth(1, duration=60)
    assert len(frames) == 1800

    m13 = A.m13_cross_run([(meta, frames), (meta, frames)])
    assert 0.99 <= m13["mean"] <= 1.001, "同一份数据自比应 ≈1.0"

    m14 = A.m14_autocorr(frames)
    assert m14 and all(abs(v) <= 1.0 for v in m14.values())

    m18 = A.m18_spectrum(frames)
    assert m18 and m18["peak_over_baseline"] >= 1.0

    m21 = A.m21_turning_interval(frames)
    assert m21 and m21["cv"] > 0.0

    m15 = A.m15_round_structure(frames)
    assert m15, "族段统计非空"


def test_m13_flags_identical_runs():
    """两份完全相同的 run 必须被判为高相似（否则指标不可信）。"""
    meta, frames = synth(2, duration=30)
    r = A.m13_cross_run([(meta, frames), (meta, [dict(f) for f in frames])])
    assert r["max"] > 0.99


def test_m22_reproducible():
    """用工作区内目录，不用 tmp_path（%TEMP% 在沙箱里不可写）。"""
    import json
    import shutil
    d = os.path.join(ROOT, "tests", "_tmp_audit")
    os.makedirs(d, exist_ok=True)
    a = os.path.join(d, "a.jsonl")
    b = os.path.join(d, "b.jsonl")
    meta, frames = synth(3, duration=5)
    for p, rid in ((a, "r1"), (b, "r2")):
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "meta", **meta, "run_id": rid}) + "\n")
            for f in frames:
                fh.write(json.dumps({"type": "frame", **f}) + "\n")
    try:
        assert A.m22_reproducible(a, b)["identical"] is True
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_seed_audit_finds_fixed_seeds():
    """种子已参数化（GAZE_SEED env），审计应找不到硬编码的 42/7"""
    hits = A.seed_audit(ROOT)
    seeds = {h["seed"] for h in hits}
    assert 7 not in seeds and 42 not in seeds, f"仍有硬编码种子: {hits}"


def test_idle_filter_excludes_social():
    frames = [{"family": "SOCIAL_GAZE"}, {"family": "THINKING"}]
    assert [f["family"] for f in A.idle_only(frames)] == ["THINKING"]
