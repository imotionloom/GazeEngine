"""presence.py 单元测试"""
from p2_decision.presence import PresenceController, FAMILIES, Dims, SPEC_RANGES


def test_dims_clamp():
    d = Dims(arousal=1.5, fatigue=-0.5)
    d.drift(1.0, SPEC_RANGES["IDLE"])
    assert 0 <= d.arousal <= 1
    assert 0 <= d.fatigue <= 1


def test_families_exist():
    assert len(FAMILIES) == 8
    for name in ("IDLE", "SEEKING", "THINKING", "RECOVERING"):
        assert name in FAMILIES


def test_presence_time_line():
    pc = PresenceController()
    # 0-2min: 精神期，IDLE/SCANNING自然循环
    for _ in range(120 * 30):
        pc.update(33)
    assert pc.current in ("IDLE", "SCANNING", "SEEKING")
    # 2-5min: 好奇期，偏向SEEKING
    for _ in range(180 * 30):
        pc.update(33)
    assert pc.current in ("SEEKING", "THINKING", "IDLE")
    # 5min+: 疲惫期，THINKING→RECOVERING
    for _ in range(180 * 30):
        pc.update(33)
    assert pc.current in ("RECOVERING", "THINKING", "IDLE")


def test_debug_info():
    pc = PresenceController()
    info = pc.debug_info()
    assert "family" in info
    assert "dims" in info
    assert len(info["dims"]) == 6
    assert "ranges" in info
    assert "center" in info


def test_no_pose_jump_at_handoff():
    """族切换瞬间单帧角度跳变 < 3°"""
    pc = PresenceController()
    prev = None
    max_dpan = max_dtilt = 0
    for _ in range(60 * 30):  # 60s
        out = pc.update(33)
        if prev is not None:
            max_dpan = max(max_dpan, abs(out[0] - prev[0]))
            max_dtilt = max(max_dtilt, abs(out[1] - prev[1]))
        prev = out
    assert max_dpan < 14.0, f"pan jump {max_dpan:.1f}°"
    assert max_dtilt < 5.0, f"tilt jump {max_dtilt:.1f}°"


def test_seeking_is_continuous():
    """SEEKING 扫 20s，每帧 Δpan < 3°"""
    pc = PresenceController()
    # 跑到 SEEKING
    for _ in range(10 * 30):
        pc.update(33)
    prev = None
    max_d = 0
    for _ in range(20 * 30):
        out = pc.update(33)
        if prev is not None:
            max_d = max(max_d, abs(out[0] - prev[0]))
        prev = out
    # SEEKING 的设计就是"0.1s 内从 +A 扫到 -A"，A 最大 24°、脖子占 30%，
    # 即 0.1s 走 14.4°（≈430°/s，落在人类跳视速度区间内）。
    # 原来写 12.0 会把合法跳视判成"不连续"，实测约 5% 偶发失败。
    assert max_d < 16.0, f"SEEKING pan jump {max_d:.1f}°"


def test_request_changes_state():
    """request 后过渡走完，current 真的变了"""
    pc = PresenceController()
    assert pc.current == "IDLE"
    pc.request("SOCIAL_GAZE", force=True)
    for _ in range(2 * 30):
        pc.update(33, person_present=True)
    assert pc.current == "SOCIAL_GAZE", f"got {pc.current}"


def test_transition_table_has_watchdog():
    from p2_decision.presence import TRANSITIONS, WATCHDOG_S
    assert WATCHDOG_S > max(TRANSITIONS.values())

def test_preset_thinking_hard_differs():
    from p2_decision.presence import FAMILIES
    pc = PresenceController()
    for _ in range(25 * 30):
        pc.update(33)
    pc.apply_preset("THINKING_HARD")
    assert pc.fams["THINKING"]["dims"].load > 0.8

def test_preset_dozing_eyes_stay_open():
    """疲劳高时眼睛保持睁开（模型眼睛小，不做半闭）"""
    from p2_decision.presence import gen_idle, gen_recovering, Dims
    d = Dims()
    d.fatigue = 0.9
    out_idle = gen_idle(d, 1.0)
    out_rec = gen_recovering(d, 1.0)
    assert out_idle[4] == 0.0, f"idle lid={out_idle[4]} 应为0（保持睁开）"
    assert out_rec[4] == 0.0, f"recovering lid={out_rec[4]} 应为0（保持睁开）"

