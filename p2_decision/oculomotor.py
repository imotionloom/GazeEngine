"""眼动系统：扫视主序 + 注视噪声（微扫视+漂移）。"""
import math
from . import rng


def _min_jerk(s):
    """最小 jerk 轨迹：10s^3 - 15s^4 + 6s^5。"""
    return 10*s**3 - 15*s**4 + 6*s**5


class SaccadeGenerator:
    """一次扫视的弹道轨迹。"""
    def __init__(self, threshold_deg=0.15):
        # 低于阈值的位移不改指令：阈值来自 C7 的 deadzone/2，避免发"动不了"的指令
        self.threshold_deg = threshold_deg
        self.current = 0.0
        self.inflight = False
        self.from_ = 0.0
        self.delta = 0.0
        self.t0 = 0
        self.D = 0

    def duration(self, amp_deg):
        return max(20, min(120, 21 + 2.2 * abs(amp_deg)))

    def trigger(self, target, ts_ms):
        if abs(target - self.current) < self.threshold_deg:
            return
        if self.inflight:
            return
        self.from_ = self.current
        self.delta = target - self.current
        self.D = self.duration(self.delta)
        self.t0 = ts_ms
        self.inflight = True

    def value(self, ts_ms):
        if not self.inflight:
            return self.current
        s = max(0, min(1, (ts_ms - self.t0) / self.D))
        prof = _min_jerk(s)
        prof *= 1 + 0.05 * math.sin(math.pi * s)  # 欠阻尼过冲
        self.current = self.from_ + self.delta * prof
        if s >= 1:
            self.inflight = False
        return self.current


class FixationNoise:
    """注视期漂移 + 微扫视（只作用于眼球）。"""
    def __init__(self, min_amp=0.08, max_amp=1.0):
        # 幅度区间来自 C7：低于 r_eff 的微扫视机械上做不出来，等于没做
        self.min_amp = max(0.02, float(min_amp))
        self.max_amp = max(self.min_amp, float(max_amp))
        self._drift = 0.0
        self._drift_tau = 800
        self._micro_pan = 0.0
        self._micro_tilt = 0.0
        self._next_micro = 0
        self._last_dir_p = 1
        self._last_dir_t = 1

    def step(self, ts_ms, dt_ms):
        k = dt_ms / self._drift_tau
        # OU 过程：噪声项乘 sqrt(dt)，保证 30Hz/100Hz 幅度一致
        sqrt_dt = math.sqrt(max(1.0, dt_ms) / 1000.0)
        self._drift += -self._drift * k + 0.25 * (rng.rand() - 0.5) * 0.2 * sqrt_dt
        if ts_ms > self._next_micro:
            self._next_micro = ts_ms + int(rng.rand_range(500, 1200))
            amp = 0.3 * math.exp(0.5 * (rng.rand() * 2 - 1))
            amp = max(self.min_amp, min(self.max_amp, amp))
            if rng.rand() < 0.7: self._last_dir_p *= -1
            if rng.rand() < 0.7: self._last_dir_t *= -1
            lim_p = self.max_amp * 0.6
            lim_t = self.max_amp * 0.4
            self._micro_pan = max(-lim_p, min(lim_p, self._micro_pan + self._last_dir_p * amp))
            self._micro_tilt = max(-lim_t, min(lim_t, self._micro_tilt + self._last_dir_t * amp * 0.6))
        return self._drift + self._micro_pan, self._drift * 0.5 + self._micro_tilt
