"""
P2 · IDLE 无人三状态序列（S1 呆立 → S2 思考偏头 → S3 低头闭眼）
文档：docs/IDLE_PRESET_PLAN.md v1.4 定稿
"""
import random
import math


class IdleSequence:
    """无人脸时的拟人空闲姿态机。

    状态：
      S1_STAND   — 呆立 26s，眼平视，微动
      S2_THINK   — 眼先偏一侧，头延迟跟随；带微扫视跳-停-回
      S3A_DROWSY — 半垂眼，头微低，微颤
      S3B_CLOSE  — 低头闭眼，慢漂移
    """
    # 时长（ms）
    S1_MS = 26000          # S1 呆立 26s
    S2_TRANSITION_MS = 1200  # S1→S2 过渡 1.2s（计入 S2 开头）
    S2_MS = 15000         # S2 思考总时长（含过渡）
    S3A_MS = 4000         # S3a 半垂眼 4s
    S3B_MS = 20000        # S3b 闭眼 20s

    # S2 微扫视
    SAC_JUMP_MS = 100
    SAC_HOLD_MIN_MS = 300
    SAC_HOLD_MAX_MS = 800
    SAC_RETURN_MS = 200
    SAC_RETURN_KEEP = 0.7  # 回落到 70%

    # S3a 微颤
    TREMOR_PERIOD_MS = 3000  # 3s 一次，4s 内约 1.3 次

    # S3b 慢漂移周期
    S3B_DRIFT_PERIOD_S = 10.0

    # 中断恢复：人出现后半睁看人 0.3s
    WAKE_HALF_OPEN_MS = 300

    def __init__(self, rng=None):
        self._rng = rng or random.Random()
        self.reset(0)

    def reset(self, ts_ms: int):
        self.state = "S1_STAND"
        self.state_since = ts_ms
        self.side = 1 if self._rng.random() > 0.5 else -1  # S2 偏左/右
        # S2 微扫视状态
        self._sac_phase = "wait"   # wait / jump / linger / return
        self._sac_until = ts_ms
        self._sac_offset = 0.0
        self._sac_hold_ms = 500
        self._sac_return_start = 0.0

    def update(self, ts_ms: int):
        """返回 (neck_pan, neck_tilt, eye_pan, eye_tilt, lid_closure 0~1)"""
        elapsed = ts_ms - self.state_since

        # 状态推进
        if self.state == "S1_STAND" and elapsed > self.S1_MS:
            self.state = "S2_THINK"
            self.state_since = ts_ms
            self._sac_until = ts_ms + self._sac_hold_ms
        elif self.state == "S2_THINK" and elapsed > self.S2_MS:
            self.state = "S3A_DROWSY"
            self.state_since = ts_ms
        elif self.state == "S3A_DROWSY" and elapsed > self.S3A_MS:
            self.state = "S3B_CLOSE"
            self.state_since = ts_ms

        # 输出姿态
        if self.state == "S1_STAND":
            return 0.0, 0.0, 0.0, 0.0, 0.0

        if self.state == "S2_THINK":
            return self._s2(ts_ms, elapsed)

        if self.state == "S3A_DROWSY":
            # 半垂眼，头微低 + 微颤
            k = min(1.0, elapsed / 800)
            tremor = math.sin(2 * math.pi * elapsed / self.TREMOR_PERIOD_MS) * 0.3
            return 0.0, 2.0 * k + tremor, 0.0, 4.0 * k, 0.3 * k

        if self.state == "S3B_CLOSE":
            # 低头闭眼，慢漂移
            k = min(1.0, elapsed / 1500)
            drift = math.sin(2 * math.pi * elapsed / (self.S3B_DRIFT_PERIOD_S * 1000))
            pan = drift * 1.5
            return pan, 5.0 * k, 0.0, 6.0 * k, k

    def _s2(self, ts_ms: int, elapsed: int):
        """S2 思考：眼先偏，头延迟跟随 + 微扫视跳-停-回"""
        # S2_TRANSITION_MS 过渡：眼 1.2s 内偏到 8°，头延迟 0.5s 跟到 5°
        eye_k = min(1.0, elapsed / self.S2_TRANSITION_MS)
        eye_pan_target = self.side * 8.0 * eye_k
        head_delay = max(0, min(1.0, (elapsed - 500) / 800))
        neck_pan = self.side * 5.0 * head_delay

        # 微扫视
        eye_pan = self._saccade(ts_ms, eye_pan_target)
        return neck_pan, 0.0, eye_pan, 2.0, 0.1

    def _saccade(self, ts_ms: int, base: float) -> float:
        """跳-停-回三段微扫视：
        wait(零位等待) → jump(跳到随机偏移) → linger(停在偏移上) → return(缓回落到0) → wait
        """
        if ts_ms > self._sac_until:
            if self._sac_phase == "wait":
                # 零位等待结束，开始一次新微扫视
                self._sac_phase = "jump"
                self._sac_offset = self._rng.uniform(0.03, 0.06) * (1 if self._rng.random() > 0.5 else -1)
                self._sac_until = ts_ms + self.SAC_JUMP_MS
            elif self._sac_phase == "jump":
                # 跳完，停在偏移位置
                self._sac_phase = "linger"
                self._sac_until = ts_ms + self._rng.randint(self.SAC_HOLD_MIN_MS, self.SAC_HOLD_MAX_MS)
            elif self._sac_phase == "linger":
                # 停留结束，开始缓回落
                self._sac_phase = "return"
                self._sac_return_start = ts_ms
                self._sac_until = ts_ms + self.SAC_RETURN_MS
            elif self._sac_phase == "return":
                # 回落完成，归零，进入下一次零位等待
                self._sac_phase = "wait"
                self._sac_offset = 0.0
                self._sac_until = ts_ms + self._rng.randint(self.SAC_HOLD_MIN_MS, self.SAC_HOLD_MAX_MS)

        # 输出阶段
        if self._sac_phase == "jump":
            return base + self._sac_offset
        if self._sac_phase == "linger":
            return base + self._sac_offset
        if self._sac_phase == "return":
            prog = min(1.0, (ts_ms - self._sac_return_start) / self.SAC_RETURN_MS)
            # 从当前offset线性回落到0
            return base + self._sac_offset * (1.0 - prog)
        return base  # wait阶段


if __name__ == "__main__":
    s = IdleSequence()
    t = 0
    while t < 60_000:
        out = s.update(t)
        if t % 1000 == 0:
            print(f"t={t:5d} state={s.state:12s} n_pan={out[0]:+.1f} e_pan={out[2]:+.1f} lid={out[4]:.2f}")
        t += 33
