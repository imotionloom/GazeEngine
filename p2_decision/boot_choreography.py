"""
BootChoreography · 开机展示编排（17秒确定性脚本）
详见 docs/BOOT_SEQUENCE.md。

上电后强制播放：低头闭眼 -> 慢睁眼 -> 直视 -> 眼上瞟左 -> 头跟随 ->
眼球横扫右 -> 头跟随 -> 半垂脸回望 -> 慢眨眼 -> 警觉。
期间忽略所有 IF-1a 追踪帧，不眨眼，不走 FSM。
结束后 sample() 返回 None，主循环交回 GazeStateMachine。

头通道（pan/tilt/roll）用 smoothstep 缓动（ease-in-out），
眼通道（epan/etilt）和眼睑保持线性（真人扫视本身就是急加速）。
"""
import math
import time

# (t_s, body_pan, body_tilt, neck_roll, eye_pan, eye_tilt, lid_up)
# 坐标约定：+pan=右转, +tilt=低头, lid_up 1=全睁/0=全闭
_KEYFRAMES = [
    (0.00,   0.0,  8.0,  0.0,   0.0,  8.0, 0.0),   # PREPARE 低头闭眼
    (2.00,   0.0,  8.0,  0.0,   0.0,  8.0, 0.0),   # BOOT 起点
    (3.50,   0.0,  8.0,  0.0,   0.0,  8.0, 0.3),   # 眼皮微缝偷看
    (5.00,   0.0,  0.0,  0.0,   0.0,  0.0, 0.8),   # 头抬起，眼睁开80%
    (5.50,   0.0,  0.0,  3.0,   0.0,  0.0, 1.0),   # 直视，头微歪
    (6.00,   0.0,  0.0,  3.0,   0.0,  0.0, 1.0),   # 睁眼保持
    (6.06,   0.0,  0.0,  3.0,   0.0,  0.0, 0.0),   # 快眨闭（0.06s）
    (6.18,   0.0,  0.0,  3.0,   0.0,  0.0, 1.0),   # 快眨开（0.12s）
    (6.50,   0.0,  0.0,  3.0,   0.0,  0.0, 1.0),   # 直视保持
    (7.00,   0.0, -3.0,  3.0,  15.0, -8.0, 0.9),   # 眼头上挑右上，警觉睁眼
    (8.50,  25.0, -3.0,  5.0,  15.0, -8.0, 0.95),  # 头右转+抬头，眼睛睁大
    (9.50,  25.0, -3.0,  5.0,  15.0, -8.0, 0.95),  # 右上保持，睁眼
    (10.00, 25.0, -3.0,  5.0,   0.0, -4.0, 0.95),  # 眼扫回前方，睁着眼
    (10.50, 25.0,  0.0,  5.0,   0.0,  0.0, 0.9),   # 头放平，睁眼
    (11.00, 25.0,  0.0,  5.0,   0.0,  0.0, 0.9),   # 停顿，睁眼
    (12.50,-25.0,  0.0, -5.0,   0.0,  0.0, 0.9),   # 头左转到位，睁眼
    (13.50,-25.0,  0.0, -5.0,   0.0,  0.0, 0.9),   # 睁眼保持
    (13.56,-25.0,  0.0, -5.0,   0.0,  0.0, 0.0),   # 眨闭（0.06s）
    (13.68,-25.0,  0.0, -5.0,   0.0,  0.0, 1.0),   # 眨开（0.12s）
    (14.50,-25.0,  0.0, -5.0,   0.0,  0.0, 1.0),   # 左边停顿，睁眼
    (15.50,  0.0,  0.0,  0.0,   0.0,  0.0, 0.9),   # 头回中，睁眼
    (16.00,  0.0, -3.0,  0.0,   0.0,  0.0, 0.9),   # 抬下巴睁眼
    (16.50,  0.0, -3.0,  0.0,   0.0,  0.0, 0.9),   # 睁眼保持
    (16.56,  0.0, -3.0,  0.0,   0.0,  0.0, 0.0),   # 收尾眨闭（0.06s）
    (16.68,  0.0, -3.0,  0.0,   0.0,  0.0, 1.0),   # 收尾眨开（0.12s）
    (17.00,  0.0, -3.0,  0.0,   0.0,  0.0, 1.0),   # 保持
    (17.00,  0.0,  0.0,  0.0,   0.0,  0.0, 1.0),   # BOOT 结束，警觉待命
]

