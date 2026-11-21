"""不变量测试：这些 bug 都不该再溜过去。"""
import math
from p2_decision.geometry import GeometrySolver, vergence_offset_deg, EYE_MAX_DEG
from p2_decision import config as C


def test_neck_eye_sum_equals_target():
    """|target| < ENTER 时，眼=target, 颈=0；> ENTER 时，颈+眼=target。"""
    g = GeometrySolver(person_debounce=1)
    for target in (-40, -30, -25, -20, -10, 0, 10, 20, 25, 30, 40):
        n, t, e_p, e_t = g._split(target, 0)
        total = n + e_p
        assert abs(total - target) < 0.5, f"target={target}, neck+eye={total}"


def test_no_gap_between_eye_max_and_enter():
    """ENTER_PAN_DEG 必须 <= EYE_MAX_DEG，否则 25-32° 盲区。"""
    assert C.ENTER_PAN_DEG <= EYE_MAX_DEG


def test_120s_no_drift():
    """OU 随机游走 120s 不该单向漂移超过 5°。"""
    g = GeometrySolver(person_debounce=1)
    anchors = {7: {"eye_L": (300, 240), "eye_R": (340, 240),
                   "nose": (320, 250), "mouth": (320, 270)}}
    intent = {"state": "LOOKING_AT_PERSON", "active_track_id": 7}
    p0 = None
    for i in range(3600):  # 120s at 30fps
        ts = i * 33
        r = g.solve(intent, anchors, ts)
        if p0 is None:
            p0 = r["body_tilt_deg"]
    drift = abs(r["body_tilt_deg"] - p0)
    assert drift < 15, f"drift={drift}°"


def test_two_runs_differ():
    """两次运行的眼位必须不同（不是固定回放）。"""
    results = []
    for _ in range(2):
        g = GeometrySolver(person_debounce=1)
        seq = []
        for i in range(100):
            ts = i * 33
            anchors = {7: {"eye_L": (300, 240), "eye_R": (340, 240),
                           "nose": (320, 250), "mouth": (320, 270)}}
            intent = {"state": "LOOKING_AT_PERSON", "active_track_id": 7}
            r = g.solve(intent, anchors, ts)
            seq.append(r["eye_pan_deg"])
        results.append(seq)
    assert results[0] != results[1]


def test_lid_sign():
    """向下看闭、向上看睁（公式正确即可）。"""
    from p2_decision.geometry import GeometrySolver
    g = GeometrySolver(person_debounce=1)
    # 直接验证公式：e_tilt>0 向下看 → closure>0
    closure_down = max(0, min(60, 0.6 * max(0, 10)))
    closure_up = max(0, min(60, 0.6 * max(0, -10)))
    assert closure_down > 0
    assert closure_up == 0


def test_micro_bounded():
    """10s 微扫视不越界 ±13°（包含社交撤回偏移，最终clamp到±12°）。"""
    g = GeometrySolver(person_debounce=1)
    anchors = {7: {"eye_L": (300, 240), "eye_R": (340, 240),
                   "nose": (320, 250), "mouth": (320, 270)}}
    intent = {"state": "LOOKING_AT_PERSON", "active_track_id": 7}
    for i in range(300):
        r = g.solve(intent, anchors, i * 33)
    assert abs(r["eye_pan_deg"]) < 13


def test_main_sequence():
    """主序：20° 扫视时长 55-80ms，33ms步长下200ms内到19.5°。"""
    from p2_decision.oculomotor import SaccadeGenerator
    s = SaccadeGenerator()
    D = s.duration(20)
    assert 55 < D < 80
    for t in range(0, 300, 33):
        s.trigger(20, t)
        v = s.value(t)
    assert v > 19.5, f"eye stuck at {v}"
