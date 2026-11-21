"""
P2 决策 · M2.2 几何解算
"""
import math
from . import config as C
from .state_machine import FSMConfig

def lerp(a, b, t): return a + (b - a) * t
from . import rng
from .oculomotor import SaccadeGenerator, FixationNoise
import os
import math
from .presence import PresenceController, SPEC_RANGES
from .audio_input import create_audio_input
from .driver import Driver

PD_MM = C.PD_MM
EYE_MAX_DEG = C.EYE_MAX_DEG
DEADZONE_OFFSET_DEG = C.DEADZONE_OFFSET_DEG
AWAY_BLEND_MS = 450.0   # GAZE_AWAY 进入/退出斜坡时长


def pixels_to_angle(u, v, cx, cy, fx, fy=None):
    """像素→相机系方向角（针孔模型，胸前置摄像头固定不随头动）。
    fy 缺省等于 fx（方形像素假设）；C1 标出 fy 时用实测值。"""
    fy = fx if not fy else fy
    pan = math.degrees(math.atan2(u - cx, fx))
    tilt = math.degrees(math.atan2(v - cy, fy))
    return pan, tilt


def vergence_offset_deg(d_m, stability=1.0):
    """会聚角 offset = atan(PD/2d) × stability系数"""
    if d_m <= 0.1:
        d_m = 0.1
    amp = lerp(0.4, 1.0, stability)  # stability低→会聚弱
    return math.degrees(math.atan((PD_MM / 1000.0) / (2.0 * d_m))) * amp


class EyeNeckSplitter:
    ENTER_PAN_DEG = C.ENTER_PAN_DEG
    EXIT_PAN_DEG = C.EXIT_PAN_DEG
    ENTER_TILT_DEG = C.ENTER_TILT_DEG
    EXIT_TILT_DEG = C.EXIT_TILT_DEG
    EYE_REST_PAN_DEG = C.EYE_REST_PAN_DEG
    EYE_REST_TILT_DEG = C.EYE_REST_TILT_DEG

    def __init__(self):
        self._pan_shared = False
        self._tilt_shared = False

    def _axis(self, angle, neck_max, shared, enter_deg, exit_deg, eye_rest):
        a = abs(angle)
        if shared:
            if a < exit_deg:
                shared = False
        else:
            if a > enter_deg:
                shared = True
        if not shared:
            eye = max(-EYE_MAX_DEG, min(EYE_MAX_DEG, angle))
            neck = 0.0
        else:
            # shared 模式：眼睛先吃eye_rest度，脖子补剩余
            s = 1.0 if angle >= 0 else -1.0
            eye = s * min(a, eye_rest)
            neck = max(-neck_max, min(neck_max, angle - eye))
        return neck, eye, shared


class TriangularSaccade:
    TARGETS = ["eye_midpoint", "nose", "mouth"]
    WEIGHTS = C.SACCADE_WEIGHTS

    def __init__(self, mean_interval_ms=1800):
        self.current = "eye_midpoint"
        self.last_switch = 0
        self.interval = mean_interval_ms

    def pick(self, ts_ms):
        if ts_ms - self.last_switch > self.interval:
            self.last_switch = ts_ms
            r = rng.rand()
            cum = 0
            chosen = self.TARGETS[0]
            for t, w in zip(self.TARGETS, self.WEIGHTS):
                cum += w
                if r < cum:
                    chosen = t
                    break
            self.current = chosen
            if chosen == "eye_midpoint" and rng.rand() < C.SACCADE_LONGTAIL_P:
                self.interval = rng.rand_range(C.SACCADE_LONGTAIL_MIN_MS, C.SACCADE_LONGTAIL_MAX_MS)
            else:
                self.interval = rng.rand_range(C.SACCADE_INTERVAL_MIN_MS, C.SACCADE_INTERVAL_MAX_MS)
        return self.current


class _OU:
    def __init__(self, mean, amp, tau_ms):
        self.mean = mean
        self.amp = amp
        self.tau = tau_ms
        self.x = mean

    def update(self, dt_ms):
        k = dt_ms / self.tau
        sqrt_dt = math.sqrt(max(1.0, dt_ms) / 1000.0)
        self.x += (self.mean - self.x) * k + self.amp * (rng.rand() - 0.5) * 0.2 * sqrt_dt
        return self.x


