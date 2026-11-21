"""标定加载器的不变量：缺文件不崩、脏数据丢弃、注入生效。

注意：不用 pytest 的 tmp_path（它落在 %TEMP%，沙箱下不可写），
改在测试目录内建临时目录，用完即删。
"""
import json
import os
import shutil
from contextlib import contextmanager

import pytest

from p2_decision import calibration as calib
from p2_decision.arbitration import SoftLimiter

HERE = os.path.dirname(os.path.abspath(__file__))
TMPROOT = os.path.join(os.path.dirname(HERE), "calibration", "_test")


@contextmanager
def calib_file(payload, backend="real"):
    """在项目内建一个临时标定文件（不用 %TEMP%，沙箱下不可写）。"""
    d = os.path.join(TMPROOT, backend)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "active.json")
    try:
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        yield p
    finally:
        shutil.rmtree(TMPROOT, ignore_errors=True)


def test_missing_file_falls_back():
    cfg = calib.load(path="Z:/does/not/exist.json")
    assert cfg.loaded is False
    assert cfg.warnings                       # 必须有告警，不能静默
    assert cfg.deadzone_for("eye") == 1.0     # 保守默认
    assert cfg.vergence_max_distance_m == 0.0  # 关会聚


def test_valid_config_parses():
    with calib_file({
        "schema": 1, "backend": "real",
        "runtime": {"fx": 452.0, "fy": 450.0, "cx": 320.0, "cy": 240.0,
                    "fov_h_deg": 70.5, "yaw0_deg": 0.8, "pitch0_deg": -2.0,
                    "dy_m": 0.30, "dist_k": 48.0,
                    "limits_deg": {"neck_yaw": {"neg": -43.5, "pos": 41.2}},
                    "deadzone_deg": {"eye": 0.48, "neck": 0.9},
                    "micro_amp_range_deg": [0.24, 1.0],
                    "vergence_max_distance_m": 1.8,
                    "watchdog_max_age_ms": 100, "neck_delay_ms": 172},
    }) as p:
        cfg = calib.load(path=p)
    assert cfg.loaded and cfg.has_camera
    assert cfg.fx == 452.0 and cfg.cx == 320.0
    assert cfg.limit_of("neck_yaw") == (-43.5, 41.2)
    assert cfg.micro_amp_range_deg == (0.24, 1.0)


def test_bad_limits_are_dropped():
    with calib_file({"schema": 1, "runtime": {
        "limits_deg": {"neck_yaw": {"neg": 40.0, "pos": -40.0},   # 反序
                       "eye_yaw_L": {"neg": "x", "pos": 20}},      # 非数字
    }}) as p:
        cfg = calib.load(path=p)
    assert cfg.limits == {}
    fb = calib.C.LIMIT_FALLBACK["neck_yaw"]
    assert cfg.limit_of("neck_yaw") == (fb["neg"], fb["pos"])


def test_insane_camera_falls_back():
    with calib_file({"schema": 1, "runtime": {"fov_h_deg": 179.0, "fx": 0}}) as p:
        cfg = calib.load(path=p)
    assert cfg.fov_h_deg == calib.C.FOV_H_DEG
    assert cfg.has_camera is False


def test_apply_sets_soft_limits():
    SoftLimiter.set_limits({})
    cfg = calib.CalibConfig(
        loaded=True, limits={"neck_yaw": {"neg": -43.5, "pos": 41.2}})
    issues = calib.apply(cfg)
    assert SoftLimiter.clamp({"neck_yaw": 99}) == {"neck_yaw": 41.2}
    assert issues == []
    SoftLimiter.set_limits({})


def test_apply_flags_asymmetry():
    SoftLimiter.set_limits({})
    cfg = calib.CalibConfig(
        loaded=True, limits={"neck_yaw": {"neg": -30.0, "pos": 45.0}})
    issues = calib.apply(cfg)
    assert any("不对称" in w for w in issues)
    SoftLimiter.set_limits({})


def test_geometry_uses_calibration():
    from p2_decision.geometry import GeometrySolver

    plain = GeometrySolver()
    cfg = calib.CalibConfig(loaded=True, fx=452.0, cx=320.0, cy=240.0,
                            fov_h_deg=70.5)
    tuned = GeometrySolver(cfg=cfg)
    assert plain.fx != tuned.fx
    assert tuned.fx == pytest.approx(452.0)


def test_quick_modes_do_not_crash():
    """开机自检的两个 quick 入口：没有历史标定时也不能抛。"""
    import asyncio
    from tools.calib_bridge import SimBackend, Session, StepRunner

    async def run():
        b = SimBackend(seed=11)
        sess = Session(b)
        sess.data.pop("c3_servo", None)          # 模拟"还没标定过"
        runner = StepRunner(sess, send=lambda _m: asyncio.sleep(0))
        return await runner.run_c3_quick()

    res = asyncio.run(run())
    assert res["verdict"] == "pass"
    assert res["payload"]["checked"] == {}       # 无历史 → 不测，但也不崩
