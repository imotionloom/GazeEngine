"""端到端：标定参数是否真的传到几何/关节/门限。

覆盖 "标定写入 → 加载 → 注入 → 输出变化" 的完整链路。
"""
import json
import os
import shutil
from contextlib import contextmanager

import pytest

from p2_decision import calibration as calib
from p2_decision.arbitration import SoftLimiter
from p2_decision.geometry import GeometrySolver
from p2_decision.joint_mapper import geometry_to_joints

HERE = os.path.dirname(os.path.abspath(__file__))
TMPROOT = os.path.join(os.path.dirname(HERE), "calibration", "_test_pipeline")

PAYLOAD = {
    "schema": 1, "backend": "sim",
    "runtime": {
        "fx": 452.0, "fy": 452.0, "cx": 320.0, "cy": 240.0, "fov_h_deg": 70.5,
        "yaw0_deg": 5.0, "pitch0_deg": -2.0, "dy_m": 0.30,
        "dist_k": 48.0,
        "limits_deg": {"neck_yaw": {"neg": -10.0, "pos": 10.0},
                       "eye_yaw_L": {"neg": -8.0, "pos": 8.0},
                       "eye_yaw_R": {"neg": -8.0, "pos": 8.0}},
        "deadzone_deg": {"eye": 0.6, "neck": 1.0},
        "micro_amp_range_deg": [0.3, 0.5],
        "cmd_quantum_deg": {"eye": 0.5, "neck": 0.5},
        "vergence_max_distance_m": 0.9,
        "watchdog_max_age_ms": 150, "neck_delay_ms": 180,
        "look_deg_per_weight": 23.0,
    },
}


@contextmanager
def calib_env(payload=PAYLOAD):
    d = os.path.join(TMPROOT, "sim")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "active.json")
    try:
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        yield p
    finally:
        shutil.rmtree(TMPROOT, ignore_errors=True)


def _anchors(pan_deg=20.0, tilt_deg=0.0):
    """构造一对眼睛锚点，使像素角正好等于 pan/tilt（用默认 fx 反算）。"""
    g = GeometrySolver(person_debounce=1)
    u = 320 + g.fx * __import__("math").tan(__import__("math").radians(pan_deg))
    v = 240 + g.fx * __import__("math").tan(__import__("math").radians(tilt_deg))
    return {7: {"eye_L": (u - 20, v), "eye_R": (u + 20, v),
                "nose": (u, v + 25), "mouth": (u, v + 55)}}


def test_pipeline_uses_calibration():
    SoftLimiter.set_limits({})
    with calib_env() as path:
        cfg = calib.load(path=path)
    assert cfg.loaded and cfg.fx == 452.0
    calib.apply(cfg)

    geom = GeometrySolver(cfg=cfg, person_debounce=1)
    assert geom.fx == 452.0 and geom.cx == 320.0

    intent = {"state": "LOOKING_AT_PERSON", "active_track_id": 7}
    r = geom.solve(intent, _anchors(20.0), 1000)
    joints = geometry_to_joints(r, cfg=cfg)

    # 1) 限位生效：眼被夹在 ±8°，颈在 ±10°
    assert -8.0 <= joints["eye_yaw_L"] <= 8.0
    assert -10.0 <= joints["neck_yaw"] <= 10.0
    # 2) 量化生效：输出是 0.5° 的整数倍
    for k in ("eye_yaw_L", "eye_yaw_R", "neck_yaw"):
        assert abs(joints[k] / 0.5 - round(joints[k] / 0.5)) < 1e-6, (k, joints[k])
    # 3) 手眼偏置生效：yaw0=5° 会把视线往左拉，20° 目标应明显小于 20°
    assert abs(r["_raw_pan"]) < 20.0
    SoftLimiter.set_limits({})


def test_thresholds_follow_precision():
    SoftLimiter.set_limits({})
    with calib_env() as path:
        cfg = calib.load(path=path)
    geom = GeometrySolver(cfg=cfg, person_debounce=1)
    # 扫视阈值 = deadzone/2 = 0.3°，微扫视上限 = 0.5°
    assert geom._sac_pan.threshold_deg == pytest.approx(0.3)
    assert geom._fix.max_amp == pytest.approx(0.5)
    assert geom._fix.min_amp == pytest.approx(0.3)

    plain = GeometrySolver(person_debounce=1)                 # 未标定：回到默认
    assert plain._sac_pan.threshold_deg == pytest.approx(0.25)
    assert plain._fix.max_amp == pytest.approx(1.0)