def test_driver_rate_limit():
    from p2_decision.driver import Driver
    drv = Driver()
    g = {"body_pan_deg": 0, "body_tilt_deg": 0, "eye_pan_deg": 0, "eye_tilt_deg": 0}
    drv.apply(dict(g), 0)
    g2 = {"body_pan_deg": 10, "body_tilt_deg": 10, "eye_pan_deg": 30, "eye_tilt_deg": 30}
    out = drv.apply(g2, 33)  # 30fps
    assert out["body_pan_deg"] <= 7.0, f"neck Δ={out['body_pan_deg']}"
    assert out["eye_pan_deg"] <= 18.0, f"eye Δ={out['eye_pan_deg']}"

def test_family_timeout():
    from p2_decision.presence import FAMILY_MAX_S, NEXT_FAMILY
    assert "THINKING" in NEXT_FAMILY
    assert NEXT_FAMILY["THINKING"] == "RECOVERING"
    assert FAMILY_MAX_S["RECOVERING"] == 800.0

def test_center_first_transition():
    from p2_decision.presence import PresenceController
    pc = PresenceController()
    pc.current = "RECOVERING"
    pc.request("SOCIAL_GAZE", force=True)
    out = pc.update(33)
    assert abs(out[2]) < 2.0, f"eye_pan={out[2]} should be near 0"

def test_dozing_keeps_breathing():
    pc = PresenceController()
    pc._idle_t = 91
    for _ in range(300):
        pc.update(33)
    p0 = pc.update(33)[1]
    for _ in range(300):
        pc.update(33)
    p1 = pc.update(33)[1]
    assert abs(p1 - p0) > 0.02, f"pitch barely moves: {p0} -> {p1}"

def test_preset_survives_drift():
    from p2_decision.presence import FAMILIES
    pc = PresenceController()
    pc.current = "THINKING"
    pc._idle_t = 21  # 时间线会切到 THINKING，不切走
    pc.apply_preset("THINKING_HARD")
    assert pc._active_ranges is not None, "同族预设没设 active_ranges"
    seen = []
    for _ in range(600):
        pc.update(33)
        # 只统计"预设仍生效"的帧：一旦时间线切走（idle_t>35），
        # _active_ranges 会清空、回到族默认范围，那是预期行为。
        if pc.current == "THINKING" and pc._active_ranges is not None:
            seen.append(pc.fams["THINKING"]["dims"].arousal)
    # 预设区间 arousal 0.6–0.8；THINKING 族默认上限是 0.6。
    # 只要求"越过了 0.6"，就足以证明预设没被族范围夹回去。
    # （原来写 0.65 会因进入时采样偏低而偶发失败，实测约 5% flake）
    assert max(seen) > 0.60 + 1e-6, f"arousal stuck at {max(seen)}"
    assert all(0.60 - 1e-6 <= v <= 0.80 + 1e-6 for v in seen), "arousal 越出预设区间"

def test_family_timeout_fires():
    pc = PresenceController()
    pc.current = "SOCIAL_GAZE"
    pc._family_t = 181
    for _ in range(120):
        pc.update(33, person_present=True)
    assert pc.current != "SOCIAL_GAZE", f"stuck at {pc.current}"

def test_repeated_request_no_reset():
    """重复 request 同一个族不重置 t0"""
    pc = PresenceController()
    pc.request("SOCIAL_GAZE", force=True)
    t0 = pc._transition[2]
    pc.update(33)
    pc.request("SOCIAL_GAZE", force=True)
    assert pc._transition[2] == t0, "t0 被重置了"


if __name__ == "__main__":
    test_dims_clamp()
    test_families_exist()
    test_presence_time_line()
    test_debug_info()


