"""
P2 决策 · M2.3 动力学生成（真逻辑，可自测）
- 眨眼状态机：随机间隔 1-8s，单次 ~150ms 三段曲线（睁→闭→睁）
- Bell 联动：快速扫视后联动一次短眨眼
- 呼吸微动包络：0.3-0.8Hz 正弦叠加在颈 pitch 上，0.5-1°
- 平滑跟踪限速：smooth pursuit <15°/s，超了走扫视
时钟外部注入（ts_ms）。
"""
import math
import random

BLINK_DURATION_MS = 150
# 包络形状（占整个时长的比例）：0→闭合到 0.35，0.35→0.55 全闭，0.55→1 睁开
# 消费方（渲染端 / P3 插补器）按 (now - t0) / mso 求值即可，不含任何策略
BLINK_CLOSE_RATIO = 0.35
BLINK_HOLD_RATIO = 0.55
BLINK_COOLDOWN_MS = 120          # 上一帧结束后至少这么久才允许下一次
BLINK_MIN_INTERVAL_MS = 1000
BLINK_MAX_INTERVAL_MS = 8000
BREATH_FREQ_HZ = 0.5
BREATH_AMP_DEG = 0.8
PURSuit_MAX_DPS = 15.0


class BlinkMachine:
    """眨眼状态机：IDLE → CLOSING → CLOSED → OPENING → IDLE。"""

    def __init__(self):
        self.state = "IDLE"
        self.began = 0
        self.next_blink = None
        import os
        _seed_env = os.environ.get("GAZE_SEED", "")
        try:
            _seed = int(_seed_env) if _seed_env else None
        except ValueError:
            _seed = None
        self._rng = random.Random(_seed)
        self.t0 = None          # 最近一次眨眼的起点（ms），供下游按时间展开
        self._last_end = -10 ** 9
        self.scale = 1.0   # preset: <1 更慢更稀（drowsy=0.5），>1 更频繁

    def event(self):
        """最近一次眨眼事件的起点与时长；消费者按 (now-t0)/ms 展开。"""
        return {"t0": self.t0, "ms": BLINK_DURATION_MS,
                "close": BLINK_CLOSE_RATIO, "hold": BLINK_HOLD_RATIO}

    def set_scale(self, s):
        self.scale = max(0.2, min(2.0, s))

    def trigger(self, ts_ms, force=False):
        if force:
            self.state = "CLOSING"
            self.began = ts_ms
            self.t0 = ts_ms
            return True
        if self.state == "IDLE" and ts_ms - self._last_end >= BLINK_COOLDOWN_MS:
            self.state = "CLOSING"
            self.began = ts_ms
            self.t0 = ts_ms
            return True
        return False

    @staticmethod
    def envelope(phase: float) -> float:
        """闭眼程度 0..1。phase = (now - t0) / 时长。

        这是**唯一**的曲线定义：采样值、渲染端、P3 插补器都用它，避免各写一套。
        """
        if phase <= 0.0 or phase >= 1.0:
            return 0.0
        if phase < BLINK_CLOSE_RATIO:
            return phase / BLINK_CLOSE_RATIO
        if phase < BLINK_HOLD_RATIO:
            return 1.0
        return 1.0 - (phase - BLINK_HOLD_RATIO) / (1.0 - BLINK_HOLD_RATIO)

    def update(self, ts_ms):
        """返回 lid_up 比例 0(全闭)~1(全开)。"""
        if self.state == "IDLE":
            if self.next_blink is None:
                # 对数正态：大部分3-5秒眨一次，偶尔很久不眨或连续眨
                import math
                med = 4000.0 / self.scale
                sigma = 0.5
                interval = math.exp(self._rng.gauss(math.log(med), sigma))
                self.next_blink = ts_ms + int(max(1000, min(15000, interval)))
            if ts_ms >= self.next_blink and ts_ms - self._last_end >= BLINK_COOLDOWN_MS:
                self.state = "CLOSING"
                self.began = ts_ms
                self.t0 = ts_ms
                self.next_blink = None
            else:
                return 1.0

        phase = (ts_ms - self.began) / BLINK_DURATION_MS
        if phase >= 1.0:
            self.state = "IDLE"
            self._last_end = ts_ms
            self.t0 = None   # 清掉时间基准，让 VRM 切回采样模式（全开）
            return 1.0
        return 1.0 - self.envelope(phase)


class BellCoupling:
    """扫视→眨眼联动：检测到快速眼跳（>200°/s）就触发一次短眨眼。"""

    def __init__(self, threshold_dps=200.0):
        self.th = threshold_dps
        self.prev_eye = None
        self.prev_ts = None

    def feed(self, eye_angle_deg, ts_ms, blink: BlinkMachine):
        if self.prev_eye is not None:
            dt = (ts_ms - self.prev_ts) / 1000.0
            dps = abs(eye_angle_deg - self.prev_eye) / max(dt, 1e-3)
            if dps > self.th:
                blink.trigger(ts_ms)                            # Bell: 扫视带眨
        self.prev_eye = eye_angle_deg
        self.prev_ts = ts_ms


class BreathModulation:
    """呼吸微动：颈 pitch 叠加 0.5Hz 正弦包络。"""

    def __init__(self):
        self.amp = BREATH_AMP_DEG

    def set_amp(self, a):
        self.amp = a

    def offset(self, ts_ms):
        return self.amp * math.sin(2 * math.pi * BREATH_FREQ_HZ * ts_ms / 1000.0)


class VelocityLimiter:
    """平滑跟踪限速：本步位移 = min(期望位移, max_step)。"""

    def __init__(self, max_dps=PURSuit_MAX_DPS, hz=100.0):
        self.max_step = max_dps / hz

    def step(self, prev, cur):
        d = cur - prev
        if abs(d) > self.max_step:
            d = math.copysign(self.max_step, d)
        return prev + d


# ---------------- 自测 ----------------
if __name__ == "__main__":
    print("== 眨眼状态机：3s 内 lid_up 曲线 ==")
    bm = BlinkMachine()
    bm.next_blink = 500
    for t in range(0, 800, 25):
        lid = bm.update(t)
        if 450 < t < 700:
            print(f"t={t}ms lid_up={lid:.2f} state={bm.state}")

    print("\n== Bell 联动：快速眼跳 → 眨眼 ==")
    bell = BellCoupling()
    bm2 = BlinkMachine()
    bell.feed(5.0, 0, bm2)
    bell.feed(40.0, 33, bm2)        # 35°/33ms ≈ 1000°/s → 触发
    print(f"眼跳后眨眼状态={bm2.state}")

    print("\n== 呼吸包络：0.5Hz ==")
    br = BreathModulation()
    for t in (0, 250, 500, 750, 1000):
        print(f"t={t}ms breath={br.offset(t):+.2f}°")

    print("\n== 平滑限速：15°/s @100Hz，目标突跳 30° ==")
    vl = VelocityLimiter()
    cur = 0.0
    for i in range(8):
        cur = vl.step(cur, 30.0)
        print(f"步{i} pos={cur:5.2f}°")