def test_vergence_disabled_beyond_limit():
    SoftLimiter.set_limits({})
    with calib_env() as path:
        cfg = calib.load(path=path)
    geom = GeometrySolver(cfg=cfg, person_debounce=1)
    # 眼距像素小 → 距离远 → 超过 vergence_max_distance_m=0.9 → 不做会聚
    far = {7: {"eye_L": (300, 240), "eye_R": (330, 240),
               "nose": (315, 250), "mouth": (315, 270)}}
    r_far = geom.solve({"state": "LOOKING_AT_PERSON", "active_track_id": 7}, far, 1000)
    assert r_far["offset_deg"] == 0.0

    near = {7: {"eye_L": (300, 240), "eye_R": (420, 240),
                "nose": (360, 250), "mouth": (360, 270)}}
    r_near = geom.solve({"state": "LOOKING_AT_PERSON", "active_track_id": 7}, near, 2000)
    assert r_near["offset_deg"] > 0.0


def test_renderer_payload_carries_look_scale():
    """渲染端靠 payload 的 look_scale 换算，缺省必须回落到 25。"""
    import re
    html = open(os.path.join(os.path.dirname(HERE), "3d", "index.html"),
                encoding="utf-8").read()
    assert "remote.look_scale" in html
    assert re.search(r">\s*5\s*\n?\s*\?\s*remote\.look_scale", html) or "lookScale" in html


def test_bridge_writer_matches_loader():
    """写入端(桥)与读取端(calibration.load)的字段契约：任何一侧改名都会被抓住。"""
    from tools.calib_bridge import SimBackend, Session

    d = os.path.join(TMPROOT, "writer", "sim")
    os.makedirs(d, exist_ok=True)
    try:
        sess = Session(SimBackend(seed=5))
        sess.dir = d
        sess.save("c1_camera", {"fx": 452.0, "fy": 448.0, "cx": 318.0,
                                "cy": 239.0, "fov_h_deg": 70.5})
        sess.save("c2_hand_eye", {"yaw0_deg": 0.8, "pitch0_deg": -2.0, "dy_m": 0.3})
        sess.save("c3_servo", {"limits": {"neck_yaw": {"neg": -40.0, "pos": 38.0}},
                               "channels": {}})
        sess.save("c4_distance", {"K": 47.5})
        sess.save("c5_latency", {"watchdog_max_age_ms": 120, "neck_delay_ms": 180})
        sess.save("c7_precision", {"derived": {
            "deadzone_deg": {"eye": 0.6, "neck": 1.2},
            "micro_amp_range_deg": [0.3, 0.8],
            "cmd_quantum_deg": {"eye": 0.3, "neck": 0.6},
            "vergence_max_distance_m": 1.4}})

        cfg = calib.load(path=os.path.join(d, "active.json"))
        assert cfg.fx == 452.0 and cfg.fy == 448.0
        assert cfg.cx == 318.0 and cfg.cy == 239.0
        assert cfg.yaw0_deg == 0.8 and cfg.pitch0_deg == -2.0 and cfg.dy_m == 0.3
        assert cfg.limit_of("neck_yaw") == (-40.0, 38.0)
        assert cfg.dist_k == 47.5
        assert cfg.watchdog_max_age_ms == 120 and cfg.neck_delay_ms == 180
        assert cfg.deadzone_for("eye") == 0.6
        assert cfg.micro_amp_range_deg == (0.3, 0.8)
        assert cfg.cmd_quantum_deg == {"eye": 0.3, "neck": 0.6}
        assert cfg.vergence_max_distance_m == 1.4
    finally:
        shutil.rmtree(os.path.join(TMPROOT, "writer"), ignore_errors=True)


def test_calib_command_broadcasts_pose():
    """C2 手动摆位：calib.command 必须驱动后端并广播 calib.pose 给渲染端。"""
    import asyncio
    from tools.calib_bridge import Bridge, SimBackend

    sent = []

    async def fake_send(msg):
        sent.append(msg)

    bridge = Bridge(SimBackend(seed=2))
    bridge._send = fake_send
    asyncio.run(bridge.on_command({
        "cmd": "calib.command",
        "joints": {"eye_yaw_L": 10.0, "eye_yaw_R": 10.0,
                   "eye_pitch_L": -3.0, "eye_pitch_R": -3.0,
                   "neck_yaw": 5.0, "neck_pitch": 1.0, "neck_roll": 0.0}}))

    poses = [m for m in sent if m.get("evt") == "calib.pose"]
    assert poses, "没有广播 calib.pose，渲染端不会动"
    p = poses[0]["pose"]
    assert p["eye_yaw"] == 10.0 and p["eye_tilt"] == -3.0 and p["neck_yaw"] == 5.0
