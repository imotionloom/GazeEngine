"""
P2 决策 · M2.4 仲裁限幅（真逻辑，可自测）
- 收敛/发散速率分级：会聚 10-15°/s，散开 5-10°/s（近快远慢）
- 软件软限位：颈 yaw±45/pitch±30/roll±15，眼±25
- 社交点头/歪头：倾听节律点头，好奇歪头 ±5-8°
- 自适应平滑因子：base×vel×dist clamp 0.05-0.3
"""
CONVERGE_DPS = 12.0      # 会聚
DIVERGE_DPS = 7.0        # 散开
SOFT_LIMITS = {"neck_yaw": 45.0, "neck_pitch": 30.0, "neck_roll": 15.0,
               "eye_yaw_L": 25.0, "eye_yaw_R": 25.0,
               "eye_pitch_L": 25.0, "eye_pitch_R": 25.0,
               "lid_up_L": 90.0, "lid_up_R": 90.0,
               "lid_low_L": 25.0, "lid_low_R": 25.0}


class VergenceRamp:
    """会聚速率分级：靠近用收敛速度，远离用散开速度。"""

    def __init__(self, hz=100.0):
        self.cur = 0.0
        self.converge_step = CONVERGE_DPS / hz
        self.diverge_step = DIVERGE_DPS / hz

    def step(self, target_offset_deg):
        d = target_offset_deg - self.cur
        step = self.converge_step if d > 0 else self.diverge_step
        if abs(d) < step:
            self.cur = target_offset_deg
        else:
            self.cur += math.copysign(step, d)
        return round(self.cur, 3)


import math


class SoftLimiter:
    """软限位：超限钳位。

    默认是对称限位（SOFT_LIMITS），但真机左右/上下的机械行程往往不对称
    （例如 yaw 左边 43°、右边 41°），所以支持用 C3a 极限标定的结果覆盖成
    非对称区间 [lo, hi]。
    """

    _asym: dict = {}

    @classmethod
    def set_limits(cls, limits: dict) -> None:
        """limits: {channel: {"neg": -43.0, "pos": 41.0}}，单位度。"""
        clean = {}
        for k, v in (limits or {}).items():
            try:
                lo, hi = float(v["neg"]), float(v["pos"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (lo < hi) or (hi - lo) < 1.0:      # 区间非法/太窄 → 丢弃
                continue
            clean[k] = (lo, hi)
        cls._asym = clean

    @classmethod
    def limits_of(cls, k: str):
        if k in cls._asym:
            return cls._asym[k]
        lim = SOFT_LIMITS.get(k)
        return (-lim, lim) if lim else (None, None)

    @classmethod
    def clamp(cls, joints: dict):
        out = {}
        for k, v in joints.items():
            lo, hi = cls.limits_of(k)
            out[k] = v if lo is None else max(lo, min(hi, v))
        return out


class SocialNodTilt:
    """社交点头/歪头。"""

    def __init__(self):
        self.nod_until = 0
        self.tilt_angle = 0.0

    def nod(self, ts_ms, hold_ms=600):
        """倾听节律点头一次：pitch 先下 3° 再回。"""
        self.nod_until = ts_ms + hold_ms

    def offset(self, ts_ms):
        pitch = 0.0
        if ts_ms < self.nod_until:
            k = 1 - (self.nod_until - ts_ms) / 600.0
            pitch = 3.0 * math.sin(math.pi * k)      # 钟形一次
        return {"nod_pitch": round(pitch, 2), "tilt_roll": self.tilt_angle}


class AdaptiveSmoothing:
    """平滑因子 clamp：base×vel×dist ∈ [0.05, 0.3]。"""

    @staticmethod
    def factor(base, vel_dps, dist_m):
        f = base * vel_dps * dist_m
        return round(max(0.05, min(0.3, f)), 3)


# ---------------- 自测 ----------------
if __name__ == "__main__":
    print("== 会聚速率分级：offset 从 0 升到 3°（近快 12°/s） ==")
    vr = VergenceRamp()
    for i in range(40):
        o = vr.step(3.0)
        if i % 10 == 0:
            print(f"步{i*10} offset={o:.2f}°")

    print("\n== 软限位：颈 yaw=60° 应被钳到 45° ==")
    sl = SoftLimiter.clamp({"neck_yaw": 60.0, "neck_pitch": -10.0})
    print(sl)

    print("\n== 社交点头：触发后 600ms 内 pitch 钟形 ==")
    snt = SocialNodTilt()
    snt.nod(0)
    for t in (100, 300, 500, 700):
        print(f"t={t} {snt.offset(t)}")

    print("\n== 自适应平滑因子 clamp ==")
    print(f"base0.1 vel10 dist2 = {AdaptiveSmoothing.factor(0.1, 10, 2)}")
    print(f"base0.1 vel1 dist0.2 = {AdaptiveSmoothing.factor(0.1, 1, 0.2)}")
