"""标定配置的**唯一入口**（docs/CALIBRATION_SPEC.md §9）。

设计原则：
  * 一份 `calibration/<backend>/active.json`，只读一次，冻结成 `CalibConfig`；
  * 缺项、非法、缺失文件一律回退保守默认，**绝不抛异常打死启动**；
  * 所有模块（几何 / 关节映射 / 行为层 / P3 看门狗）只从 `CalibConfig` 取参数，
    不允许各自去读文件或写死常量。

用法：
    cfg = load()                 # 自动按 backend 探测；失败返回默认
    cfg = load(path="...json")   # 指定文件
    apply(cfg)                   # 注入软限位等全局状态
    cfg.warnings                 # 加载过程中的降级记录，给日志/HUD 用
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from . import config as C

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass(frozen=True)
class CalibConfig:
    """运行时参数快照。frozen=True：加载后不允许改，避免运行期漂移。"""
    backend: str = "default"
    loaded: bool = False
    source: str = ""

    # C1
    fx: float = 450.0
    fy: float = 450.0
    cx: float = 320.0
    cy: float = 240.0
    fov_h_deg: float = C.FOV_H_DEG

    # C2
    yaw0_deg: float = 0.0
    pitch0_deg: float = 0.0
    dy_m: float = 0.30

    # C4
    dist_k: float = 48.0
    dist_a: float = 0.0  # d = A/p + B，未拟合则 0（退回 K/eye_px）
    dist_b: float = 0.0

    # C3a
    limits: dict = field(default_factory=dict)
    # C3b: sign/scale/offset per joint（P3 消费，当前仅存储）
    channels: dict = field(default_factory=dict)

    # C7
    deadzone_deg: dict = field(default_factory=dict)
    micro_amp_range_deg: tuple = (0.0, 0.0)
    vergence_max_distance_m: float = 0.0
    cmd_quantum_deg: dict = field(default_factory=dict)

    # C5
    watchdog_max_age_ms: int = 100
    neck_delay_ms: float = 200.0

    # C6
    look_deg_per_weight: float = C.LOOK_DEG_PER_WEIGHT

    warnings: tuple = ()

    # ---------------- 便捷派生 ----------------
    @property
    def has_camera(self) -> bool:
        return (self.fx > 1.0 and self.cx > 1.0
                and 0 < self.cy < 1000 and self.fy > 1.0)

    def limit_of(self, joint: str) -> tuple:
        """返回 (lo, hi)；未标定用 LIMIT_FALLBACK，再不行用对称软限位。"""
        lim = self.limits.get(joint) or C.LIMIT_FALLBACK.get(joint)
        if lim:
            try:
                return float(lim["neg"]), float(lim["pos"])
            except (KeyError, TypeError, ValueError):
                pass
        return (-C.EYE_MAX_DEG, C.EYE_MAX_DEG)

    def deadzone_for(self, group: str) -> float:
        v = self.deadzone_deg.get(group)
        if v is None:
            v = C.CALIB_FALLBACK["deadzone_deg"].get(group, 1.0)
        return float(v)


# --------------------------------------------------------------------------- #
# 加载
# --------------------------------------------------------------------------- #
def _candidates(backend: str | None) -> list:
    base = os.path.join(ROOT, C.CALIB_DIRNAME)
    out = []
    if backend:
        out.append(os.path.join(base, backend, C.CALIB_ACTIVE))
    for b in ("real", "sim", "digital"):
        out.append(os.path.join(base, b, C.CALIB_ACTIVE))
    out.append(os.path.join(base, C.CALIB_ACTIVE))
    return out


def _coerce(data: dict, source: str) -> CalibConfig:
    rt = data.get("runtime") or {}
    warnings = [f"标定缺项，已用保守默认: {k}"
                for k in (data.get("fallback_used") or [])]

    def num(key, default):
        v = rt.get(key)
        if v is None:
            return default
        try:
            return float(v)
        except (TypeError, ValueError):
            warnings.append(f"{key} 非法，用默认 {default}")
            return default

    limits = rt.get("limits_deg") or {}
    clean_limits = {}
    for k, v in limits.items():
        try:
            lo, hi = float(v["neg"]), float(v["pos"])
        except (KeyError, TypeError, ValueError):
            warnings.append(f"limits[{k}] 结构非法，已丢弃")
            continue
        # usable:false 的轴按组给保守边界（接近冻结）
        if v.get("usable", True) is False:
            if k.startswith("lid_up_"):
                clean_limits[k] = {"neg": 0.0, "pos": 5.0}  # 上眼睑：几乎冻结
            elif k.startswith("lid_low_"):
                clean_limits[k] = {"neg": 0.0, "pos": 3.0}  # 下眼睑：几乎冻结
            elif k.startswith("neck_"):
                clean_limits[k] = {"neg": -5.0, "pos": 5.0}  # 颈：几乎冻结
            else:
                clean_limits[k] = {"neg": -5.0, "pos": 5.0}  # 眼：几乎冻结
            warnings.append(f"limits[{k}] 判废，冻结在 ±5° 内")
            continue
        if not (lo < hi) or (hi - lo) < 1.0:
            warnings.append(f"limits[{k}] 区间非法 {lo}..{hi}，已丢弃")
            continue
        clean_limits[k] = {"neg": lo, "pos": hi}

    fov = num("fov_h_deg", C.FOV_H_DEG)
    fx = num("fx", 0.0)
    if not (40.0 <= fov <= 120.0) or (fx and not (80.0 <= fx <= 4000.0)):
        warnings.append(f"相机参数不合理 fov={fov} fx={fx}，回退假设值")
        fov, fx = C.FOV_H_DEG, 0.0

    backend_name = str(data.get("backend") or "unknown")
    if backend_name not in ("real", "unknown"):
        warnings.append(f"⚠️ 标定后端={backend_name}（非真机 real），当前为模拟/数字标定值")

    micro = rt.get("micro_amp_range_deg") or list(C.CALIB_FALLBACK["micro_amp_range_deg"])
    try:
        micro = (float(micro[0]), float(micro[1]))
        if micro[0] > 50 or micro[1] > 50:
            warnings.append("micro_amp_range_deg 越界（哨兵999），已关闭微扫视")
            micro = (0.0, 0.0)
    except (TypeError, ValueError, IndexError):
        micro = (0.0, 0.0)
        warnings.append("micro_amp_range_deg 非法，已关闭微扫视")

    # C7 derived 区间校验：999哨兵会锁死关节
    q = rt.get("cmd_quantum_deg") or {}
    clean_q = {}
    for k, v in q.items():
        try:
            fv = float(v)
            if 0.01 <= fv <= 10.0:
                clean_q[k] = fv
            else:
                warnings.append(f"cmd_quantum[{k}]={fv} 越界，已丢弃")
        except (TypeError, ValueError):
            pass

    dz = rt.get("deadzone_deg") or {}
    clean_dz = {}
    for k, v in dz.items():
        try:
            fv = float(v)
            if 0.01 <= fv <= 20.0:
                clean_dz[k] = fv
            else:
                warnings.append(f"deadzone[{k}]={fv} 越界，已用默认")
        except (TypeError, ValueError):
            pass

    vmax = num("vergence_max_distance_m", 0.0)
    if vmax < 0 or vmax > 10.0:
        warnings.append(f"vergence_max_distance_m={vmax} 异常，已置0（关闭会聚）")
        vmax = 0.0

    return CalibConfig(
        backend=str(data.get("backend") or "unknown"),
        loaded=True, source=source,
        fx=fx, fy=num("fy", fx) or fx,
        cx=num("cx", C.CAM_W / 2), cy=num("cy", C.CAM_H / 2), fov_h_deg=fov,
        yaw0_deg=num("yaw0_deg", 0.0),
        pitch0_deg=num("pitch0_deg", 0.0),
        dy_m=num("dy_m", 0.30),
        dist_k=num("dist_k", 48.0),
        dist_a=num("dist_a", 0.0),
        dist_b=num("dist_b", 0.0),
        limits=clean_limits,
        channels=dict(data.get("channels", {}) or {}),
        deadzone_deg=clean_dz or dict(C.CALIB_FALLBACK["deadzone_deg"]),
        micro_amp_range_deg=micro,
        vergence_max_distance_m=vmax,
        cmd_quantum_deg=clean_q,
        watchdog_max_age_ms=int(num("watchdog_max_age_ms", 100)),
        neck_delay_ms=num("neck_delay_ms", 200.0),
        look_deg_per_weight=num("look_deg_per_weight", C.LOOK_DEG_PER_WEIGHT),
        warnings=tuple(warnings),
    )


def load(path: str | None = None, backend: str | None = None) -> CalibConfig:
    """读 active.json；任何失败都返回带默认值的 CalibConfig（不抛）。

    后端选择优先级：显式参数 > 环境变量 GAZE_CALIB_BACKEND > real→sim→digital。
    """
    backend = backend or os.environ.get("GAZE_CALIB_BACKEND") or None
    cands = [path] if path else _candidates(backend)
    warnings = []
    if backend and not path:
        explicit = _candidates(backend)[0] if _candidates(backend) else None
        if explicit and not os.path.isfile(explicit):
            warnings.append(f"⚠️ 显式指定 backend={backend} 但 {explicit} 不存在，回退到下一候选")
    for p in cands:
        if not p or not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                continue
            if data.get("schema") not in (1, None):
                continue
            return _coerce(data, p)
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(f"{os.path.basename(os.path.dirname(p))} 读取失败: {type(exc).__name__}: {exc}")
            continue
    if warnings:
        return CalibConfig(warnings=tuple(warnings) + ("使用默认参数",))
    return CalibConfig(warnings=("未找到 calibration/*/active.json，使用默认参数",))


# --------------------------------------------------------------------------- #
# 注入
# --------------------------------------------------------------------------- #
def apply(cfg: CalibConfig) -> list:
    """把配置注入各模块；返回仍需人工关注的告警列表。

    注：这里只做**一次**注入（启动时），运行期不允许再改。
    """
    from .arbitration import SoftLimiter
    # 未标定轴用 LIMIT_FALLBACK 补齐，不走 SOFT_LIMITS 宽松默认
    merged = {}
    for joint, fallback in C.LIMIT_FALLBACK.items():
        merged[joint] = dict(fallback)
    merged.update(cfg.limits)  # 已标定的覆盖 fallback
    SoftLimiter.set_limits(merged)
    issues = list(cfg.warnings)

    # 限位不对称过大 → 提示先修机械
    for j, lim in cfg.limits.items():
        span = lim["pos"] - lim["neg"]
        asym = abs(lim["pos"] + lim["neg"])
        if span < 2 * C.LIMIT_MIN_USABLE_DEG:
            issues.append(f"{j} 可用行程仅 {span:.1f}°，已判废")
        elif asym > 3.0 and not j.startswith("lid"):
            issues.append(f"{j} 左右不对称 {asym:.1f}°，建议检查装配")
    return issues


__all__ = ["CalibConfig", "load", "apply"]
