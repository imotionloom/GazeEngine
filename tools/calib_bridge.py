"""标定向导桥：浏览器页面 ↔ 执行后端。

职责划分（对应 docs/CALIBRATION_SPEC.md §10）：
  * 页面：显示、采集、人工确认
  * 本桥：下发指令、读取实测、拟合、判定、落盘

后端：
  sim   —— 无硬件时的回路自检。它是一个**测试替身**：量化噪声/回差/一阶响应
           都是模型造的，结果写 calibration/sim/，绝不覆盖真机标定（原则 P2）。
  real  —— 真机。P3 尚未实现，连接失败时抛出 BackendUnavailable，页面会提示。

用法：
    python tools/calib_bridge.py --backend sim
    # 浏览器打开 http://127.0.0.1:9100/calib.html

端口约定（避开现有占用）：
    9100 HTTP 静态（3d/）
    9101 WebSocket（本桥）
    9000 ws_bridge（数字人镜像）  5557 harness 调试 PUB  5555 IF-5
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import shutil
import statistics
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

JOINTS = [
    "neck_yaw", "neck_pitch", "neck_roll",
    "eye_yaw_L", "eye_yaw_R", "eye_pitch_L", "eye_pitch_R",
    "lid_up_L", "lid_up_R", "lid_low_L", "lid_low_R",
]

# C7 阈值：眼球 0.5°（会聚在 1.8 m 内有效所需的底线），颈部 1.0°，眼睑 2.0°
REQUIREMENT_DEG = {
    "eye": 0.5,
    "neck": 1.0,
    "lid": 2.0,
}

CALIB_DIR = os.path.join(ROOT, "calibration")

# 缺失项用保守默认补齐；与 p2_decision/config.py 的 CALIB_FALLBACK 保持一致
try:
    from p2_decision import config as _C
    FALLBACK = dict(_C.CALIB_FALLBACK)
    LIMIT_STEP = _C.LIMIT_STEP_DEG
    LIMIT_MARGIN_DEG = _C.LIMIT_MARGIN_DEG
    LIMIT_PROBE_MAX = dict(_C.LIMIT_PROBE_MAX_DEG)
    LIMIT_STALL_GAIN = _C.LIMIT_STALL_GAIN
    LIMIT_MIN_USABLE_DEG = _C.LIMIT_MIN_USABLE_DEG
    LIMIT_FALLBACK = dict(_C.LIMIT_FALLBACK)
except Exception:                      # 单独运行 bridge 时也不该炸
    FALLBACK = {
        "deadzone_deg": {"eye": 1.0, "neck": 1.5},
        "micro_amp_range_deg": [0.0, 0.0],
        "vergence_max_distance_m": 0.0,
        "watchdog_max_age_ms": 100,
        "neck_delay_ms": 200,
    }
    LIMIT_STEP = 1.0
    LIMIT_MARGIN_DEG = 1.5
    LIMIT_PROBE_MAX = {"eye": 32.0, "neck": 55.0, "lid": 95.0}
    LIMIT_STALL_GAIN = 0.20
    LIMIT_MIN_USABLE_DEG = 5.0
    LIMIT_FALLBACK = {}


def joint_group(name: str) -> str:
    if name.startswith("neck"):
        return "neck"
    if name.startswith("lid"):
        return "lid"
    return "eye"


def now_ms() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------------------- #
# 后端
# --------------------------------------------------------------------------- #
class BackendUnavailable(RuntimeError):
    pass


class Backend:
    name = "abstract"

    def capabilities(self) -> dict:
        return {"actuators": JOINTS, "has_encoder": False, "has_renderer": False}

    def command(self, joints: dict, ts_ms: int) -> None:
        raise NotImplementedError

    def read_pose(self) -> dict:
        raise NotImplementedError

    def read_frame(self):
        return None


class SimBackend(Backend):
    """伺服回路的测试替身：量化 + 噪声 + 回差 + 一阶响应。"""

    name = "sim"

    PROFILE = {
        # group: (tau_s, lsb_deg, noise_deg, backlash_deg, range_deg)
        "eye": (0.045, 0.090, 0.030, 0.120, 25.0),
        "neck": (0.220, 0.090, 0.040, 0.200, 45.0),
        "lid": (0.020, 0.500, 0.100, 0.300, 90.0),
    }

    def __init__(self, seed: int | None = None):
        self.rng = random.Random(seed)
        self.target = {j: 0.0 for j in JOINTS}
        self.pos = {j: 0.0 for j in JOINTS}
        self._last_dir = {j: 0 for j in JOINTS}
        self._slack = {j: 0.0 for j in JOINTS}
        self._t = time.monotonic()

    def capabilities(self) -> dict:
        return {"actuators": JOINTS, "has_encoder": True, "has_renderer": False,
                "backend": "sim"}

    def command(self, joints: dict, ts_ms: int) -> None:
        self._advance()
        for k, v in joints.items():
            if k in self.target:
                self.target[k] = float(v)

    def _range(self, j: str):
        """物理行程：眼睑只能 0..range（闭合方向为 0），其余 ±range。"""
        rng = self.PROFILE[joint_group(j)][4]
        if j.startswith("lid"):
            return 0.0, rng
        return -rng, rng

    def _advance(self) -> None:
        t = time.monotonic()
        dt = max(1e-4, t - self._t)
        self._t = t
        for j in JOINTS:
            tau, lsb, noise, backlash, rng = self.PROFILE[joint_group(j)]
            lo, hi = self._range(j)
            tgt = max(lo, min(hi, self.target[j]))
            cur = self.pos[j]
            step = (tgt - cur) * min(1.0, dt / tau)
            # 回差：换向时先吃掉 slack
            direction = 1 if step > 1e-9 else (-1 if step < -1e-9 else 0)
            if direction and self._last_dir[j] and direction != self._last_dir[j]:
                self._slack[j] = backlash
            if direction:
                self._last_dir[j] = direction
            if self._slack[j] > 0:
                eat = min(self._slack[j], abs(step))
                self._slack[j] -= eat
                step -= math.copysign(eat, step)
            self.pos[j] = cur + step

    def read_pose(self) -> dict:
        self._advance()
        out = {}
        for j in JOINTS:
            _, lsb, noise, _, _ = self.PROFILE[joint_group(j)]
            v = self.pos[j] + self.rng.gauss(0.0, noise)
            out[j] = round(round(v / lsb) * lsb, 3)   # 编码器量化
        return {"ts_ms": now_ms(), "joints_deg": out,
                "source": "sim-encoder", "confidence": 0.99}


class RealBackend(Backend):
    """真机后端占位：P3 落地后在这里接 TCP/共享内存。"""

    name = "real"

    def __init__(self, endpoint: str | None = None):
        self.endpoint = endpoint or os.environ.get("GAZE_P3", "tcp://127.0.0.1:5561")

    def capabilities(self) -> dict:
        return {"actuators": JOINTS, "has_encoder": True, "has_renderer": False,
                "backend": "real", "endpoint": self.endpoint}

    def command(self, joints: dict, ts_ms: int) -> None:
        raise BackendUnavailable(
            f"真机后端未就绪：P3 执行进程尚未实现（{self.endpoint}）。"
            "先用 --backend sim 走通流程。")

    def read_pose(self) -> dict:
        raise BackendUnavailable("真机后端未就绪：没有编码器回读。")


# --------------------------------------------------------------------------- #
# 拟合与测量
# --------------------------------------------------------------------------- #
def fit_camera(samples: list[dict]) -> dict:
    """u = fx*tan(pan) + cx ; v = fy*tan(tilt) + cy  →  分别最小二乘。
    样本按 axis 字段分堆：axis='h' 进水平，axis='v' 进垂直。"""
    h_pts = [(math.tan(math.radians(s["angle"])), s["u"])
             for s in samples if s.get("axis", "h") == "h" and "u" in s and "angle" in s]
    v_pts = [(math.tan(math.radians(s.get("tilt", s.get("pitch", 0)))), s["v"])
             for s in samples if s.get("axis") == "v" and "v" in s]

    if len(h_pts) < 3:
        raise ValueError("水平方向至少需要 3 个点（axis=h）")

    def _lsq(pts):
        n = len(pts)
        sx = sum(p[0] for p in pts)
        sy = sum(p[1] for p in pts)
        sxx = sum(p[0] ** 2 for p in pts)
        sxy = sum(p[0] * p[1] for p in pts)
        den = n * sxx - sx * sx
        if abs(den) < 1e-9:
            return None  # 共线
        f = (n * sxy - sx * sy) / den
        c = (sy - f * sx) / n
        resid = [abs(f * x + c - y) for x, y in pts]
        return f, c, math.sqrt(sum(r * r for r in resid) / n), max(resid)

    h_res = _lsq(h_pts)
    if h_res is None:
        raise ValueError("水平点共线（所有角度相同），无法拟合 fx/cx")
    fx, cx, rms_h, max_h = h_res
    if abs(fx) < 1e-3:
        raise ValueError("拟合 fx≈0，标定点退化（所有 u 相同）")
    result = {"fx": round(fx, 2), "cx": round(cx, 2),
              "fov_h_deg": round(2 * math.degrees(math.atan(320.0 / fx)), 2),
              "rms_px": round(rms_h, 2), "max_px": round(max_h, 2), "n": len(h_pts)}

    if len(v_pts) >= 3:
        # 共线保护：所有 tilt 相同则无法拟合 fy/cy
        unique_x = len({round(p[0], 4) for p in v_pts})
        if unique_x < 2:
            result["warn"] = f"垂直点共线（所有 tilt 相同），未拟合 fy/cy"
        else:
            v_res = _lsq(v_pts)
            if v_res is None:
                result["warn"] = "垂直点共线，未拟合 fy/cy"
            else:
                fy, cy, rms_v, max_v = v_res
                result.update({"fy": round(fy, 2), "cy": round(cy, 2),
                               "rms_v_px": round(rms_v, 2), "max_v_px": round(max_v, 2),
                               "n_v": len(v_pts)})
    else:
        result["warn"] = f"垂直点不足 {len(v_pts)}/3，未拟合 fy/cy"
    return result


def fit_hand_eye(points: list[dict]) -> dict:
    """off_pan = yaw0 ; off_tilt = pitch0 + degrees(atan(dy/d))。"""
    if len(points) < 3:
        raise ValueError("至少需要 3 个点")
    # 距离分布校验：dy 只在多距离下可辨识
    distances = set(round(p["d"], 1) for p in points)
    if len(distances) < 2:
        raise ValueError(f"需要至少 2 个不同距离（当前只有 {len(distances)} 个: {sorted(distances)}），否则 dy 不可辨识")
    for p in points:
        p["off_pan"] = p["pan_dial"] - p["pan_cam"]
        p["off_tilt"] = p["tilt_dial"] - p["tilt_cam"]
    n = len(points)
    yaw0 = statistics.mean(p["off_pan"] for p in points)

    # 用真实几何模型（atan），一维搜索 dy；pitch0 取残差均值。
    # 线性近似会对 3 点拟合产生 ~0.5° 的 pitch0 偏差，这里避免它。
    def sse(dy_m):
        p0 = statistics.mean(p["off_tilt"] - math.degrees(math.atan(dy_m / p["d"]))
                             for p in points)
        s = sum((p0 + math.degrees(math.atan(dy_m / p["d"])) - p["off_tilt"]) ** 2
                for p in points)
        return s, p0

    best_s, best_dy, best_p0 = None, 0.0, 0.0
    lo, hi = 0.0, 1.0
    for _ in range(3):                       # 粗搜 0.01 → 细搜 0.0001
        step = (hi - lo) / 100.0
        for i in range(101):
            dy = lo + i * step
            s, p0 = sse(dy)
            if best_s is None or s < best_s:
                best_s, best_dy, best_p0 = s, dy, p0
        lo, hi = max(0.0, best_dy - step), best_dy + step

    res = [abs(best_p0 + math.degrees(math.atan(best_dy / p["d"])) - p["off_tilt"])
           for p in points]
    return {"yaw0_deg": round(yaw0, 2), "pitch0_deg": round(best_p0, 2),
            "dy_m": round(best_dy, 3),
            "residual": {"max": round(max(res), 2),
                         "rms": round(math.sqrt(sum(r * r for r in res) / n), 2)}}


def fit_distance(samples: list[dict]) -> dict:
    """先按 d=K/p 拟合；若误差 >10% 退化为 d=A/p+B（两点式）。"""
    ks = [s["d"] * s["eye_dist_px"] for s in samples]
    K = statistics.median(ks)

    def errs_of(pred_fn):
        return {f'{s["d"]}m': round(abs(pred_fn(s) - s["d"]) / s["d"] * 100, 1)
                for s in samples}

    err_k = errs_of(lambda s: K / max(1.0, s["eye_dist_px"]))
    model, A, B, errs = "d=K/p", None, None, err_k
    if max(err_k.values()) > 10.0 and len(samples) >= 2:
        # d = A*(1/p) + B，最小二乘
        xs = [1.0 / max(1.0, s["eye_dist_px"]) for s in samples]
        ys = [s["d"] for s in samples]
        n = len(samples)
        sx, sy = sum(xs), sum(ys)
        sxx = sum(x * x for x in xs)
        sxy = sum(x * y for x, y in zip(xs, ys))
        den = n * sxx - sx * sx
        if abs(den) > 1e-12:
            A = (n * sxy - sx * sy) / den
            B = (sy - A * sx) / n
            cand = errs_of(lambda s: A / max(1.0, s["eye_dist_px"]) + B)
            if max(cand.values()) < max(err_k.values()):
                model, errs = "d=A/p+B", cand

    out = {"model": model, "K": round(K, 1),
           "samples": [[s["d"], s["eye_dist_px"]] for s in samples],
           "err_pct": errs, "max_err_pct": max(errs.values())}
    if model == "d=A/p+B":
        out.update({"A": round(A, 2), "B": round(B, 3)})
    return out


def measure_noise(backend: Backend, joint: str, seconds: float = 1.0) -> float:
    vals = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        vals.append(backend.read_pose()["joints_deg"][joint])
        time.sleep(0.02)
    return statistics.pstdev(vals) if len(vals) > 2 else 0.0


def settle(backend: Backend, joint_group_name: str, factor: float = 3.0) -> None:
    tau = SimBackend.PROFILE[joint_group_name][0] if isinstance(backend, SimBackend) else 0.2
    time.sleep(min(2.0, tau * factor))


def compute_derived(prec: dict) -> dict:
    """由精度反推运行时参数（docs §8.4）。"""
    r = prec["effective_step_deg"]
    eye = r.get("eye", 0.5)
    neck = r.get("neck", 1.0)
    res_v = math.sqrt(eye ** 2 + eye ** 2)
    # 最大会聚距离：由眼距(0.063m)和角分辨率决定
    max_d = 0.063 / (2 * math.tan(math.radians(max(0.05, 3 * res_v))))
    return {
        "cmd_quantum_deg": {"eye": eye, "neck": neck},
        "deadzone_deg": {"eye": round(2 * eye, 2), "neck": round(1.5 * neck, 2)},
        "micro_amp_range_deg": [round(eye, 2), round(max(1.0, eye), 2)],
        "vergence_resolution_deg": round(res_v, 2),
        "vergence_max_distance_m": round(max_d, 2),
    }


# --------------------------------------------------------------------------- #
# 会话：步骤状态 + 落盘
# --------------------------------------------------------------------------- #
class Session:
    def __init__(self, backend: Backend):
        self.backend = backend
        self.dir = os.path.join(CALIB_DIR, backend.name)
        os.makedirs(os.path.join(self.dir, "steps"), exist_ok=True)
        self.data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        step_dir = os.path.join(self.dir, "steps")
        for f in os.listdir(step_dir) if os.path.isdir(step_dir) else []:
            if f.endswith(".json"):
                try:
                    with open(os.path.join(step_dir, f), encoding="utf-8") as fh:
                        self.data[f[:-5]] = json.load(fh)
                except (json.JSONDecodeError, OSError) as e:
                    print(f"[calib] 跳过损坏的 {f}: {e}")

    def save(self, key: str, payload: dict) -> None:
        payload = dict(payload, saved_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
        self.data[key] = payload
        step_dir = os.path.join(self.dir, "steps")
        os.makedirs(step_dir, exist_ok=True)
        path = os.path.join(step_dir, f"{key}.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        self.merge_active()

    def save_samples(self, key: str, payload: dict) -> None:
        """采样专用：只落盘，不触发 merge_active（避免采1个点就标 complete）"""
        self.data[key] = payload
        step_dir = os.path.join(self.dir, "steps")
        os.makedirs(step_dir, exist_ok=True)
        path = os.path.join(step_dir, f"{key}.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def snapshot(self) -> str:
        """完整留档：把 steps/ 和 active.json 复制到 history/<ts>_<backend>/。"""
        ts = time.strftime("%Y%m%d_%H%M%S")
        dst = os.path.join(CALIB_DIR, "history", f"{ts}_{self.backend.name}")
        os.makedirs(dst, exist_ok=True)
        src = os.path.join(self.dir, "steps")
        if os.path.isdir(src):
            for f in os.listdir(src):
                if f.endswith(".json"):
                    shutil.copy2(os.path.join(src, f), os.path.join(dst, f))
        act = os.path.join(self.dir, "active.json")
        if os.path.isfile(act):
            shutil.copy2(act, os.path.join(dst, "active.json"))
        return dst

    def merge_active(self) -> dict:
        d = self.data
        runtime = {
            "fov_h_deg": d.get("c1_camera", {}).get("fov_h_deg"),
            "fx": d.get("c1_camera", {}).get("fx"),
            "fy": d.get("c1_camera", {}).get("fy"),
            "cx": d.get("c1_camera", {}).get("cx"),
            "cy": d.get("c1_camera", {}).get("cy"),
            "yaw0_deg": d.get("c2_hand_eye", {}).get("yaw0_deg"),
            "pitch0_deg": d.get("c2_hand_eye", {}).get("pitch0_deg"),
            "dy_m": d.get("c2_hand_eye", {}).get("dy_m"),
            "dist_k": d.get("c4_distance", {}).get("K"),
            "dist_a": d.get("c4_distance", {}).get("A", 0.0),
            "dist_b": d.get("c4_distance", {}).get("B", 0.0),
            "deadzone_deg": d.get("c7_precision", {}).get("derived", {}).get("deadzone_deg"),
            "cmd_quantum_deg": d.get("c7_precision", {}).get("derived", {}).get("cmd_quantum_deg"),
            "micro_amp_range_deg": d.get("c7_precision", {}).get("derived", {}).get("micro_amp_range_deg"),
            "vergence_max_distance_m": d.get("c7_precision", {}).get("derived", {}).get("vergence_max_distance_m"),
            "watchdog_max_age_ms": d.get("c5_latency", {}).get("watchdog_max_age_ms"),
            "neck_delay_ms": d.get("c5_latency", {}).get("neck_delay_ms"),
            "look_deg_per_weight": d.get("c6_look", {}).get("deg_per_weight"),
            "limits_deg": d.get("c3_servo", {}).get("limits"),
            "channels": d.get("c3_servo", {}).get("channels", {}),
        }
        # complete 按关键字段判定，不是"有 samples 就算完成"
        def _complete(k):
            step = d.get(k, {})
            if k == "c1_camera":
                return step.get("fx") is not None
            if k == "c2_hand_eye":
                return step.get("yaw0_deg") is not None
            if k == "c3_servo":
                return step.get("limits") is not None
            if k == "c4_distance":
                return step.get("K") is not None
            if k == "c5_latency":
                return step.get("watchdog_max_age_ms") is not None
            if k == "c6_look":
                return step.get("joints") is not None
            if k == "c7_precision":
                return step.get("derived") is not None
            return False
        active = {
            "schema": 1, "backend": self.backend.name,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "complete": {k: _complete(k) for k in
                         ("c1_camera", "c2_hand_eye", "c3_servo", "c4_distance",
                          "c5_latency", "c6_look", "c7_precision")},
            "runtime": runtime,
            "steps": {k: v for k, v in d.items()},
        }
        # 缺项不要留 null：消费者会误判。用保守默认补齐（docs §9.3）
        for key, fallback in FALLBACK.items():
            if runtime.get(key) is None:
                runtime[key] = fallback
                active.setdefault("fallback_used", []).append(key)
        if runtime.get("limits_deg") is None:
            runtime["limits_deg"] = LIMIT_FALLBACK
            active.setdefault("fallback_used", []).append("limits_deg")
        os.makedirs(self.dir, exist_ok=True)
        path = os.path.join(self.dir, "active.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(active, fh, ensure_ascii=False, indent=2)
        return active


# --------------------------------------------------------------------------- #
# 七个步骤
# --------------------------------------------------------------------------- #
STEP_ORDER = ["c1", "c2", "c3", "c4", "c5", "c6", "c7"]

STEP_META = {
    "c1": {"key": "c1_camera", "title": "相机内参",
           "mode": "manual",
           "desc": "把标记放到画面里已知角度的位置，逐点采集；后端拟合 f / cx。"},
    "c2": {"key": "c2_hand_eye", "title": "手眼零位",
           "mode": "manual",
           "desc": "按提示站位（3 距离 × 5 点），用方向键把视线对到标记上，逐点采集。"},
    "c3": {"key": "c3_servo", "title": "舵机粗标",
           "mode": "auto",
           "desc": "自动扫角，拟合零点/方向/量程。"},
    "c4": {"key": "c4_distance", "title": "距离标定",
           "mode": "manual",
           "desc": "站到 0.5 / 1.0 / 2.0 m，各采集一次眼距像素。"},
    "c5": {"key": "c5_latency", "title": "延迟自标定",
           "mode": "auto",
           "desc": "自动 ping-pong + 阶跃，得到 IPC p99 与各关节 t90。"},
    "c6": {"key": "c6_look", "title": "量程与饱和",
           "mode": "auto",
           "desc": "逐级加大指令，找到响应饱和点，确定有效量程。"},
    "c7": {"key": "c7_precision", "title": "精度标定",
           "mode": "auto",
           "desc": "分辨率/重复性/回差/噪声，判定是否满足 0.5°，并反推运行时参数。"},
}


class StepRunner:
    """把每个步骤的自动流程跑完，期间通过回调推送进度。"""

    def __init__(self, session: Session, send):
        self.s = session
        self.b = session.backend
        self.send = send

    async def _emit(self, **kw):
        await self.send(kw)

    async def run(self, step: str) -> dict:
        handler = getattr(self, f"_run_{step}", None)
        if handler is None:
            raise ValueError(f"未知步骤 {step}")
        return await handler()

    # ---------------- C3：3a 极限 → 3b 零点/线性 ----------------
    async def _settle(self, grp: str, factor: float = 3.0) -> None:
        """阻塞式等待放到线程里，避免把 WS 事件循环卡死（会触发 ping 超时）。"""
        await asyncio.to_thread(settle, self.b, grp, factor)

    async def _read(self) -> dict:
        return await asyncio.to_thread(self.b.read_pose)

    async def _measure_limit(self, joint: str, sign: int, probe_max: float) -> dict:
        """从中心单向外走，直到走不动；返回最后到达的位置。

        判据（任一满足即停）：
          1) 编码器增量 < LIMIT_STALL_GAIN × 指令步长，连续 2 步；
          2) 后端回传 stall/fault；
          3) 到达 probe_max（设计名义值 ×1.15，防止一直顶）。
        """
        grp = joint_group(joint)
        step = LIMIT_STEP if grp == "eye" else 2.0     # 大关节用 2°，减少步数
        self.b.command({joint: 0.0}, now_ms())
        await self._settle(grp, 4)
        cmd, last_good, stalled = 0.0, 0.0, 0
        while cmd < probe_max:
            cmd += step
            self.b.command({joint: sign * cmd}, now_ms())
            await self._settle(grp, 2)
            pose = await self._read()
            act = abs(pose["joints_deg"][joint])
            fault = (pose.get("fault") or {}).get(joint)
            moved = act - last_good
            if fault:
                break
            if moved < LIMIT_STALL_GAIN * step:
                stalled += 1
                if stalled >= 2:
                    break
            else:
                stalled = 0
                last_good = act
            await asyncio.sleep(0)
        return {"raw": round(last_good, 1), "cmd_stop": round(cmd, 1),
                "method": "stall" if stalled >= 2 else "probe_max"}

    async def _run_c3(self) -> dict:
        limits = {}
        total = len(JOINTS)
        # ---- 3a：极限（成对） ----
        for idx, j in enumerate(JOINTS):
            probe = LIMIT_PROBE_MAX[joint_group(j)]
            pos = await self._measure_limit(j, +1, probe)
            neg = await self._measure_limit(j, -1, probe)
            # 一侧完全动不了（例如眼睑下限就是 0）→ 该侧边界取 0，不留负余量
            hi = pos["raw"] - LIMIT_MARGIN_DEG if pos["raw"] >= 2 * LIMIT_STEP else 0.0
            lo = -(neg["raw"] - LIMIT_MARGIN_DEG) if neg["raw"] >= 2 * LIMIT_STEP else 0.0
            hi = max(hi, 0.0)
            # 眼睑是单向轴（0 = 全开），不能因为"负侧为 0"就判异常
            one_sided = joint_group(j) == "lid"
            ok = (hi - lo >= 2 * LIMIT_MIN_USABLE_DEG) and (
                one_sided or min(abs(hi), abs(lo)) >= 1.0)
            limits[j] = {"neg": round(lo, 1), "pos": round(hi, 1),
                         "raw_neg": -neg["raw"], "raw_pos": pos["raw"],
                         "margin": LIMIT_MARGIN_DEG,
                         "asym_deg": round(pos["raw"] - neg["raw"], 1),
                         "method": pos["method"], "usable": ok}
            await self._emit(evt="calib.progress", step="c3", phase="极限",
                             done=idx + 1, total=total, note=j)
            await asyncio.sleep(0)

        # ---- 3b：零位/方向/线性（只在极限内扫，按区间比例取点） ----
        channels = {}
        for idx, j in enumerate(JOINTS):
            lim = limits[j]
            lo, hi = lim["neg"], lim["pos"]
            span = hi - lo
            cmds = [lo + span * k for k in (0.1, 0.3, 0.5, 0.7, 0.9)]
            pts = []
            for cmd in cmds:
                self.b.command({j: cmd}, now_ms())
                await self._settle(joint_group(j))
                pts.append([cmd, (await self._read())["joints_deg"][j]])
            n = len(pts)
            sx = sum(p[0] for p in pts)
            sy = sum(p[1] for p in pts)
            sxx = sum(p[0] ** 2 for p in pts)
            sxy = sum(p[0] * p[1] for p in pts)
            den = n * sxx - sx * sx
            scale = (n * sxy - sx * sy) / den if abs(den) > 1e-9 else 1.0
            offset = (sy - scale * sx) / n
            pred = [scale * p[0] + offset for p in pts]
            ss_res = sum((p[1] - q) ** 2 for p, q in zip(pts, pred))
            ss_tot = sum((p[1] - statistics.mean([q[1] for q in pts])) ** 2 for p in pts)
            r2 = 1 - ss_res / ss_tot if ss_tot > 1e-12 else 1.0
            channels[j] = {"sign": 1 if scale >= 0 else -1, "scale": round(scale, 4),
                           "offset": round(offset, 3),
                           "min": lim["neg"], "max": lim["pos"],
                           "r2": round(r2, 5)}
            await self._emit(evt="calib.progress", step="c3", phase="线性",
                             done=idx + 1, total=total, note=j)
            await asyncio.sleep(0)

        bad_limits = [k for k, v in limits.items() if not v["usable"]]
        # C3a 判定：非眼睑轴左右不对称 > 3° 说明装配偏了
        asym_bad = [k for k, v in limits.items()
                    if not k.startswith("lid") and abs(v["asym_deg"]) > 3.0]
        # C3b 判定：零位误差超容差（眼/颈 0.3°，眼睑按 LSB 放宽到 1.0°）
        zero_tol = {"eye": 0.3, "neck": 0.3, "lid": 1.0}
        zero_bad = [k for k, v in channels.items()
                    if abs(v["offset"]) > zero_tol[joint_group(k)]]
        bad_lin = [k for k, v in channels.items() if v["r2"] <= 0.999]
        payload = {"limits": limits, "channels": channels,
                   "backend": self.b.name}
        self.s.save("c3_servo", payload)
        verdict = "pass" if not (bad_limits or asym_bad or zero_bad or bad_lin) else "warn"
        notes = []
        if bad_limits: notes.append(f"极限不可用: {bad_limits}")
        if asym_bad:   notes.append(f"左右不对称>3°: {asym_bad}")
        if zero_bad:   notes.append(f"零位偏差>0.3°: {zero_bad}")
        if bad_lin:    notes.append(f"线性 R²<0.999: {bad_lin}")
        return {"verdict": verdict, "payload": payload, "note": "；".join(notes)}

    async def run_c3_quick(self) -> dict:
        """开机复验：不重新摸极限，只验证已标定极值的 90% 仍可达。"""
        saved = self.s.data.get("c3_servo", {}).get("limits") or {}
        checked, bad = {}, []
        for j in JOINTS:
            lim = saved.get(j)
            if not lim:
                continue
            tgt_p = lim["pos"] * 0.9
            tgt_n = lim["neg"] * 0.9
            self.b.command({j: 0.0}, now_ms())
            await self._settle(joint_group(j), 4)
            self.b.command({j: tgt_p}, now_ms())
            await self._settle(joint_group(j), 3)
            got_p = (await self._read())["joints_deg"][j]
            self.b.command({j: tgt_n}, now_ms())
            await self._settle(joint_group(j), 3)
            got_n = (await self._read())["joints_deg"][j]
            # lid 单侧轴（neg≈0）只查正向；双侧轴查正反
            if abs(tgt_n) < 1.0:
                ok = got_p > tgt_p * 0.85
            else:
                ok = got_p > tgt_p * 0.85 and got_n < tgt_n * 0.85
            checked[j] = {"pos": round(got_p, 1), "neg": round(got_n, 1), "ok": ok}
            if not ok:
                bad.append(j)
            await self._emit(evt="calib.progress", step="c3", phase="复验",
                             done=len(checked), total=len(saved), note=j)
        self.b.command({j: 0.0 for j in JOINTS}, now_ms())
        return {"verdict": "pass" if not bad else "warn",
                "saved": False,  # quick 复验不落盘
                "payload": {"checked": checked, "bad": bad}}

    # ---------------- C5 ----------------
    async def _run_c5(self) -> dict:
        # 端口到端口往返（含桥自身开销）
        rtts = []
        for _ in range(200):
            t0 = time.perf_counter()
            self.b.command({"eye_yaw_L": 0.0}, now_ms())
            await asyncio.to_thread(self.b.read_pose)
            rtts.append((time.perf_counter() - t0) * 1000)
        rtts.sort()
        p99 = rtts[int(len(rtts) * 0.99) - 1]
        # 阶跃 t90
        mech = {}
        for j in ("eye_pitch_L", "neck_pitch"):
            self.b.command({j: 0.0}, now_ms())
            await asyncio.to_thread(settle, self.b, joint_group(j), 4)
            base = (await asyncio.to_thread(self.b.read_pose))["joints_deg"][j]
            self.b.command({j: base + 2.0}, now_ms())
            t0 = time.monotonic()
            t90 = None
            while time.monotonic() - t0 < 2.0:
                cur = (await asyncio.to_thread(self.b.read_pose))["joints_deg"][j]
                if abs(cur - base) >= 1.8:
                    t90 = (time.monotonic() - t0) * 1000
                    break
                await asyncio.sleep(0.005)
            mech[j] = {"t90_ms": round(t90, 1) if t90 else None}
            await self._emit(evt="calib.progress", step="c5", done=1, total=2, note=j)
        watchdog = max(100, round(5 * p99))
        extra = ((mech.get("neck_pitch", {}).get("t90_ms") or 200)
                 - (mech.get("eye_pitch_L", {}).get("t90_ms") or 25))
        # 颈延迟落在 120–400 ms，超出说明机械异常，只告警不直接采信
        neck_delay = max(120.0, min(400.0, 150.0 + extra))
        payload = {"ipc": {"rtt_ms": {"mean": round(statistics.mean(rtts), 3),
                                      "p99": round(p99, 3)}},
                   "mech_ms": mech,
                   "watchdog_max_age_ms": watchdog,
                   "neck_delay_ms": neck_delay,
                   "backend": self.b.name}
        self.s.save("c5_latency", payload)
        return {"verdict": "pass" if p99 < 5 else "warn", "payload": payload}

    # ---------------- C6 ----------------
    async def _run_c6(self) -> dict:
        res = {}
        for j in ("eye_yaw_L", "eye_yaw_R", "eye_pitch_L", "eye_pitch_R"):
            self.b.command({j: 0.0}, now_ms())
            await asyncio.to_thread(settle, self.b, "eye", 4)
            prev_cmd = prev_act = 0.0
            sat = None
            cmd = 0.0
            while cmd < 35.0:
                cmd += 2.0
                self.b.command({j: cmd}, now_ms())
                await asyncio.to_thread(settle, self.b, "eye", 3)
                act = abs((await asyncio.to_thread(self.b.read_pose))["joints_deg"][j])
                d_cmd = cmd - prev_cmd
                d_act = act - abs(prev_act)
                if d_act < 0.5 * d_cmd:
                    sat = abs(prev_act)
                    break
                prev_cmd, prev_act = cmd, act
            res[j] = {"saturation_deg": round(sat if sat is not None else cmd, 1),
                      "range_deg": round(sat if sat is not None else cmd, 1),
                      "unsaturated": sat is None}  # 未找到饱和=量程大于测试范围
            # P1-12: 测完回中，不留饱和位
            self.b.command({j: 0.0}, now_ms())
            await asyncio.to_thread(settle, self.b, "eye", 2)
            await self._emit(evt="calib.progress", step="c6", done=len(res), total=4, note=j)
        weights = (self.b.capabilities() or {}).get("has_renderer")
        # sim 后端：weight 0..1 对应 0..range_deg，可直接算 deg_per_weight
        dpl = {}
        for j, v in res.items():
            dpl[j] = round(v["range_deg"], 2)
        payload = {"joints": res, "backend": self.b.name,
                   "deg_per_weight": dpl,
                   "deg_per_weight_note": (
                       "渲染端实测" if weights else "sim 估算：range_deg/1.0")}
        # 量程>=20°即合格；未找到饱和说明量程>35°（更宽，也算合格）
        ok = all(v["range_deg"] >= 20.0 for v in res.values())
        self.s.save("c6_look", payload)
        return {"verdict": "pass" if ok else "warn", "payload": payload}

    # ---------------- C7 辅助：重复性 / 回差 ----------------
    async def _repeatability(self, joint: str, grp: str, delta: float, n: int) -> float:
        """M2：同一方向、同一目标，重复 n 次的标准差。"""
        vals = []
        for _ in range(n):
            self.b.command({joint: 0.0}, now_ms())
            await self._settle(grp, 3)
            self.b.command({joint: delta}, now_ms())
            await self._settle(grp, 3)
            vals.append((await self._read())["joints_deg"][joint])
            await asyncio.sleep(0)
        return statistics.pstdev(vals) if len(vals) > 2 else 0.0

    async def _backlash(self, joint: str, grp: str, low: float, high: float) -> float:
        """M3：从两侧分别接近同一目标，落点差即回差。"""
        self.b.command({joint: low}, now_ms())
        await self._settle(grp, 3)
        self.b.command({joint: high}, now_ms())
        await self._settle(grp, 3)
        a = (await self._read())["joints_deg"][joint]

        self.b.command({joint: high + 2.0}, now_ms())
        await self._settle(grp, 3)
        self.b.command({joint: high}, now_ms())
        await self._settle(grp, 3)
        b = (await self._read())["joints_deg"][joint]
        return abs(a - b)

    # ---------------- C7 ----------------
    async def _run_c7(self, quick: bool = False) -> dict:
        amps = [0.5] if quick else [0.05, 0.1, 0.2, 0.3, 0.5, 1.0]
        trials = 1 if quick else 5
        channels = ["eye_pitch_L", "eye_yaw_L", "neck_pitch"] if quick else JOINTS
        per = {}
        total = len(channels)
        for idx, j in enumerate(channels):
            grp = joint_group(j)
            # 先回中并稳定，再测噪声 —— 顺序反了会把上一条通道的余动算进噪声
            self.b.command({j: 0.0}, now_ms())
            await asyncio.to_thread(settle, self.b, grp, 4)
            noise = await asyncio.to_thread(measure_noise, self.b, j, 0.4 if quick else 1.0)
            self.b.command({j: 0.0}, now_ms())
            await asyncio.to_thread(settle, self.b, grp, 4)
            res = None
            for delta in amps:
                deltas = []
                for _ in range(trials):
                    self.b.command({j: delta}, now_ms())
                    await asyncio.to_thread(settle, self.b, grp)
                    e1 = (await asyncio.to_thread(self.b.read_pose))["joints_deg"][j]
                    self.b.command({j: 0.0}, now_ms())
                    await asyncio.to_thread(settle, self.b, grp)
                    e0 = (await asyncio.to_thread(self.b.read_pose))["joints_deg"][j]
                    deltas.append(abs(e1 - e0))
                if statistics.median(deltas) > max(3 * noise, 0.02):
                    res = delta
                    break
            # LSB 从当前 backend 读取，仅 sim 后端用 PROFILE 常量
            if isinstance(self.b, SimBackend):
                lsb = SimBackend.PROFILE[grp][1]
            else:
                lsb = (self.b.capabilities() or {}).get("lsb_deg")
                if lsb is None:
                    lsb = 0.1  # 真机默认 0.1°，待 RealBackend 接入编码器分辨率
            # M2 重复性 & M3 回差（quick 模式跳过，太慢）
            if not quick:
                sigma = await self._repeatability(j, grp, 1.0, 20)
                backlash = await self._backlash(j, grp, 1.0, 3.0)
            else:
                sigma = backlash = 0.0
            # r_eff 取四者的上界：分辨率 / 3σ / 回差一半 / 编码器 LSB
            eff = max(res or 999, 3 * noise, backlash / 2.0, lsb)
            per[j] = {"noise_deg": round(noise, 3),
                      "resolution_deg": round(res or 999, 2),
                      "repeatability_deg": round(sigma, 3),
                      "backlash_deg": round(backlash, 3),
                      "encoder_lsb_deg": lsb,
                      "effective_step_deg": round(eff, 3),
                      "requirement_deg": REQUIREMENT_DEG[grp],
                      "pass": eff <= REQUIREMENT_DEG[grp]}
            await self._emit(evt="calib.progress", step="c7", done=idx + 1,
                             total=total, note=j)
            await asyncio.sleep(0)
        eff = {g: max(v["effective_step_deg"] for k, v in per.items()
                      if joint_group(k) == g) for g in ("eye", "neck")}
        derived = compute_derived({"effective_step_deg": eff})
        payload = {"mode": "quick" if quick else "full",
                   "channels": per, "derived": derived,
                   "overall": {"pass": all(v["pass"] for v in per.values()),
                               "failed": [k for k, v in per.items() if not v["pass"]]},
                   "backend": self.b.name}
        if not quick:
            if payload["overall"]["pass"]:
                self.s.save("c7_precision", payload)
            else:
                # 失败时不落 derived（999哨兵会锁死关节），只存诊断
                diag = {k: v for k, v in payload.items() if k != "derived"}
                self.s.save("c7_precision", diag)
            try:
                self.s.snapshot()
            except OSError:
                pass
        return {"verdict": "pass" if payload["overall"]["pass"] else "warn",
                "saved": not quick,  # quick 不落盘
                "payload": payload}

    # ---------------- C7 quick（开机用，独立入口） ----------------
    async def run_c7_quick(self) -> dict:
        return await self._run_c7(quick=True)


# --------------------------------------------------------------------------- #
# WebSocket 服务
# --------------------------------------------------------------------------- #
class Bridge:
    def __init__(self, backend: Backend):
        self.session = Session(backend)
        try:                       # 与 harness 用同一份标定值
            from p2_decision import calibration as _calib
            self.look_scale = float(_calib.load().look_deg_per_weight)
        except Exception:
            self.look_scale = 30.0
        self.runner = StepRunner(self.session, self._send)
        self.clients: set = set()
        self._gaze = None            # 最近一帧数字人可直接消费的姿态
        self._gaze_until = 0.0

    async def gaze_loop(self):
        """标定摆位期间以 30Hz 重发姿态帧——数字人 500ms 没收到会清空 remote。"""
        while True:
            if self._gaze and time.monotonic() < self._gaze_until:
                await self._send(self._gaze)
            await asyncio.sleep(1.0 / 30.0)

    async def _send(self, msg: dict) -> None:
        if not self.clients:
            return
        data = json.dumps(msg, ensure_ascii=False)
        await asyncio.gather(*[c.send(data) for c in list(self.clients)],
                             return_exceptions=True)

    async def handler(self, ws):
        self.clients.add(ws)
        # saved 按 active.complete 过滤（只采样未拟合的不算完成）
        active_path = os.path.join(self.session.dir, "active.json")
        try:
            with open(active_path, encoding="utf-8") as fh:
                active = json.load(fh)
        except (OSError, json.JSONDecodeError):
            active = {}
        complete = active.get("complete", {})
        saved = sorted([k for k, v in complete.items() if v])
        await ws.send(json.dumps({
            "evt": "hello", "backend": self.session.backend.name,
            "capabilities": self.session.backend.capabilities(),
            "steps": [{"id": k, **STEP_META[k]} for k in STEP_ORDER],
            "saved": saved,
        }, ensure_ascii=False))
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                    await self.on_command(msg)
                except Exception as exc:      # 单条命令失败不影响连接
                    await ws.send(json.dumps(
                        {"evt": "error", "message": f"{type(exc).__name__}: {exc}"},
                        ensure_ascii=False))
        finally:
            self.clients.discard(ws)

    async def on_command(self, msg: dict) -> None:
        cmd = msg.get("cmd")
        if cmd == "calib.start":
            step = msg.get("step")
            meta = STEP_META.get(step)
            if not meta:
                raise ValueError(f"未知步骤 {step}")
            await self._send({"evt": "step.begin", "step": step, **meta})
        elif cmd == "calib.sample":
            step = msg["step"]
            key = STEP_META[step]["key"]
            # 注意：不能用 data.get(key, {}) —— 那会写进临时字典，count 永远是 1
            bucket = self.session.data.setdefault(key, {})
            samples = bucket.setdefault("samples", [])
            samples.append(msg["payload"])
            # 采样落盘但不触发 merge_active（拟合后才写 active.json）
            self.session.save_samples(key, bucket)
            await self._send({"evt": "step.sample", "step": step,
                              "count": len(samples), "payload": msg["payload"]})
        elif cmd == "calib.fit":
            await self._fit(msg["step"])
        elif cmd == "calib.run":
            step = msg["step"]
            if msg.get("quick") and step == "c7":
                res = await self.runner.run_c7_quick()
            elif msg.get("quick") and step == "c3":
                res = await self.runner.run_c3_quick()
            else:
                res = await self.runner.run(step)
            await self._send({"evt": "calib.result", "step": step, **res})
        elif cmd == "calib.status":
            # 只读操作，从磁盘读 active.json（不调 merge_active 重写）
            active_path = os.path.join(self.session.dir, "active.json")
            try:
                with open(active_path, encoding="utf-8") as fh:
                    active = json.load(fh)
            except (OSError, json.JSONDecodeError):
                active = {}
            # saved 按 active.complete 过滤（只采样未拟合的不算完成）
            complete = active.get("complete", {})
            saved = sorted([k for k, v in complete.items() if v])
            await self._send({"evt": "calib.status",
                              "saved": saved,
                              "clients": len(self.clients),
                              "active": active})
        elif cmd == "calib.command":
            # 标定向导在 C2 里手动摆位：驱动后端，同时广播给数字人渲染端
            joints = msg.get("joints") or {}
            print(f"[calib] ← command eye={joints.get('eye_yaw_L')} "
                  f"tilt={joints.get('eye_pitch_L')} 客户端={len(self.clients)}", flush=True)
            if joints:
                self.session.backend.command(joints, now_ms())
            pose = msg.get("pose") or self._pose_from_joints(joints)
            ttl_ms = int(msg.get("ttl_ms", 3000))
            # 给数字人的数据帧：字段与 harness 一致，index.html 直接能吃
            self._gaze = {
                "neck_yaw": pose.get("neck_yaw", 0.0),
                "neck_pitch": pose.get("neck_pitch", 0.0),
                "neck_roll": pose.get("neck_roll", 0.0),
                "eye_yaw": pose.get("eye_yaw", 0.0),
                "eye_tilt": pose.get("eye_tilt", 0.0),
                "eye_offset": pose.get("eye_offset", 0.0),
                "blink": 1.0,
                # 与 harness payload 保持同一套眨眼字段；标定期间不播眨眼
                "blink_age_ms": None,
                "blink_ms": 150,
                "blink_close": 0.35,
                "blink_hold": 0.55,
                "lid_closure": pose.get("lid_closure", 0.0),
                "look_scale": self.look_scale,
                "source": "calib", "state": "calib",
                "ts": now_ms(),
            }
            self._gaze_until = time.monotonic() + ttl_ms / 1000.0
            await self._send(self._gaze)
            await self._send({"evt": "calib.pose", "pose": pose, "ttl_ms": ttl_ms})
        elif cmd == "calib.abort":
            await self._send({"evt": "calib.aborted"})
        else:
            raise ValueError(f"未知命令 {cmd}")

    @staticmethod
    def _pose_from_joints(joints: dict) -> dict:
        """11 通道指令 → index.html 认识的 pose 字段。"""
        def g(k, d=0.0):
            return float(joints.get(k, d))
        return {
            "neck_yaw": g("neck_yaw"), "neck_pitch": g("neck_pitch"),
            "neck_roll": g("neck_roll"),
            "eye_yaw": (g("eye_yaw_L") + g("eye_yaw_R")) / 2.0,
            "eye_tilt": (g("eye_pitch_L") + g("eye_pitch_R")) / 2.0,
            "eye_offset": (g("eye_yaw_L") - g("eye_yaw_R")) / 2.0,   # 会聚：正=内聚
            "lid_closure": g("lid_up_L"),
        }

    async def _fit(self, step: str) -> None:
        key = STEP_META[step]["key"]
        raw = self.session.data.get(key, {})
        samples = raw.get("samples", [])
        # 防御：如果samples为空（可能session重置），从落盘文件恢复
        if not samples:
            path = os.path.join(self.session.dir, "steps", f"{key}.json")
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    saved = json.load(fh)
                samples = saved.get("samples", [])
                if samples:
                    raw = saved
        if step == "c1":
            payload = fit_camera(samples)
        elif step == "c2":
            payload = fit_hand_eye(samples)
        elif step == "c4":
            payload = fit_distance(samples)
        else:
            raise ValueError(f"{step} 不是手动拟合步骤")
        # 拟合结果保留 samples，避免覆盖后无法重新拟合
        payload["samples"] = samples
        self.session.save(key, payload)
        verdict = "pass"
        if step == "c1" and (payload["rms_px"] > 1.5
                             or not (55.0 <= payload["fov_h_deg"] <= 85.0)
                             or payload.get("warn")):
            verdict = "warn"
        if step == "c2" and payload["residual"]["max"] > 2.0:
            verdict = "warn"
        if step == "c4" and payload.get("max_err_pct", 0) > 10.0:
            verdict = "warn"
        await self._send({"evt": "calib.result", "step": step,
                          "verdict": verdict, "payload": payload})


# --------------------------------------------------------------------------- #
# 静态服务 + 入口
# --------------------------------------------------------------------------- #
def free_port(start: int, tries: int = 5) -> int:
    """从 start 起找一个可用端口；找不到就原样返回（让 bind 自己报错）。"""
    import socket
    for p in range(start, start + tries):
        with socket.socket() as sk:
            try:
                sk.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start


def serve_http(directory: str, port: int) -> ThreadingHTTPServer:
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=directory, **kw)

        def log_message(self, *a):    # 静音
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


async def main_async(args) -> None:
    backend = SimBackend(seed=args.seed) if args.backend == "sim" else RealBackend(args.p3)
    bridge = Bridge(backend)
    http_port = free_port(args.http)
    ws_port = free_port(args.ws)
    if http_port != args.http:
        print(f"[calib] {args.http} 被占用，改用 {http_port}")
    serve_http(os.path.join(ROOT, "3d"), http_port)
    print(f"[calib] 标定页 http://127.0.0.1:{http_port}/calib.html")
    print(f"[calib] 数字人 http://127.0.0.1:{http_port}/index.html")
    print(f"[calib] WS    ws://127.0.0.1:{ws_port}")
    print(f"[calib] 后端  {backend.name}  输出目录 {bridge.session.dir}")
    print("[calib] 只跑这一个进程即可：不需要 ws_bridge，也不需要另起 8000 静态服务")
    # ping_interval=None：标定单步可能连续动 1–2 分钟，默认 20s 心跳会把长步骤判死
    websockets = __import__("websockets")
    for offset in range(5):
        port = ws_port + offset
        try:
            server = await websockets.serve(
                bridge.handler, "127.0.0.1", port,
                ping_interval=None, max_queue=256)
            break
        except OSError:
            if offset == 4:
                print(f"[calib] 端口 {ws_port}–{ws_port + 4} 都被占用。"
                      "可能已有一个标定桥在跑；换端口重试：\n"
                      f"        python -m tools.calib_bridge --http {http_port + 100}")
                return
            print(f"[calib] {port} 被占用，改用 {port + 1}")
    if port != ws_port:
        print(f"[calib] WS 改用 {port}")
    asyncio.create_task(bridge.gaze_loop())
    async with server:
        await asyncio.Future()


def main() -> None:
    ap = argparse.ArgumentParser(description="标定向导桥")
    ap.add_argument("--backend", choices=["sim", "real"], default="sim")
    ap.add_argument("--http", type=int, default=9100)
    ap.add_argument("--ws", type=int, default=9101)
    ap.add_argument("--p3", default=None, help="真机 P3 端点")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n[calib] 退出")


if __name__ == "__main__":
    main()