class GeometrySolver:
    def __init__(self, cam_w=640, cam_h=480, fov_h_deg=90.0, cfg=None, person_debounce=5):
        """cfg: p2_decision.calibration.CalibConfig（可选）。
        有标定时用实测 fx/cx/cy + 手眼偏置；没有就用传入的假设值。"""
        self.cfg = cfg
        if cfg is not None and getattr(cfg, "has_camera", False):
            self.cx = float(cfg.cx)
            self.cy = float(cfg.cy)
            self.fx = float(cfg.fx)
        else:
            self.cx, self.cy = cam_w / 2, cam_h / 2
            self.fx = (cam_w / 2) / math.tan(math.radians(fov_h_deg / 2))
        self.cam_w, self.cam_h = cam_w, cam_h
        self.prev = {"pan": 0.0, "tilt": 0.0, "offset": 0.0}
        self.tri = TriangularSaccade()
        self._last_g = None
        _seed_env = os.environ.get("GAZE_SEED", "")
        try:
            seed = int(_seed_env) if _seed_env else None
        except ValueError:
            seed = None
        self.presence = PresenceController(seed=seed)
        if seed is not None:
            rng._rng.seed(seed)
        self.driver = Driver()
        self.audio = create_audio_input(enable=os.environ.get("GAZE_AUDIO", "0") == "1")
        self._idle_started = False
        self._idle_pause_until = 0
        self._idle_moving_until = 0
        self._idle_pause_pan = 0.0
        self._idle_pause_tilt = 0.0
        self._wake_since = None
        self.split = EyeNeckSplitter()
        # 灵动性状态
        self._gaze_hold_since = 0      # 对视开始时间
        self._e_away_from = 0.0
        self._away_exit_t0 = 0
        self._away_exit_pan = 0.0
        self._away_exit_eye = 0.0
        self._away_dir = None
        self._person_prev = False
        self._hit = 0
        self._miss = 0
        self._leave_t0 = 0
        self._leave_g = None
        self._enter_t0 = 0
        self._enter_g = None
        # 眼颈时序延迟：眼先跳目标，脖子延迟120ms再开始跟
        self._neck_hold_until = 0
        self._prev_fc_pan = 0.0
        self._prev_fc_tilt = 0.0
        self._depart_sac = None
        self._depart_sac_fired = False
        self._track_prev = False
        self._person_debounce = max(1, int(person_debounce))
        self._overshoot_until = 0
        self._overshoot_amp_p = 0.0
        self._overshoot_amp_t = 0.0
        self._overshoot_from_p = 0.0
        self._overshoot_from_t = 0.0
        self._last_point = None
        self._tilt_until = 0           # 歪头结束时间
        self._tilt_val = 0.0
        self._next_tilt = 10000
        # 目光撤回序列
        self._retire_t0 = 0            # 撤回开始时间，0=未在撤回
        self._retire_dir = 1           # 眼扫偏方向（左/右随机）
        self._retire_blink_fired = False
        self._retire_neck_from = 0.0   # 撤回开始时脖子 pan 位置
        self._speaking = False         # 当前是否在说话
        self._identity = "unknown"     # 当前人脸身份
        self._last_ts = 0
        self._sp_gain = 1.0
        # OU 随机游走
        self._breath = _OU(0, C.BREATH_AMP, C.BREATH_TAU_MS)
        self._nod = _OU(0, C.NOD_AMP, C.NOD_TAU_MS)
        # 由标定决定的感知门限：死区一半作为扫视阈值，微扫视幅度取 C7 的区间
        dz = float(cfg.deadzone_for("eye")) if cfg is not None else 0.5
        sac_thr = max(0.15, dz / 2.0)
        if cfg is None:
            micro = (0.08, 1.0)  # 完全无配置时默认开启
        else:
            micro = getattr(cfg, "micro_amp_range_deg", (0.0, 0.0))
        self._sac_pan = SaccadeGenerator(sac_thr)
        self._sac_tilt = SaccadeGenerator(sac_thr)
        self._fix = FixationNoise(min_amp=micro[0], max_amp=micro[1])

    def _split(self, pan, tilt):
        """实例级眼颈分配（携带滞回状态）。"""
        n_pan, e_pan, sp = self.split._axis(pan, C.NECK_MAX_YAW,
            self.split._pan_shared,
            self.split.ENTER_PAN_DEG, self.split.EXIT_PAN_DEG,
            self.split.EYE_REST_PAN_DEG)
        n_tilt, e_tilt, st = self.split._axis(tilt, C.NECK_MAX_PITCH,
            self.split._tilt_shared,
            self.split.ENTER_TILT_DEG, self.split.EXIT_TILT_DEG,
            self.split.EYE_REST_TILT_DEG)
        self.split._pan_shared = sp
        self.split._tilt_shared = st
        return n_pan, n_tilt, e_pan, e_tilt

    def solve(self, intent, tracks_anchors, ts_ms, extra_pan_deg=0.0):
        g = self._solve_inner(intent, tracks_anchors, ts_ms, extra_pan_deg)
        g["state"] = self.presence.current
        # 人回来：200ms 从 idle 姿态过渡到目光姿态
        if getattr(self, "_enter_t0", 0) and getattr(self, "_enter_g", None):
            kk = min(1.0, (ts_ms - self._enter_t0) / 200.0)
            kk = kk*kk*(3-2*kk)
            for key in ("body_pan_deg","body_tilt_deg","eye_pan_deg","eye_tilt_deg","neck_roll_deg"):
                a0 = float(self._enter_g.get(key, 0.0))
                g[key] = round(a0 + (g[key] - a0)*kk, 2)
            if kk >= 1.0:
                self._enter_t0 = 0
                self._enter_g = None
        return self.driver.apply(g, ts_ms)

    def _solve_inner(self, intent, tracks_anchors, ts_ms, extra_pan_deg=0.0):
        """
        intent: 状态机输出 {state, active_track_id, scan, lost_stage}
        tracks_anchors: {track_id: {eye_L:(u,v), eye_R, nose, mouth}}
        → IF-3 GazeTarget
        """
        self._breath_amp = intent.get("breath_amp", 1.0)
        # 迟滞去抖：获得需 5 帧，丢失需 3 帧（≈100ms）
        up = self._person_debounce
        down = 3
        if tracks_anchors:
            self._hit = min(self._hit + 1, up)
            self._miss = 0
        else:
            self._hit = 0
            self._miss += 1
        person = (self._hit >= up) or (self._person_prev and self._miss < down)
        has_anc = bool(tracks_anchors)
        if getattr(self, "_track_prev", False) and not has_anc:
            self._leave_t0 = ts_ms
            self._leave_g = dict(self._last_g) if self._last_g else None
        self._track_prev = has_anc
        # person False→True：记录 idle 末态，200ms 过渡到目光姿态
        if person and not self._person_prev:
            self._enter_t0 = ts_ms
            self._enter_g = dict(self._last_g) if self._last_g else None
            self._depart_sac = None
            self._depart_sac_fired = False
            self._leave_t0 = 0
            self._leave_g = None
            self._gaze_hold_since = 0  # 重新计时对视
            self._avoid_after_ms = 0
        self._person_prev = person
        # 有人脸时重置 idle 序列
        if person:
            if self._idle_started:
                self._wake_since = ts_ms
                self._wake_lid0 = (self._last_g or {}).get("lid_closure_deg", 0.0)
                self.presence._idle_t = 0.0
            self._idle_started = False
            self._idle_timers_init = False  # 人离开后重新进idle时重置timer
            # 有人来：打断 presence，切 SOCIAL_GAZE（保留当前姿态，不硬切 IDLE）
            if self.presence.current not in ("SOCIAL_GAZE", "SOCIAL_AVOID", "LISTENING"):
                self.presence.request("SOCIAL_GAZE", force=True)
            # 检测到说话：切 LISTENING
            if self.audio.is_speaking() and self.presence.current == "SOCIAL_GAZE":
                self.presence.request("LISTENING", force=True)
            # 移开由 _social_step 负责（微上移4°），不再切 SOCIAL_AVOID 族
            # AVOID 停留 1.5s 后切回 GAZE
            if self.presence.current == "SOCIAL_AVOID":
                if ts_ms - getattr(self, "_avoid_since", ts_ms) > 1500:
                    self.presence.request("SOCIAL_GAZE", force=True)

        # 计算 dt，所有分支统一更新
        dt = max(1, ts_ms - (self._last_ts or ts_ms))
        self._last_ts = ts_ms

        # presence 时钟每帧都推进
        self.presence.update(dt, person)

        # --- 预设锚点：meditate 等强制看向固定方向，按 neck_ratio 分配（不跟人） ---
        if intent.get("anchor_deg") is not None:
            ap, at = intent["anchor_deg"]
            ratio = intent.get("neck_ratio", 0.75)
            n_pan, n_tilt = ap * ratio, at * ratio
            e_pan, e_tilt = ap * (1 - ratio), at * (1 - ratio)
            # 预设路径也加呼吸微动（复用上面算好的 ms dt）
            breath_amp = intent.get("breath_amp", 1.0)
            n_tilt += self._breath.update(dt) * breath_amp
            off = vergence_offset_deg(1.5)
            g = self._pack(n_pan, n_tilt, e_pan, e_tilt, 0.0, off, 1.5, "preset", ts_ms)
            self.prev = {"pan": n_pan, "tilt": n_tilt, "offset": off}
            self._last_g = g
            return g

        # --- SCANNING ---
        if intent["state"] == "SCANNING":
            self._away_dir = None
            # 修复1：扫描期间检测到脸时，走统一 _split 分配颈/眼，不再自己写 pan*0.3，
            # 避免颈吃满角度 + 过冲 30%。
            if tracks_anchors:
                first_anc = next(iter(tracks_anchors.values()))
                u = (first_anc["eye_L"][0] + first_anc["eye_R"][0]) / 2
                v = (first_anc["eye_L"][1] + first_anc["eye_R"][1]) / 2
                fy = float(getattr(self.cfg, "fy", 0.0) or 0.0) if self.cfg else 0.0
                pan, tilt = pixels_to_angle(u, v, self.cx, self.cy, self.fx, fy)
                pan = -pan
                d_cam = self._estimate_distance(first_anc)
                if self.cfg is not None:
                    pan -= float(getattr(self.cfg, "yaw0_deg", 0.0))
                    tilt += float(getattr(self.cfg, "pitch0_deg", 0.0)) + math.degrees(
                        math.atan(float(getattr(self.cfg, "dy_m", 0.3)) / max(0.2, d_cam)))
                # DROWSY 等预设的下视偏置
                tilt += intent.get("scan_tilt_bias", 0.0)
                # 低通滤波：防止脸检测闪烁/track切换导致pan跳变
                if not hasattr(self, "_scan_pan"):
                    self._scan_pan = pan
                    self._scan_tilt = tilt
                self._scan_pan += (pan - self._scan_pan) * 0.15
                self._scan_tilt += (tilt - self._scan_tilt) * 0.15
                n_pan, n_tilt, e_pan, e_tilt = self._split(self._scan_pan, self._scan_tilt)
                e_pan = max(-12.0, min(12.0, e_pan))
                e_tilt = max(-12.0, min(12.0, e_tilt))
                n_pan = max(-35.0, min(35.0, n_pan))
                n_tilt = max(-8.0, min(8.0, n_tilt))
                g = {
                    "body_pan_deg": round(n_pan, 2), "body_tilt_deg": round(n_tilt, 2),
                    "eye_pan_deg": round(e_pan, 2), "eye_tilt_deg": round(e_tilt, 2),
                    "neck_roll_deg": 0,
                    "offset_deg": round(self._vergence_offset(d_cam), 2),
                    "distance_m": round(d_cam, 2), "source": "scan", "ts_ms": ts_ms,
                    # 诊断：记录送入几何的关键点中心，供与 YuNet bbox 中心对比
                    "_tgt_u": round(u, 1), "_tgt_v": round(v, 1),
                    "_eye_dist_px": round(abs(first_anc["eye_R"][0] - first_anc["eye_L"][0]), 1),
                    "_raw_pan": round(pan, 2), "_raw_tilt": round(tilt, 2),
                }
                # 修复A：保留 _last_g，不置 None，避免进 LOOKING 时 track 缺失闪正中
                self._last_g = g
                return g
            else:
                # 无人脸：用 PresenceController 状态族
                return self._emit_idle(ts_ms, extra_pan_deg)

        # --- GAZE_AWAY：视线移开到侧上方，方向锁存；track 丢了也继续 away（Bug A） ---
        if intent["state"] == "GAZE_AWAY":
            if not hasattr(self, "_away_dir") or self._away_dir is None:
                # 进入时算好方向，整个away周期复用（不每帧随机）
                self._away_pan = rng.rand_range(-18, 18)
                self._away_neck_tilt = rng.rand_range(-4, 2)
                self._away_eye_tilt = rng.rand_range(-6, 3)
                self._away_t0 = ts_ms
                self._away_from = (self._last_g or {}).get("body_pan_deg", 0.0)
                self._e_away_from = (self._last_g or {}).get("eye_pan_deg", 0.0)
                self._away_from_tilt = (self._last_g or {}).get("body_tilt_deg", 0.0)
                self._e_away_from_tilt = (self._last_g or {}).get("eye_tilt_deg", 0.0)
                self._away_from_roll = (self._last_g or {}).get("neck_roll_deg", 0.0)
                self._away_dir = self._away_pan  # 标记已初始化
            k = min(1.0, (ts_ms - self._away_t0) / AWAY_BLEND_MS)
            k = k*k*(3-2*k)
            n_away = self._away_from + (self._away_pan * 0.3 - self._away_from) * k
            e_away = self._e_away_from + (self._away_pan * 0.7 - self._e_away_from) * k
            n_tilt = self._away_from_tilt + (self._away_neck_tilt - self._away_from_tilt) * k
            e_tilt = self._e_away_from_tilt + (self._away_eye_tilt - self._e_away_from_tilt) * k
            roll = self._away_from_roll + (0.0 - self._away_from_roll) * k
            g = self._pack(n_away, n_tilt, e_away, e_tilt, roll,
                           vergence_offset_deg(1.5), 1.5, "away", ts_ms)
            self._last_g = g
            return g
        if self._away_dir is not None:
            self._away_exit_t0 = ts_ms
            self._away_exit_pan = (self._last_g or {}).get("body_pan_deg", 0.0)
            self._away_exit_eye = (self._last_g or {}).get("eye_pan_deg", 0.0)
            self._away_exit_neck_tilt = (self._last_g or {}).get("body_tilt_deg", 0.0)
            self._away_exit_eye_tilt = (self._last_g or {}).get("eye_tilt_deg", 0.0)
            self._away_exit_roll = (self._last_g or {}).get("neck_roll_deg", 0.0)
        self._away_dir = None

        # 无人脸：500ms内曾有人则保持上一帧，否则走 PresenceController 兜底
        if not person:
            if self._last_g is not None:
                last_ts = self._last_g.get("ts_ms", 0)
                if ts_ms - last_ts < 500 and self._last_g.get("source") not in ("idle",):
                    g = dict(self._last_g)
                    g["ts_ms"] = ts_ms
                    return g
            return self._emit_idle(ts_ms, extra_pan_deg)

        tid = intent.get("active_track_id")
        if tid not in tracks_anchors:
            # Bug fix: 换人过渡期（tracker 换 id）沿用上一帧，不回中闪一下。
            # 等 FSM 确认新 track 后自动切过去。
            if self._last_g is not None:
                g = dict(self._last_g)
                g["ts_ms"] = ts_ms
                return g
            return self._emit(0.0, 0.0, 1.5, "frame_center", ts_ms)

        anc = tracks_anchors[tid]
        # 说话状态（P1 嘴部开合估计）
        self._speaking = anc.get("speaking_prob", 0.0) > 0.3
        self._identity = anc.get("identity", "unknown")
        point = self.tri.pick(ts_ms)
        # 锁存注视点：不要每帧在眼/鼻/嘴之间跳，保持0.8-1.5秒再换
        if not hasattr(self, "_locked_point"):
            self._locked_point = point
            self._locked_point_until = ts_ms + rng.rand_range(800, 1500)
        elif ts_ms >= self._locked_point_until:
            self._locked_point = point
            self._locked_point_until = ts_ms + rng.rand_range(800, 1500)
        point = self._locked_point
        # 扫视过冲：换点时记起点，幅度按本次步长
        if point != self._last_point:
            self._overshoot_from_p = (self._last_g or {}).get("eye_pan_deg", 0.0)
            self._overshoot_from_t = (self._last_g or {}).get("eye_tilt_deg", 0.0)
            self._overshoot_amp_p = None
            self._overshoot_amp_t = None
            self._overshoot_until = ts_ms + C.OVERSHOOT_MS
            self._overshoot_mag = rng.rand_range(C.OVERSHOOT_MIN, C.OVERSHOOT_MAX)
            self._last_point = point
        target_px = {
            "eye_midpoint": ((anc["eye_L"][0] + anc["eye_R"][0]) / 2,
                             (anc["eye_L"][1] + anc["eye_R"][1]) / 2),
            "nose": anc["nose"],
            "mouth": anc["mouth"],
        }[point]
        # 人脸中心（鼻子）：脖子跟这个走，不因扫视目标而动
        face_center_px = anc["nose"]

        fy = float(getattr(self.cfg, "fy", 0.0) or 0.0) if self.cfg else 0.0
        pan, tilt = pixels_to_angle(target_px[0], target_px[1], self.cx, self.cy,
                                    self.fx, fy)
        fc_pan, fc_tilt = pixels_to_angle(face_center_px[0], face_center_px[1],
                                          self.cx, self.cy, self.fx, fy)
        pan = -pan   # 镜像：用户左移 → 数字人朝左看（与翻转预览窗口一致）
        fc_pan = -fc_pan
        d_cam = self._estimate_distance(anc)
        if self.cfg is not None:
            pan -= float(getattr(self.cfg, "yaw0_deg", 0.0))
            fc_pan -= float(getattr(self.cfg, "yaw0_deg", 0.0))
            tilt += float(getattr(self.cfg, "pitch0_deg", 0.0)) + math.degrees(
                math.atan(float(getattr(self.cfg, "dy_m", 0.3)) / max(0.2, d_cam)))
            fc_tilt += float(getattr(self.cfg, "pitch0_deg", 0.0)) + math.degrees(
                math.atan(float(getattr(self.cfg, "dy_m", 0.3)) / max(0.2, d_cam)))
        raw_pan, raw_tilt = pan, tilt
        eye_dist_px = abs(anc["eye_R"][0] - anc["eye_L"][0])
        d = d_cam
        # C7 结论：会聚在远处没有意义（分辨率不够），超过上限就不做
        offset = self._vergence_offset(d)

        # smooth pursuit：pan 慢动(<15°/s)时低通，快动(>25°/s)硬跟，带滞回
        dpan = (pan - self.prev["pan"]) / (dt / 1000.0)
        if abs(dpan) > C.SP_FAST_DEG_PER_S:
            self._sp_gain = 1.0
        elif abs(dpan) < C.SP_SLOW_DEG_PER_S:
            self._sp_gain = C.SP_SLOW_GAIN
        pan_sp = self.prev["pan"] + (pan - self.prev["pan"]) * self._sp_gain

        pan = pan_sp
        if abs(offset) < DEADZONE_OFFSET_DEG:
            offset = self.prev["offset"]
        self.prev = {"pan": pan, "tilt": tilt, "offset": offset}

        # 脖子只跟人脸中心（fc_pan/fc_tilt），不跟扫视目标
        # 眼睛快速追踪，脖子慢速延迟跟随（模拟真人眼先动、头晚到）
        if not hasattr(self, "_eye_track_pan"):
            self._eye_track_pan = fc_pan
            self._eye_track_tilt = fc_tilt
            self._neck_track_pan = fc_pan
            self._neck_track_tilt = fc_tilt
            self._last_track_id = tid
        # track切换：眼睛硬切到新人，脖子延迟120ms再开始跟（眼领头随）
        if tid != getattr(self, "_last_track_id", None):
            self._eye_track_pan = fc_pan
            self._eye_track_tilt = fc_tilt
            # 脖子不从旧位置硬切，保持当前位置，等hold结束后慢慢跟
            self._neck_hold_until = ts_ms + 120
            self._last_track_id = tid
        # 从idle切回looking：脖子从当前位置200ms快速过渡到人脸，不硬切也不慢爬
        if getattr(self, "_idle_started", False):
            # 用idle最后一帧的输出位置作为过渡起点
            last = self._last_g or {}
            self._neck_track_pan = float(last.get("body_pan_deg", 0.0))
            self._neck_track_tilt = float(last.get("body_tilt_deg", 0.0))
            self._eye_track_pan = fc_pan  # 眼睛还是先快速到位
            self._eye_track_tilt = fc_tilt
            self._return_ease_until = ts_ms + 200  # 200ms快速过渡窗口
            self._idle_started = False
            self._idle_timers_init = False
        # 眼睛快速跟（α=0.3，~100ms到位，不要瞬间打到clamp）
        self._eye_track_pan += (fc_pan - self._eye_track_pan) * 0.3
        self._eye_track_tilt += (fc_tilt - self._eye_track_tilt) * 0.3
        # 同track内目标跳变>8°时，脖子也延迟120ms（模拟眼先跳、头后转）
        if abs(fc_pan - self._prev_fc_pan) > 8.0 or abs(fc_tilt - self._prev_fc_tilt) > 5.0:
            self._neck_hold_until = ts_ms + 120
        self._prev_fc_pan = fc_pan
        self._prev_fc_tilt = fc_tilt
        # 脖子慢跟：hold期间保持不动，hold结束后开始跟
        neck_holding = ts_ms < self._neck_hold_until
        if not neck_holding:
            if ts_ms < getattr(self, "_return_ease_until", 0):
                neck_gain_p = 0.5
                neck_gain_t = 0.4
            elif intent["state"] == "RETURN":
                neck_gain_p = 0.05
                neck_gain_t = 0.03
            else:
                neck_gain_p = 0.25
                neck_gain_t = 0.15
            self._neck_track_pan += (fc_pan - self._neck_track_pan) * neck_gain_p
            self._neck_track_tilt += (fc_tilt - self._neck_track_tilt) * neck_gain_t
        # 眼睛用快速追踪值，脖子用慢速延迟值
        e_pan_raw = self._eye_track_pan
        e_tilt_raw = self._eye_track_tilt
        fc_pan_s = self._neck_track_pan
        fc_tilt_s = self._neck_track_tilt
        n_pan, n_tilt, _, _ = self._split(fc_pan_s, fc_tilt_s)
        # 眼睛角度 = 快速追踪值 - 脖子当前位置（头局部坐标）
        e_pan = e_pan_raw - n_pan
        e_tilt = e_tilt_raw - n_tilt
        # 扫视偏移：脖子静止时眼睛才微动，脖子在追目标时眼睛保持居中
        prev_n_pan = getattr(self, "_prev_n_pan", n_pan)
        dt_s = max(dt, 1) / 1000.0  # ms→s，得到 deg/s
        n_vel_pan = abs(n_pan - prev_n_pan) / dt_s
        prev_n_tilt = getattr(self, "_prev_n_tilt", n_tilt)
        n_vel_tilt = abs(n_tilt - prev_n_tilt) / dt_s
        self._prev_n_pan = n_pan
        self._prev_n_tilt = n_tilt
        # 扫视偏移：脖子静止时全量，脖子转动时不加（避免眼颈反向）
        if n_vel_pan < 5:
            e_pan += (pan - fc_pan)
        if n_vel_tilt < 5:
            e_tilt += (tilt - fc_tilt)
        self.prev["n_pan"] = n_pan
        self.prev["n_tilt"] = n_tilt
        e_pan = max(-12.0, min(12.0, e_pan))
        e_tilt = max(-12.0, min(12.0, e_tilt))
        n_tilt = self._physiology_step(dt, n_tilt, intent.get("nod_mult", 1.0))
        # 过冲幅度：用目标值（trigger前），不用sac输出
        if self._overshoot_amp_p is None:
            self._overshoot_amp_p = e_pan - self._overshoot_from_p
        if self._overshoot_amp_t is None:
            self._overshoot_amp_t = e_tilt - self._overshoot_from_t
        # 扫视主序：眼睛按弹道走，不再低通
        self._sac_pan.trigger(e_pan, ts_ms)
        self._sac_tilt.trigger(e_tilt, ts_ms)
        e_pan = self._sac_pan.value(ts_ms)
        e_tilt = self._sac_tilt.value(ts_ms)
        e_pan = self._overshoot_step(ts_ms, e_pan, "p")
        e_tilt = self._overshoot_step(ts_ms, e_tilt, "t")
        # 注视噪声（漂移+微扫视，二维）
        fp, ft = self._fix.step(ts_ms, dt)
        e_pan += fp
        e_tilt += ft
        roll = self._roll_step(ts_ms)
        if self._away_exit_t0:
            kk = min(1.0, (ts_ms - self._away_exit_t0) / AWAY_BLEND_MS)
            kk = kk * kk * (3 - 2 * kk)
            w = 1.0 - kk
            n_pan += (self._away_exit_pan - n_pan) * w
            e_pan += (self._away_exit_eye - e_pan) * w
            n_tilt += (self._away_exit_neck_tilt - n_tilt) * w
            e_tilt += (self._away_exit_eye_tilt - e_tilt) * w
            roll += (self._away_exit_roll - roll) * w
            if kk >= 1.0:
                self._away_exit_t0 = 0
        n_pan_off, e_tilt, e_pan_off, force_blink = self._social_step(ts_ms, e_tilt, n_pan)
        n_pan += n_pan_off
        e_pan += e_pan_off
        e_tilt = max(-12.0, min(12.0, e_tilt))
        e_pan = max(-12.0, min(12.0, e_pan))
        n_pan = max(-35.0, min(35.0, n_pan))
        n_tilt = max(-8.0, min(8.0, n_tilt))
        g = {
            "body_pan_deg": round(n_pan, 2), "body_tilt_deg": round(n_tilt, 2),
            "eye_pan_deg": round(e_pan, 2), "eye_tilt_deg": round(e_tilt, 2),
            "neck_roll_deg": round(roll, 2),
            "lid_closure_deg": 0,
            "offset_deg": round(offset, 2), "distance_m": d,
            "source": point, "ts_ms": ts_ms,
            "_force_blink": force_blink,
            # 镜像眨眼已停用（见 test_harness.run_p2_if1a 的说明）；
            # 恢复时取消下面一行注释，并同步恢复 harness 里的检测与触发
            # "_force_blink": ts_ms < getattr(self, "_mirror_blink_at", 0) + 100,
            "_raw_pan": round(raw_pan, 2), "_raw_tilt": round(raw_tilt, 2),
            "_tgt_u": round(target_px[0], 1), "_tgt_v": round(target_px[1], 1),
            "_eye_dist_px": round(abs(anc["eye_R"][0] - anc["eye_L"][0]), 1),
            "_pan_sp": round(pan_sp, 2),
            "_split_engaged": int(self.split._pan_shared or self.split._tilt_shared),
            "_sac_inflight": int(self._sac_pan.inflight or self._sac_tilt.inflight),
        }
        # 中断恢复：刚从 idle 醒来，从当前 presence lid 渐变到 0
        if self._wake_since is not None:
            wake_elapsed = ts_ms - self._wake_since
            if wake_elapsed < 300:
                g["lid_closure_deg"] = getattr(self, "_wake_lid0", 0.0) * (1 - wake_elapsed / 300)
            else:
                self._wake_since = None

        self._last_g = g
        return g

    def _physiology_step(self, dt, n_tilt, nod_mult=1.0):
        breath_amp = getattr(self, "_breath_amp", 1.0)
        n_tilt += self._breath.update(dt) * breath_amp + self._nod.update(dt) * nod_mult
        return max(-30.0, min(30.0, n_tilt))

    def _overshoot_step(self, ts_ms, v, axis):
        """跳视过冲：按本次步长的 20~40% 冲出，80ms 线性收回"""
        if ts_ms >= self._overshoot_until:
            return v
        amp = self._overshoot_amp_p if axis == "p" else self._overshoot_amp_t
        t = (ts_ms - (self._overshoot_until - C.OVERSHOOT_MS)) / C.OVERSHOOT_MS
        v += amp * (self._overshoot_mag - 1.0) * (1.0 - t)
        return max(-24.0, min(24.0, v))

    def _roll_step(self, ts_ms):
        if ts_ms > self._next_tilt and ts_ms > self._tilt_until:
            self._tilt_val = (1 if rng.rand() > 0.5 else -1) * C.TILT_ROLL_DEG
            self._tilt_until = ts_ms + C.TILT_ROLL_DURATION_MS
            self._next_tilt = ts_ms + rng.rand_range(C.TILT_ROLL_INTERVAL_MIN_MS, C.TILT_ROLL_INTERVAL_MAX_MS)
        target = self._tilt_val if ts_ms < self._tilt_until else 0.0
        # 400ms 定时插值：目标变化时记起点，跨长空闲自动复位
        if not hasattr(self, "_roll_target") or target != self._roll_target:
            self._roll_from = getattr(self, "_roll_cur", 0.0)
            self._roll_t0 = ts_ms
            self._roll_target = target
        k = min(1.0, (ts_ms - self._roll_t0) / 400.0)
        k = k * k * (3 - 2 * k)
        self._roll_cur = self._roll_from + (target - self._roll_from) * k
        return self._roll_cur

    def _estimate_distance(self, anc):
        """瞳距 → 相机距离（米）。LOOKING / SCAN 共用。"""
        try:
            eye_px = abs(float(anc["eye_R"][0]) - float(anc["eye_L"][0]))
        except (KeyError, TypeError, ValueError):
            return 1.5
        dist_a = float(getattr(self.cfg, "dist_a", 0.0)) if self.cfg else 0.0
        dist_b = float(getattr(self.cfg, "dist_b", 0.0)) if self.cfg else 0.0
        if dist_a > 0:
            # C4 拟合模型 d = A/p + B
            d = dist_a / max(20.0, eye_px) + dist_b
        else:
            # 退回 K/eye_px
            dist_k = float(getattr(self.cfg, "dist_k", C.DIST_CALIB_K)) if self.cfg else C.DIST_CALIB_K
            d = dist_k / max(20.0, eye_px)
        return max(C.DIST_MIN_M, min(C.DIST_MAX_M, d))

    def _vergence_offset(self, d):
        """会聚角；vmax<=0 表示未标定→关闭会聚；距离超过 vmax 时也不做。"""
        vmax = float(getattr(self.cfg, "vergence_max_distance_m", 0.0)) if self.cfg else 0.0
        if vmax <= 0 or d > vmax:
            return 0.0
        return vergence_offset_deg(d)

    def _pick_gaze_limit(self):
        dim_intent = 0.5
        try:
            dim_intent = self.presence.fams[self.presence.current]["dims"].intent
        except (AttributeError, KeyError):
            pass
        lo = lerp(4000, 9000, dim_intent)
        hi = lerp(8000, 16000, dim_intent)
        # 熟人看更久，陌生人看更短，unknown 中性
        ident = getattr(self, "_identity", "unknown")
        if ident not in ("stranger", "unknown"):
            lo *= 1.3; hi *= 1.3
        elif ident == "stranger":
            lo *= 0.7; hi *= 0.7
        # 人正在说话：延长注视时间，不打断
        if getattr(self, "_speaking", False):
            lo *= 1.5
            hi *= 1.5
        return rng.rand_range(lo, hi)

    def _emit_idle(self, ts_ms, extra_pan_deg=0.0):
        """无人时统一出口：presence 兜底 + 人离开淡出 + 清 away 退出混合"""
        if not self._idle_started:
            p = self.presence
            p.t = 0
            p._idle_t = 0.0
            p._family_t = 0.0
            p._transition = None
            p.current = "IDLE"
            p.clear_preset()
            p.fams["IDLE"]["dims"].resample(SPEC_RANGES["IDLE"], p._rng)
            self._idle_started = True
            self._idle_start_ts = ts_ms
            # idle基准 = 离开方向的25%残差（钳±4°），不要完全回中
            leave = self._leave_g or {}
            leave_pan = float(leave.get("body_pan_deg", 0.0))
            leave_tilt = float(leave.get("body_tilt_deg", 0.0))
            self._idle_base_pan = max(-6.0, min(6.0, leave_pan * 0.25))
            self._idle_base_tilt = max(-4.0, min(4.0, leave_tilt * 0.25))
        n_pan, n_tilt, e_pan, e_tilt, lid, roll = self.presence.sample()
        # idle脖子 = 离开方向残差基准 + presence输出
        # （gen函数内部已做眼颈分配：neck_pan = pan * 0.3，此处不再二次缩放）
        n_pan = getattr(self, "_idle_base_pan", 0.0) + n_pan
        n_tilt = getattr(self, "_idle_base_tilt", 0.0) + n_tilt
        # 首次进入idle时初始化pause/moving计时器
        if not getattr(self, "_idle_timers_init", False):
            self._idle_pause_until = ts_ms + 2000
            self._idle_moving_until = 0
            self._idle_pause_pan = n_pan
            self._idle_pause_tilt = n_tilt
            self._idle_pause_roll = roll
            self._idle_blend_from_pan = n_pan
            self._idle_blend_from_tilt = n_tilt
            self._idle_blend_from_roll = roll
            self._idle_blend_until = 0
            self._idle_timers_init = True
        import math
        # 状态：moving_until>0=移动中，pause_until>0=停顿中
        # 第一步：移动结束 → 进入停顿
        if self._idle_moving_until > 0 and ts_ms >= self._idle_moving_until:
            med_p = 1500.0
            pause = math.exp(rng.gauss(math.log(med_p), 0.45))
            self._idle_pause_until = ts_ms + int(max(500, min(6000, pause)))
            self._idle_pause_pan = n_pan
            self._idle_pause_tilt = n_tilt
            self._idle_pause_roll = roll
            self._idle_moving_until = 0
        # 第二步：停顿结束 → 开始移动（记录blend起点，平滑过渡不跳变）
        if self._idle_pause_until > 0 and ts_ms >= self._idle_pause_until:
            med_mv = 800.0
            mv = math.exp(rng.gauss(math.log(med_mv), 0.4))
            self._idle_moving_until = ts_ms + int(max(200, min(3000, mv)))
            self._idle_pause_until = 0
            self._idle_blend_from_pan = self._idle_pause_pan
            self._idle_blend_from_tilt = self._idle_pause_tilt
            self._idle_blend_from_roll = self._idle_pause_roll
            self._idle_blend_until = ts_ms + 300  # 300ms平滑过渡
        # 第三步：停顿时保持位置（STRETCH进行中时跳过冻结，让歪头动作生效）
        _is_stretch = getattr(self.presence, '_interlude_name', None) == "STRETCH"
        if self._idle_pause_until > 0 and ts_ms < self._idle_pause_until and not _is_stretch:
            n_pan = self._idle_pause_pan
            n_tilt = self._idle_pause_tilt
            roll = self._idle_pause_roll
        # 第四步：停顿结束后300ms内，从冻结位置平滑过渡到当前presence位置
        if self._idle_blend_until > 0 and ts_ms < self._idle_blend_until:
            blend_k = 1.0 - (self._idle_blend_until - ts_ms) / 300.0
            blend_k = blend_k * blend_k * (3 - 2 * blend_k)  # smootherstep
            n_pan = self._idle_blend_from_pan + (n_pan - self._idle_blend_from_pan) * blend_k
            n_tilt = self._idle_blend_from_tilt + (n_tilt - self._idle_blend_from_tilt) * blend_k
            roll = self._idle_blend_from_roll + (roll - self._idle_blend_from_roll) * blend_k
        elif self._idle_blend_until > 0 and ts_ms >= self._idle_blend_until:
            self._idle_blend_until = 0
        n_pan += extra_pan_deg * 0.7
        e_pan += extra_pan_deg * 0.3
        n_pan = max(-10.0, min(10.0, n_pan))
        n_tilt = max(-6.0, min(6.0, n_tilt))
        e_pan = max(-15.0, min(15.0, e_pan))
        e_tilt = max(-12.0, min(12.0, e_tilt))
        g = self._pack(n_pan, n_tilt, e_pan, e_tilt, roll,
                       vergence_offset_deg(1.5), 1.5, "idle", ts_ms)
        g["lid_closure_deg"] = lid * 95.0
        g["state"] = self.presence.current
        fam = self.presence.fams.get(self.presence.current)
        if fam and fam.get("dims"):
            g["dims"] = fam["dims"].as_dict()
        _fade_active = False
        if self._leave_t0 and self._leave_g:
            age = ts_ms - self._leave_t0
            HOLD_MS = FSMConfig.HOLD_MS + FSMConfig.PREDICT_MS
            EYE_FADE_MS = FSMConfig.RECENTER_MS - HOLD_MS
            NECK_FADE_MS = EYE_FADE_MS + 400.0
            ARRIVE_MS = HOLD_MS + NECK_FADE_MS
            def _fade(age, fade_ms):
                if age < HOLD_MS:
                    return 0.0
                t = min(1.0, (age - HOLD_MS) / fade_ms)
                return t*t*(3-2*t)
            k_eye = _fade(age, EYE_FADE_MS)
            k_neck = _fade(age, NECK_FADE_MS)
            _fade_active = k_eye > 0 or k_neck > 0
            for key in ("body_pan_deg","body_tilt_deg","neck_roll_deg"):
                a = float(self._leave_g.get(key, 0.0))
                g[key] = round(a + (g[key]-a)*k_neck, 2)
            for key in ("eye_pan_deg","eye_tilt_deg","lid_closure_deg"):
                a = float(self._leave_g.get(key, 0.0))
                g[key] = round(a + (g[key]-a)*k_eye, 2)
            # 回中途中一次眼球小跳
            if not self._depart_sac_fired and age > HOLD_MS + C.DEPART_SAC_AT * EYE_FADE_MS:
                self._depart_sac_fired = True
                self._depart_sac = (
                    ts_ms,
                    rng.rand_range(*C.DEPART_SAC_MS),
                    rng.rand_range(*C.DEPART_SAC_DEG) * (1.0 if rng.rand() > 0.5 else -1.0),
                )
            if self._depart_sac:
                t0, dur, amp = self._depart_sac
                p = (ts_ms - t0) / dur
                if 0.0 <= p < 1.0:
                    g["eye_pan_deg"] = round(
                        g["eye_pan_deg"] + amp * math.sin(math.pi * p), 2)
                else:
                    self._depart_sac = None
            # 到达后颈部过冲（各通道独立方向）
            if age > ARRIVE_MS and C.DEPART_OVER_DEG > 0:
                u = (age - ARRIVE_MS) / C.DEPART_OVER_MS
                if u < 1.0:
                    for key, w in (("body_pan_deg", 1.0), ("body_tilt_deg", 0.5)):
                        tr = float(g[key]) - float(self._leave_g.get(key, 0.0))
                        a2 = min(C.DEPART_OVER_DEG,
                                 C.DEPART_OVER_RATIO * abs(tr)) * math.sin(math.pi * u)
                        g[key] = round(g[key] + (1.0 if tr >= 0 else -1.0) * a2 * w, 2)
            if age > ARRIVE_MS + C.DEPART_OVER_MS:
                self._leave_t0 = 0
                self._leave_g = None
                self._depart_sac = None
                self._depart_sac_fired = False
        # idle 接管后不再需要 away 退出混合
        self._away_exit_t0 = 0
        # source 标 distraction 仅在偏移实际生效时（hold 期间 kk=0 不标）
        if extra_pan_deg and (not self._leave_t0 or _fade_active):
            g["source"] = "distraction"
        self._last_g = g
        return g

    def _social_step(self, ts_ms, e_tilt, n_pan=0.0):
        """返回 (n_pan_off, e_tilt, e_pan_off, force_blink)。"""
        # 撤回序列进行中
        if self._retire_t0 > 0:
            rt = ts_ms - self._retire_t0
            force_blink = (rt >= C.RETIRE_BLINK_AT and not self._retire_blink_fired)
            if force_blink:
                self._retire_blink_fired = True
            # Phase 1: 眼 saccade 到偏方向 (0-80ms)
            if rt < C.RETIRE_EYE_SACCADE_MS:
                k = rt / C.RETIRE_EYE_SACCADE_MS
                k = 1.0 - (1.0 - k) ** 2  # ease-out
                e_pan_off = self._retire_dir * C.RETIRE_AWAY_PAN_DEG * k
                e_tilt_off = C.RETIRE_AWAY_TILT_DEG * k
                n_pan_off = 0.0
            # Phase 2: 眼停住，脖子不动 (80-300ms)
            elif rt < C.RETIRE_EYE_SACCADE_MS + C.RETIRE_HOLD_MS:
                e_pan_off = self._retire_dir * C.RETIRE_AWAY_PAN_DEG
                e_tilt_off = C.RETIRE_AWAY_TILT_DEG
                n_pan_off = 0.0
            # Phase 3: 脖子按 25°/s 慢回中，叠加呼吸 (300-2100ms)
            elif rt < C.RETIRE_EYE_SACCADE_MS + C.RETIRE_HOLD_MS + C.RETIRE_NECK_DRIFT_MS:
                drift_ms = rt - C.RETIRE_EYE_SACCADE_MS - C.RETIRE_HOLD_MS
                k = drift_ms / C.RETIRE_NECK_DRIFT_MS
                # 眼慢慢回到中性
                e_pan_off = self._retire_dir * C.RETIRE_AWAY_PAN_DEG * (1 - k)
                e_tilt_off = C.RETIRE_AWAY_TILT_DEG * (1 - k)
                # 脖子以 25°/s 限速从 retire_neck_from 回中（绝对偏移，非增量）
                max_drift = C.RETIRE_NECK_DPS * drift_ms / 1000.0
                total = abs(self._retire_neck_from)
                actual = min(max_drift, total)
                n_pan_off = -math.copysign(actual, self._retire_neck_from) if total > 0.1 else 0.0
                # 叠加 OU-like 慢漂（±0.5°）
                n_pan_off += 0.5 * math.sin(rt / 400.0) * (1 - k)
            else:
                self._retire_t0 = 0
                self._retire_blink_fired = False
                self._gaze_hold_since = 0
                self._avoid_since = ts_ms
                self.presence.request("SOCIAL_AVOID", force=True)
                return 0.0, e_tilt, 0.0, False
            return n_pan_off, e_tilt + e_tilt_off, e_pan_off, force_blink

        # 进入 AVOID 后 300ms：away 偏移平滑衰减
        if hasattr(self, "_avoid_since") and ts_ms - self._avoid_since < 300:
            k = (ts_ms - self._avoid_since) / 300.0
            return -1.5 * (1 - k), e_tilt - C.SOCIAL_GAZE_UP_DEG * (1 - k), 0.0, False
        if self.presence._transition or self.presence.current == "SOCIAL_AVOID":
            return 0.0, e_tilt, 0.0, False
        if self._gaze_hold_since <= 0 or not hasattr(self, "_avoid_after_ms"):
            self._gaze_hold_since = ts_ms
            self._avoid_after_ms = self._pick_gaze_limit()
        elapsed = ts_ms - self._gaze_hold_since
        if elapsed > self._avoid_after_ms:
            # 启动撤回序列
            self._retire_t0 = ts_ms
            self._retire_dir = 1 if rng.rand() < 0.5 else -1
            self._retire_blink_fired = False
            self._retire_neck_from = n_pan
            return 0.0, e_tilt, 0.0, False
        return 0.0, e_tilt, 0.0, False

    def _pack(self, n_pan, n_tilt, e_pan, e_tilt, roll, off, d, src, ts, **kw):
        g = {
            "body_pan_deg": round(n_pan, 2), "body_tilt_deg": round(n_tilt, 2),
            "eye_pan_deg": round(e_pan, 2), "eye_tilt_deg": round(e_tilt, 2),
            "neck_roll_deg": round(roll, 2),
            "lid_closure_deg": 0,
            "offset_deg": round(off, 2), "distance_m": d,
            "source": src, "ts_ms": ts,
        }
        g.update(kw)
        return g

    def _emit(self, pan, tilt, d, src, ts):
        n_p, n_t, e_p, e_t = self._split(pan, tilt)
        return self._pack(n_p, n_t, e_p, e_t, 0.0, vergence_offset_deg(d), d, src, ts)