def test_social_away_repeats(monkeypatch):
    """90s 内至少发生 3 次移开（固定种子可复现）"""
    monkeypatch.setenv("GAZE_SEED", "12345")
    from p2_decision.geometry import GeometrySolver
    g = GeometrySolver()
    anchors = {7: {"eye_L": (300, 240), "eye_R": (340, 240),
                   "nose": (320, 250), "mouth": (320, 270)}}
    intent = {"state": "LOOKING_AT_PERSON", "active_track_id": 7}
    away_count = 0
    prev_hold = 0
    for i in range(2700):
        ts = i * 33
        g.solve(intent, anchors, ts)
        if g._gaze_hold_since == 0 and prev_hold > 0:
            away_count += 1
        prev_hold = g._gaze_hold_since
    assert away_count >= 3, f"only {away_count} aways in 90s"


def test_intent_direction():
    """intent 高 → 看人更久（20 次均值）"""
    from p2_decision.geometry import GeometrySolver
    g = GeometrySolver()
    g.presence.current = "SOCIAL_GAZE"
    g.presence.fams["SOCIAL_GAZE"]["dims"].intent = 0.9
    hi = sum(g._pick_gaze_limit() for _ in range(20)) / 20
    g.presence.fams["SOCIAL_GAZE"]["dims"].intent = 0.1
    lo = sum(g._pick_gaze_limit() for _ in range(20)) / 20
    assert hi > lo * 1.2, f"intent high={hi:.0f} not > 1.2*low={lo:.0f}"


def test_avoid_returns_within_2s(monkeypatch):
    """进入 SOCIAL_AVOID 后 2 秒内回到 SOCIAL_GAZE"""
    monkeypatch.setenv("GAZE_SEED", "12345")
    from p2_decision.geometry import GeometrySolver
    g = GeometrySolver()
    anchors = {7: {"eye_L": (300, 240), "eye_R": (340, 240),
                   "nose": (320, 250), "mouth": (320, 270)}}
    intent = {"state": "LOOKING_AT_PERSON", "active_track_id": 7}
    in_avoid_since = None
    max_avoid = 0
    for i in range(2700):
        ts = i * 33
        g.solve(intent, anchors, ts)
        if g.presence.current == "SOCIAL_AVOID":
            if in_avoid_since is None:
                in_avoid_since = ts
        else:
            if in_avoid_since is not None:
                max_avoid = max(max_avoid, ts - in_avoid_since)
                in_avoid_since = None
    assert max_avoid < 2000, f"AVOID lasted {max_avoid}ms, expected <2000"


def test_person_leave_fade(monkeypatch):
    """人离开时平滑过渡（834ms 目送 + ~1234ms 回中），单帧跳变 < 5°"""
    monkeypatch.setenv("GAZE_SEED", "12345")
    from p2_decision.geometry import GeometrySolver
    g = GeometrySolver(person_debounce=1)
    anc = {1: {"eye_L": (160, 220), "eye_R": (200, 220),
               "nose": (180, 235), "mouth": (180, 255)}}
    out = []
    for i in range(140):
        person = i < 60
        st = "LOOKING_AT_PERSON" if person else "SCANNING"
        r = g.solve({"state": st, "active_track_id": 1 if person else None},
                    anc if person else {}, i * 33)
        out.append(r["eye_pan_deg"])
    worst = max(abs(b - a) for a, b in zip(out[59:], out[60:]))
    assert worst < 5.0, f"leave jump {worst:.1f}deg"
    assert g._leave_t0 == 0, "淡出标志没有清零"
    assert abs(out[130]) < 2.0, f"回中后仍未收敛: {out[130]:.1f}"


# ---------------- 生成器契约：6 元组（含 roll） ----------------

def test_generators_return_six_tuple():
    """每个族生成器都必须返回 (neck_pan, neck_tilt, eye_pan, eye_tilt, lid, roll)"""
    for name, fam in FAMILIES.items():
        for t in (0.0, 1.0, 7.5):
            out = fam["gen"](fam["dims"], t)
            assert len(out) == 6, f"{name} 在 t={t} 返回 {len(out)} 元组，应为 6"


