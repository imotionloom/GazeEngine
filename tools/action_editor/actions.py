"""
动作库 - 所有固定keyframe动作都在这里定义
格式: (t, pan_off, tilt_off, epan_off, etilt_off, lid_off, roll_off)
- lid_off: 闭眼程度，0=不闭，1=全闭
"""

# ========== STRETCH - 累了扭脖子 ==========
STRETCH_KEYFRAMES = [
    (0.0,   0.0,  0.0,  0.0,  0.0,  0.0,  0.0),
    (0.2,   0.0,  0.0,  0.0,  0.0,  0.0,  0.0),
    (0.5,   0.0,  1.5,  0.0,  0.0,  0.75, 0.0),
    (1.2,   0.0,  3.0, -3.0,  0.0,  1.0, -30.0),
    (1.4,   0.0,  3.75,-3.0,  0.0,  1.0, -33.0),
    (2.2,   0.0,  3.75,-3.0,  0.0,  1.0, -33.0),
    (2.8,   0.0,  1.5,  0.0,  0.0,  0.75, 0.0),
    (3.0,   0.0,  1.5,  0.0,  0.0,  0.0,  0.0),
    (3.7,   0.0,  3.0,  3.0,  0.0,  1.0, 24.0),
    (3.9,   0.0,  3.45, 3.0, 0.0,  1.0, 27.0),
    (4.6,   0.0,  3.45, 3.0, 0.0,  1.0, 27.0),
    (5.2,   0.0,  0.0,  0.0,  0.0,  0.0,  0.0),
]
STRETCH_DUR = 5.2

# ========== SLOW_NOD - 慢慢点头 ==========
SLOW_NOD_KEYFRAMES = [
    (0.0,  0.0,  0.0,  0.0,  0.0,  0.0, 0.0),
    (0.5,  0.0,  2.0,  0.0,  0.0,  0.0, 0.0),
    (0.9,  0.0,  0.0,  0.0,  0.0,  0.0, 0.0),
    (1.4,  0.0,  2.0,  0.0,  0.0,  0.0, 0.0),
    (1.8,  0.0,  0.0,  0.0,  0.0,  0.0, 0.0),
]
SLOW_NOD_DUR = 1.8

# ========== HEAD_COCK - 歪头好奇 ==========
HEAD_COCK_KEYFRAMES = [
    (0.0,  0.0,  0.0,  0.0,  0.0,  0.0, 0.0),
    (0.4,  0.0,  0.0,  0.0,  0.0,  0.0, 3.0),
    (0.7,  0.0,  0.0,  0.0,  0.0,  0.0, 3.0),
    (1.0,  0.0,  0.0,  0.0,  0.0,  0.0, 0.0),
]
HEAD_COCK_DUR = 1.0

# ========== LOOK_UP_RECALL - 抬头回忆 ==========
LOOK_UP_RECALL_KEYFRAMES = [
    (0.0,  0.0,  0.0,  0.0,  0.0,  0.0, 0.0),
    (0.4,  0.0,  0.0,  5.0, -5.0,  0.0, 0.0),
    (1.0,  0.0,  0.0,  5.0, -5.0,  0.0, 0.0),
    (1.5,  0.0,  0.0,  0.0,  0.0,  0.0, 0.0),
]
LOOK_UP_RECALL_DUR = 1.5

# ========== GLANCE_DOWN - 低头看 ==========
GLANCE_DOWN_KEYFRAMES = [
    (0.0,  0.0,  0.0,  0.0,  0.0,  0.0, 0.0),
    (0.3,  0.0,  0.0,  0.0,  5.0,  0.0, 0.0),
    (0.7,  0.0,  0.0,  0.0,  5.0,  0.0, 0.0),
    (1.0,  0.0,  0.0,  0.0,  0.0,  0.0, 0.0),
]
GLANCE_DOWN_DUR = 1.0

# ========== 动作注册表 ==========
ACTIONS = {
    "STRETCH": (STRETCH_KEYFRAMES, STRETCH_DUR, "累了扭脖子拉伸"),
    "SLOW_NOD": (SLOW_NOD_KEYFRAMES, SLOW_NOD_DUR, "慢慢点头"),
    "HEAD_COCK": (HEAD_COCK_KEYFRAMES, HEAD_COCK_DUR, "歪头好奇"),
    "LOOK_UP_RECALL": (LOOK_UP_RECALL_KEYFRAMES, LOOK_UP_RECALL_DUR, "抬头回忆"),
    "GLANCE_DOWN": (GLANCE_DOWN_KEYFRAMES, GLANCE_DOWN_DUR, "低头看"),
}


def smootherstep(t):
    """smoothstep插值: 6t^5 - 15t^4 + 10t^3"""
    return t * t * t * (t * (t * 6 - 15) + 10)


def sample(elapsed_s, keyframes, duration):
    """在关键帧之间做 smootherstep 插值，返回6元组偏移量
    返回: (pan_off, tilt_off, epan_off, etilt_off, lid_off, roll_off)
    """
    if elapsed_s <= 0:
        return keyframes[0][1:]
    if elapsed_s >= duration:
        return keyframes[-1][1:]

    # 找当前区间
    for i in range(len(keyframes) - 1):
        t0 = keyframes[i][0]
        t1 = keyframes[i + 1][0]
        if t0 <= elapsed_s <= t1:
            if t1 == t0:
                f = 0.0
            else:
                f = smootherstep((elapsed_s - t0) / (t1 - t0))
            k0 = keyframes[i]
            k1 = keyframes[i + 1]
            return tuple(k0[j] + (k1[j] - k0[j]) * f for j in range(1, 7))

    return keyframes[-1][1:]


def export_python(action_name):
    """导出成Python代码片段，方便粘回presence.py"""
    kfs, dur, desc = ACTIONS[action_name]
    lines = [f'{action_name}_KEYFRAMES = [']
    for kf in kfs:
        lines.append(f'    {kf},')
    lines.append(']')
    lines.append(f'{action_name}_DUR = {dur}')
    return '\n'.join(lines)