# ---------------- 自测 ----------------
if __name__ == "__main__":
    print("== 会聚角物理校验（对标文档：3m=0.6°, 1m=1.8°, 0.5m=3.5°）==")
    for d in (3.0, 1.0, 0.5):
        print(f"d={d}m → offset={vergence_offset_deg(d):.2f}°")

    print("\n== 像素→角度：人脸在画面中心偏右60px ==")
    pan, tilt = pixels_to_angle(320 + 60, 240, 320, 240, 320)
    print(f"pan={pan:.1f}° tilt={tilt:.1f}°")

    print("\n== 眼颈分配：目标 pan=40°（超眼饱和25°） ==")
    gs = GeometrySolver()
    np_, nt, ep, et = gs._split(40, 0)
    print(f"颈承担={np_:.0f}°  眼饱和在={ep:.0f}°")

    print("\n== 整链路：一张脸在画面右下方 ==")
    g = GeometrySolver()
    anchors = {7: {"eye_L": (380, 200), "eye_R": (420, 200),
                    "nose": (400, 230), "mouth": (400, 260)}}
    intent = {"state": "LOOKING_AT_PERSON", "active_track_id": 7, "scan": False}
    for ts in (0, 600, 1200, 1800):
        r = g.solve(intent, anchors, ts)
        print(f"t={ts}ms 目标点={r['source']:12s} 颈pan={r['body_pan_deg']:+6.1f} 眼pan={r['eye_pan_deg']:+5.1f} 会聚={r['offset_deg']:.2f}°")
