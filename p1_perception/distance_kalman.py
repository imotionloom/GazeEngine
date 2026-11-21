"""
P1 感知 · 距离估计真逻辑（可直接跑测试）
- DistanceKalman: 一维匀速模型 Kalman（位置 d / 速度 d_dot），用 wall-clock dt，掉帧自动外推
- SlidingWindowAverager: 1s 窗口平均距离，用于"谁最近"目标选择（新防线：慢误差不驱动快切换）
- LoomingGate: 连续 N 帧 d_dot 超阈值才确认靠近事件
"""
from collections import deque
import time


class DistanceKalman:
    """x = [d, d_dot]，观测 z = d_meas。dt 用真实秒，不按帧计数。"""

    def __init__(self, q_d=0.05, q_v=0.1, r=0.08, init_d=1.2):
        self.x = [init_d, 0.0]          # [m, m/s]
        self.P = [[1.0, 0.0], [0.0, 1.0]]
        self.Q = [[q_d, 0.0], [0.0, q_v]]
        self.R = r
        self.last_ts = None
        self.initialized = False

    def _predict(self, dt):
        # F = [[1, dt], [0, 1]]
        x = self.x
        x[0] = x[0] + dt * x[1]
        # P = F P F' + Q
        p = self.P
        p00 = p[0][0] + dt * (p[0][1] + p[1][0]) + dt * dt * p[1][1] + self.Q[0][0]
        p01 = p[0][1] + dt * p[1][1]
        p10 = p[1][0] + dt * p[1][1]
        p11 = p[1][1] + self.Q[1][1]
        self.P = [[p00, p01], [p10, p11]]

    def update(self, d_meas_m, ts_ms):
        if self.last_ts is None:
            self.last_ts = ts_ms
            self.initialized = True
            self.x[0] = d_meas_m
            return self.state()
        dt = max((ts_ms - self.last_ts) / 1000.0, 1e-3)   # 掉帧大 dt 自动外推
        self.last_ts = ts_ms
        self._predict(dt)
        # H = [1, 0]
        y = d_meas_m - self.x[0]
        S = self.P[0][0] + self.R
        K = [self.P[0][0] / S, self.P[1][0] / S]
        self.x[0] += K[0] * y
        self.x[1] += K[1] * y
        p = self.P
        self.P = [
            [p[0][0] - K[0] * p[0][0], p[0][1] - K[0] * p[0][1]],
            [p[1][0] - K[1] * p[0][0], p[1][1] - K[1] * p[0][1]],
        ]
        return self.state()

    def state(self):
        return {"est_m": round(self.x[0], 3), "dot_mps": round(self.x[1], 3)}


class SlidingWindowAverager:
    """目标选择专用：距离用 1s 窗口平均后再比较，逐帧比 d 会被噪声带着切人。"""

    def __init__(self, window_ms=1000):
        self.window_ms = window_ms
        self.buf = deque()   # (ts_ms, value)

    def add(self, value, ts_ms):
        self.buf.append((ts_ms, value))
        while self.buf and ts_ms - self.buf[0][0] > self.window_ms:
            self.buf.popleft()

    def mean(self):
        if not self.buf:
            return None
        return round(sum(v for _, v in self.buf) / len(self.buf), 3)


class LoomingGate:
    """靠近事件：连续 N 帧 d_dot < -threshold 才确认，防单帧毛刺触发瞬态会聚。"""

    def __init__(self, n_frames=4, dot_threshold_mps=-0.3):
        self.n = n_frames
        self.th = dot_threshold_mps
        self.cnt = 0

    def feed(self, dot_mps):
        self.cnt = self.cnt + 1 if dot_mps < self.th else 0
        return self.cnt >= self.n


# ---------------- 自测 ----------------
if __name__ == "__main__":
    kf = DistanceKalman()
    t0 = 1000
    # 模拟：人从 2.0m 匀速走近，叠加 ±0.1 测量噪声
    import random
    random.seed(0)
    print("== Kalman 滤波（真实 2.0m→0.8m，测量带噪）==")
    for i in range(20):
        true_d = 2.0 - 0.06 * i
        meas = true_d + random.uniform(-0.1, 0.1)
        ts = t0 + i * 33          # 30fps
        s = kf.update(meas, ts)
        print(f"帧{i:2d}  真实={true_d:.2f}  测量={meas:.2f}  滤波={s['est_m']:.2f}  dot={s['dot_mps']:+.2f}")

    win = SlidingWindowAverager()
    print("\n== 1s 窗口平均选目标 ==")
    for ts, v in [(1000, 1.05), (1033, 1.08), (1066, 0.95), (1099, 1.02), (1132, 1.00)]:
        win.add(v, ts)
    print(f"窗口均值={win.mean()}（噪声单点0.95被平滑掉）")

    g = LoomingGate()
    print("\n== Looming 连续确认 ==")
    dots = [-0.1, -0.5, -0.55, -0.5, -0.6]
    for d in dots:
        print(f"dot={d:+.2f}  确认={g.feed(d)}")
