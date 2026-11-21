"""FSM 状态机测试：覆盖卡死、换人、低置信度等边界。"""
from p2_decision.state_machine import GazeStateMachine, GazeState


def test_low_conf_does_not_stuck_in_looking():
    """Bug 回归：低置信度误检不应导致永久 LOOKING。"""
    fsm = GazeStateMachine()
    fsm.attention_mode = "NEUTRAL"  # 固定模式避免FOCUSED时长太长
    fsm._mode_until = 10**9  # 锁定模式不切换
    t = 0
    # 先确认一个高置信度的人（需要 >500ms SCANNING 最小驻留）
    face = {"track_id": 1, "conf": 0.9, "bbox_center": (320, 240)}
    for _ in range(25):
        fsm.update([face], t)
        t += 33
    assert fsm.state == GazeState.LOOKING

    # 现在喂低置信度的新人持续 20 秒，统计状态分布
    bad_face = {"track_id": 2, "conf": 0.2, "bbox_center": (300, 240)}
    from collections import Counter
    states = []
    for _ in range(500):
        r = fsm.update([bad_face], t)
        states.append(r["state"])
        t += 33
    cnt = Counter(states)
    # 必须出现过 GAZE_AWAY（没有永久卡在 LOOKING）
    assert "GAZE_AWAY" in cnt, \
        f"低置信度 10s 内从未进 GAZE_AWAY，卡在 LOOKING: {cnt}"


def test_track_switch_picks_nearest():
    """换人时选最近位置的 track，不跳错人。"""
    fsm = GazeStateMachine()
    t = 0
    # 锁定左边的人 (100, 240)（需要 >500ms SCANNING 最小驻留）
    face_left = {"track_id": 1, "conf": 0.9, "bbox_center": (100, 240)}
    for _ in range(25):
        fsm.update([face_left], t)
        t += 33
    assert fsm.state == GazeState.LOOKING
    assert fsm.active_track == 1

    # track 丢了，出现两个新人：左边(120,240)和右边(500,240)
    # 需要跑够1.5秒(46帧)超过lost锁定窗口
    face_left2 = {"track_id": 2, "conf": 0.9, "bbox_center": (120, 240)}
    face_right = {"track_id": 3, "conf": 0.9, "bbox_center": (500, 240)}
    for _ in range(60):
        fsm.update([face_right, face_left2], t)
        t += 33

    # 应该选左边的新人 2，不是右边的 3
    assert fsm.active_track == 2, \
        f"应选最近位置 track=2，实际选了 {fsm.active_track}"
