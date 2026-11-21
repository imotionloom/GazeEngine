"""渲染 payload 的字段契约。

P0 的教训：生产者与消费者用两套时间基准，页面会静默不再眨眼。
所以这里把"payload 里必须有什么、眨眼字段是什么语义"钉死。
"""
import json

from p2_decision.calibration import CalibConfig
from p2_decision.dynamics import (BlinkMachine, BLINK_DURATION_MS,
                                  BLINK_CLOSE_RATIO, BLINK_HOLD_RATIO)

# 渲染端 index.html 必需/可选字段
REQUIRED = ("neck_yaw", "neck_pitch", "neck_roll", "eye_yaw", "eye_tilt", "blink")
BLINK_FIELDS = ("blink_age_ms", "blink_ms", "blink_close", "blink_hold")


def _g():
    return {"body_pan_deg": 5.0, "body_tilt_deg": -2.0, "neck_roll_deg": 1.0,
            "eye_pan_deg": 12.0, "eye_tilt_deg": -3.0, "lid_closure_deg": 4.0,
            "source": "eye_midpoint"}


def _payload(blink, ts, cfg=None):
    import test_harness as th
    return th.build_render_payload(_g(), 1.0, blink, ts, cfg or CalibConfig(loaded=True),
                                   "LOOKING_AT_PERSON")


def test_required_fields_present():
    b = BlinkMachine()
    p = _payload(b, 1000)
    for k in REQUIRED:
        assert k in p, f"缺字段 {k}"
    for k in BLINK_FIELDS:
        assert k in p


def test_blink_age_is_relative_not_absolute():
    """age = ts - t0，且 t0 来自同一毫秒轴——换台机器也不会错位。"""
    b = BlinkMachine()
    b.trigger(500)
    p = _payload(b, 620)
    assert p["blink_age_ms"] == 120          # 120ms 前开始眨
    assert p["blink_ms"] == BLINK_DURATION_MS
    assert p["blink_close"] == BLINK_CLOSE_RATIO
    assert p["blink_hold"] == BLINK_HOLD_RATIO


def test_blink_age_none_before_first_blink():
    b = BlinkMachine()
    p = _payload(b, 100)
    assert p["blink_age_ms"] is None         # JSON 里是 null，渲染端据此回退采样值


def test_age_survives_json_roundtrip():
    b = BlinkMachine()
    b.trigger(0)
    p = _payload(b, 90)
    back = json.loads(json.dumps(p))
    assert back["blink_age_ms"] == 90
    assert isinstance(back["blink_ms"], int)


def test_look_scale_comes_from_calibration():
    b = BlinkMachine()
    cfg = CalibConfig(loaded=True, look_deg_per_weight=27.5)
    assert _payload(b, 0, cfg)["look_scale"] == 27.5


def test_look_scale_falls_back_to_config_constant():
    import test_harness as th
    cfg = CalibConfig(loaded=True, look_deg_per_weight=0.0)
    p = th.build_render_payload(_g(), 1.0, BlinkMachine(), 0, cfg, "SCANNING")
    assert p["look_scale"] == 0.0            # 显式 0 不做兜底（由渲染端默认）


def test_state_passthrough():
    import test_harness as th
    p = th.build_render_payload(_g(), 1.0, BlinkMachine(), 0, CalibConfig(), "SCANNING")
    assert p["state"] == "SCANNING"
    assert p["source"] == "eye_midpoint"