TOTAL_S = 17.0       # 2s prepare + 15s boot
BREATH_START_S = 3.5 # 呼吸叠加起点
BREATH_AMP_DEG = 0.3
BREATH_HZ = 0.2      # 5s 周期


def _lerp(a, b, u):
    return a + (b - a) * u


def _smoothstep(u):
    """ease-in-out：起步慢、中间快、收尾慢。用于头通道。"""
    return u * u * (3.0 - 2.0 * u)


def _interp(t):
    """插值关键帧，返回 (pan, tilt, roll, epan, etilt, lid)。
    头通道(pan/tilt/roll)用 smoothstep 缓动；眼/眼睑保持线性。"""
    if t <= _KEYFRAMES[0][0]:
        k = _KEYFRAMES[0]
        return k[1], k[2], k[3], k[4], k[5], k[6]
    if t >= _KEYFRAMES[-1][0]:
        k = _KEYFRAMES[-1]
        return k[1], k[2], k[3], k[4], k[5], k[6]
    for i in range(len(_KEYFRAMES) - 1):
        k0 = _KEYFRAMES[i]
        k1 = _KEYFRAMES[i + 1]
        if k0[0] <= t <= k1[0]:
            u = (t - k0[0]) / (k1[0] - k0[0])
            ue = _smoothstep(u)
            # 头通道：缓动
            pan = _lerp(k0[1], k1[1], ue)
            tilt = _lerp(k0[2], k1[2], ue)
            roll = _lerp(k0[3], k1[3], ue)
            # 眼/睑：线性（扫视要快，眼睑开合本身匀速）
            epan = _lerp(k0[4], k1[4], u)
            etilt = _lerp(k0[5], k1[5], u)
            lid = _lerp(k0[6], k1[6], u)
            return pan, tilt, roll, epan, etilt, lid
    k = _KEYFRAMES[-1]
    return k[1], k[2], k[3], k[4], k[5], k[6]


class BootChoreography:
    """17s 开机编排。sample() 返回 geometry_to_joints 兼容的 g dict；结束返回 None。"""

    def __init__(self):
        self.t0 = None

    def start(self):
        self.t0 = time.monotonic()

    def is_active(self):
        if self.t0 is None:
            return False
        return (time.monotonic() - self.t0) < TOTAL_S

    def elapsed(self):
        if self.t0 is None:
            return 0.0
        return time.monotonic() - self.t0

    def sample(self):
        """返回 g dict；编排结束返回 None。"""
        t = self.elapsed()
        if t >= TOTAL_S:
            return None
        pan, tilt, roll, epan, etilt, lid = _interp(t)
        # 呼吸叠加（t > BREATH_START_S 后）
        if t > BREATH_START_S:
            tilt += BREATH_AMP_DEG * math.sin(2.0 * math.pi * BREATH_HZ * (t - BREATH_START_S))
        return {
            "body_pan_deg": pan,
            "body_tilt_deg": tilt,
            "neck_roll_deg": roll,
            "eye_pan_deg": epan,
            "eye_tilt_deg": etilt,
            "offset_deg": 0.0,
            "lid_up": lid,
        }


# 自测：python -m p2_decision.boot_choreography
if __name__ == "__main__":
    b = BootChoreography()
    b.start()
    print(f"BOOT sequence: {TOTAL_S}s total, {len(_KEYFRAMES)} keyframes")
    real_t0 = b.t0
    for kf in _KEYFRAMES:
        t_target = kf[0]
        b.t0 = real_t0 - t_target - 0.001
        g = b.sample()
        if g is None:
            print(f"t={t_target:5.2f}s  (finished)")
            continue
        print(f"t={t_target:5.2f}s  pan={g['body_pan_deg']:6.2f}  tilt={g['body_tilt_deg']:6.2f}  "
              f"roll={g['neck_roll_deg']:5.2f}  epan={g['eye_pan_deg']:6.2f}  "
              f"etilt={g['eye_tilt_deg']:6.2f}  lid={g['lid_up']:.2f}")
    b.t0 = real_t0
    print(f"\nis_active at t={TOTAL_S+1}: ", end="")
    b.t0 = time.monotonic() - TOTAL_S - 1
    print(b.is_active(), " sample() ->", b.sample())