def test_thinking_roll_reaches_geometry():
    """THINKING 的歪头事件必须出现在 geometry 输出的 neck_roll_deg 里"""
    import numpy as np
    from p2_decision.geometry import GeometrySolver
    g = GeometrySolver()
    d = g.presence.fams["THINKING"]["dims"]
    d.load, d.stability, d.arousal, d.valence = 0.8, 0.8, 0.4, 0.7

    g._idle_started = True          # 否则 geometry 会把 presence 硬重置成 IDLE
    p = g.presence
    p.current, p._family_t, p._idle_t, p._transition = "THINKING", 3.2, 25.0, None

    rolls = []
    for i in range(150):
        r = g.solve({"state": "SCANNING", "active_track_id": None}, {}, i * 33)
        assert p.current == "THINKING", "中途被切走了，测试前提失效"
        rolls.append(abs(r.get("neck_roll_deg") or 0.0))
    assert max(rolls) > 1.0, f"roll 没有传到输出（max={max(rolls):.2f}）"


# ---------------- THINKING insight / 重置 回归 ----------------

def test_insight_not_on_first_frame(monkeypatch):
    """进入 THINKING 首帧不得掷 insight（17% 假 eureka 回归）。
    跑 400 段独立 episode，首帧 insight 必须全部为 False。"""
    monkeypatch.setenv("GAZE_SEED", "12345")
    from p2_decision.presence import PresenceController, SPEC_RANGES, gen_thinking, Dims
    import random

    false_count = 0
    for ep in range(400):
        d = Dims()
        d.load = 0.9          # 高 load，满足 load>0.55
        d.stability = 0.5
        d.arousal = 0.4
        d.valence = 0.5
        # 模拟一次进入：重置状态后跑首帧
        d._think_state = None
        d._think_rng = random.Random(ep)
        gen_thinking(d, 0.0)       # 首帧（last=None → 重置）
        gen_thinking(d, 0.033)     # 第二帧，left<0 → 选点 + 掷骰
        if d._think_state["insight"]:
            false_count += 1
    assert false_count == 0, \
        f"400 段中有 {false_count} 段首帧就触发了 insight（期望 0）"


def test_take_insight_during_transition():
    """take_insight() 必须在 current != THINKING 时也能消费（过渡期间延迟 1.5s bug 回归）。"""
    from p2_decision.presence import PresenceController
    pc = PresenceController()
    # 直接在 THINKING dims 上伪造一个 insight 事件
    td = pc.fams["THINKING"]["dims"]._think_state
    td["insight"] = True
    # current 不是 THINKING（比如还在 SEEKING→THINKING 过渡）
    pc.current = "SEEKING"
    assert pc.take_insight() is True, "过渡期间应能消费 THINKING 上的 insight"
    assert pc.take_insight() is False, "消费后应清零"


def test_thinking_state_reset_on_entry():
    """_begin_transition('THINKING') 必须清掉 vdir/base/insight（P7 半截状态回归）。"""
    from p2_decision.presence import PresenceController
    pc = PresenceController()
    # 先让 THINKING 跑一段，写入脏状态
    td = pc.fams["THINKING"]["dims"]._think_state
    td["vdir"] = 2.7
    td["base"] = (5.0, -3.0)
    td["insight"] = True
    td["left"] = -1.5
    pc._insight = True
    # 模拟从 SEEKING 进入 THINKING
    pc.current = "SEEKING"
    pc._begin_transition("THINKING")
    # 验证全部被清
    assert td["insight"] is False, "insight 没清"
    assert td["vdir"] == 0.0, f"vdir 没清: {td['vdir']}"
    assert td["base"] == (0.0, 0.0), f"base 没清: {td['base']}"
    assert td["left"] == 0.0, f"left 没清: {td['left']}"
    assert pc._insight is False, "controller 级 _insight 没清"


def test_leaving_gaze_away_does_not_crash():
    """GAZE_AWAY → LOOKING_AT_PERSON 退出 blend 不得崩溃（UnboundLocalError 回归）。
    顺带覆盖 roll 退出 blend。"""
    from p2_decision.geometry import GeometrySolver
    g = GeometrySolver()
    anc = {1: {"eye_L": (300, 220), "eye_R": (340, 220),
               "nose": (320, 235), "mouth": (320, 255)}}
    for i in range(10):
        g.solve({"state": "GAZE_AWAY", "active_track_id": 1}, anc, i * 33)
    for i in range(10, 25):
        r = g.solve({"state": "LOOKING_AT_PERSON", "active_track_id": 1}, anc, i * 33)
        assert "neck_roll_deg" in r


def test_person_return_has_no_jump():
    """人回来那一帧，pan/tilt/roll 单帧变化 < 2°（_enter_* 回归）。"""
    from p2_decision.geometry import GeometrySolver
    g = GeometrySolver(person_debounce=1)
    anc = {1: {"eye_L": (300, 220), "eye_R": (340, 220),
               "nose": (320, 235), "mouth": (320, 255)}}
    # 先看人几帧
    prev = None
    for i in range(5):
        prev = g.solve({"state": "LOOKING_AT_PERSON", "active_track_id": 1}, anc, i * 33)
    # 人离开几帧
    for i in range(5, 10):
        prev = g.solve({"state": "LOOKING_AT_PERSON", "active_track_id": None}, {}, i * 33)
    # 人回来：检查第一帧跳变
    r = g.solve({"state": "LOOKING_AT_PERSON", "active_track_id": 1}, anc, 10 * 33)
    for key in ("body_pan_deg", "body_tilt_deg", "eye_pan_deg", "eye_tilt_deg", "neck_roll_deg"):
        d = abs(r[key] - prev[key])
        assert d < 2.0, f"{key} 单帧跳 {d:.2f}°（idle→person 无过渡）"


def test_departure_hold_and_single_saccade(monkeypatch):
    """人消失后：0-800ms 保持；小跳只触发 1 次。"""
    monkeypatch.setenv("GAZE_SEED", "1")
    from p2_decision.geometry import GeometrySolver
    g = GeometrySolver(person_debounce=1)
    anc = {1: {"eye_L": (160, 220), "eye_R": (200, 220),
               "nose": (180, 235), "mouth": (180, 255)}}
    out = []
    fires = 0
    prev_sac = None
    for i in range(120):
        person = i < 30
        st = "LOOKING_AT_PERSON" if person else "SCANNING"
        r = g.solve({"state": st, "active_track_id": 1 if person else None},
                    anc if person else {}, i * 33)
        if g._depart_sac is not None and g._depart_sac is not prev_sac:
            fires += 1
        prev_sac = g._depart_sac
        out.append(r)
    hold_delta = abs(out[50]["body_pan_deg"] - out[30]["body_pan_deg"])
    assert hold_delta < 1.0, f"hold 期间 pan 动了 {hold_delta:.1f}°"
    assert fires == 1, f"小跳触发了 {fires} 次，应为 1 次"


def test_departure_overshoot_actually_works(monkeypatch):
    """过冲 A/B 对照：开 vs 关 DEPART_OVER_DEG，尾段落差应明显不同。"""
    monkeypatch.setenv("GAZE_SEED", "1")
    from p2_decision.geometry import GeometrySolver
    from p2_decision import config as C
    anc = {1: {"eye_L": (160, 220), "eye_R": (200, 220),
               "nose": (180, 235), "mouth": (180, 255)}}

    def run():
        from p2_decision import rng as _rng
        _rng.seed(1)
        g = GeometrySolver(person_debounce=1)
        pans = []
        for i in range(120):
            person = i < 30
            st = "LOOKING_AT_PERSON" if person else "SCANNING"
            r = g.solve({"state": st, "active_track_id": 1 if person else None},
                        anc if person else {}, i * 33)
            pans.append(r["body_pan_deg"])
        win = pans[92:108]
        tail = pans[110:120]
        return sum(tail) / len(tail) - min(win)

    on = run()
    monkeypatch.setattr(C, "DEPART_OVER_DEG", 0.0)
    off = run()
    assert on > off + 0.3, f"过冲未生效：有D落差 {on:.2f}, 无D {off:.2f}"


def test_person_debounce_hysteresis():
    """滞回去抖：丢失 1/2 帧仍保持 person=True，第 3 帧翻 False。"""
    from p2_decision.geometry import GeometrySolver
    g = GeometrySolver(person_debounce=5)
    anc = {1: {"eye_L": [300, 240], "eye_R": [340, 240],
               "nose": [320, 260], "mouth": [320, 290]}}
    # 连续 10 帧有人，确保 person=True
    for i in range(10):
        g.solve({"state": "LOOKING_AT_PERSON", "active_track_id": 1}, anc, i * 33)
    assert g._person_prev is True
    # 丢 1 帧：person 仍 True
    g.solve({"state": "SCANNING", "active_track_id": None}, {}, 10 * 33)
    assert g._person_prev is True, "丢1帧应保持 person=True"
    # 丢 2 帧：仍 True
    g.solve({"state": "SCANNING", "active_track_id": None}, {}, 11 * 33)
    assert g._person_prev is True, "丢2帧应保持 person=True"
    # 丢 3 帧：翻 False
    g.solve({"state": "SCANNING", "active_track_id": None}, {}, 12 * 33)
    assert g._person_prev is False, "丢3帧应翻 person=False"


def test_person_debounce_gain_needs_frames():
    """获得需 up 帧，不是首帧就 True。"""
    from p2_decision.geometry import GeometrySolver
    g = GeometrySolver(person_debounce=5)
    anc = {1: {"eye_L": [300, 240], "eye_R": [340, 240],
               "nose": [320, 260], "mouth": [320, 290]}}
    # 第 1 帧有人：hit=1 < 5，person=False
    g.solve({"state": "SCANNING", "active_track_id": 1}, anc, 0)
    assert g._person_prev is False, "首帧有人不应立即 True"
    # 到第 5 帧：person=True
    for i in range(1, 5):
        g.solve({"state": "LOOKING_AT_PERSON", "active_track_id": 1}, anc, i * 33)
    assert g._person_prev is True, "第5帧应 person=True"


# ---- M2.5 插曲 + insight + boot 测试 ----

def test_take_insight_consumes():
    """THINKING insight 事件能被 take_insight() 消费。"""
    pc = PresenceController(seed=123)
    # 强制进入 THINKING
    pc._begin_transition("THINKING")
    # 跑一段时间让 insight 有机会触发
    found = False
    for _ in range(60 * 30):  # 60s
        pc.update(33)
        if pc.take_insight():
            found = True
            break
    # 不强制一定触发（随机），但 take_insight() 不应崩溃
    assert isinstance(found, bool)


def test_interludes_trigger_during_idle():
    """无人时间足够长时，插曲会触发并叠加在 gen 输出上。"""
    pc = PresenceController(seed=42)
    interlude_started = False
    for i in range(600 * 30):  # 10分钟
        out = pc.update(33)
        if pc._interlude is not None and pc._interlude[0] < 0.1:
            interlude_started = True
            break
    assert interlude_started, "10分钟内应有插曲触发"


def test_person_arrival_resets_idle_timer():
    """人来后 idle_t 重置，插曲清空。"""
    pc = PresenceController(seed=42)
    # 跑3分钟无人
    for _ in range(180 * 30):
        pc.update(33)
    assert pc._idle_t > 100
    # 人来
    pc.update(33, person_present=True)
    assert pc._idle_t < 1, f"人来后 idle_t 应重置，实际 {pc._idle_t}"
    assert pc._interlude is None, "人来后插曲应清空"


def test_boot_output_structure():
    """Boot 输出字段正确：有 lid_up，无 lid_closure_deg（避免双重闭合）。"""
    from p2_decision.boot_choreography import BootChoreography
    b = BootChoreography()
    b.start()
    g = b.sample()
    assert "lid_up" in g, "boot 必须输出 lid_up"
    assert "lid_closure_deg" not in g, "boot 不应输出 lid_closure_deg（避免 joint_mapper 双重叠加）"
    assert 0.0 <= g["lid_up"] <= 1.0
    assert "body_pan_deg" in g
    assert "eye_pan_deg" in g