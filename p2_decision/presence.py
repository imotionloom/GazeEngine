"""
P2 · 拟人状态分层架构：Dims × Family × Transition
文档：拟人状态实现方案 v1.0 完整版

边界说明：
- PresenceController 负责全部 8 个族：有人走 SOCIAL_GAZE/LISTENING，无人走 IDLE→SCANNING→SEEKING→THINKING→RECOVERING
- 上层 FSM 的人脸跟踪负责计算目标角度，presence 负责拟人微动作叠加
"""
import random
import math
import copy


def clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def lerp(a, b, t):
    return a + (b - a) * clamp(t)


def _ease_over(k, over=0.08):
    """0→1 的缓入缓出；末尾允许 over 比例过冲后回稳（用于"扭头带惯性"）"""
    if k <= 0.0:
        return 0.0
    if k >= 1.0:
        return 1.0
    s = k * k * k * (k * (k * 6.0 - 15.0) + 10.0)        # smootherstep：慢起慢落
    if k > 0.6:                                            # 尾段过冲后回稳
        s += over * math.sin(math.pi * (k - 0.6) / 0.4)
    return max(0.0, min(1.0 + over, s))


def _nod_curve(t, period, amp, t0=0.0):
    """离散的低头/抬头事件：0.8s 下压 → 0.8s 保持 → 1.2s 回正；每次事件左右交替方向。
    tilt>0 = 低头，所以 sgn=+1 是低头、sgn=-1 是抬头。"""
    ph = (t - t0) % period
    if ph >= 2.8:
        return 0.0
    idx = int((t - t0) // period)
    sgn = 1.0 if idx % 2 == 0 else -1.0
    if ph < 0.8:
        k = ph / 0.8
    elif ph < 1.6:
        k = 1.0
    else:
        k = 1.0 - (ph - 1.6) / 1.2
    e = k * k * (3.0 - 2.0 * k)
    return sgn * amp * e


def _roll_event(t, period, amp, t0=0.0):
    """离散歪头事件：0.6s 缓入 → 1.5s 保持 → 0.4s 回正，每次换边。
    与 THINKING/IDLE 的 roll 风格统一（事件式，不是连续正弦）。"""
    DUR = 0.6 + 1.5 + 0.4
    ph = (t - t0) % period
    if ph >= DUR:
        return 0.0
    idx = int((t - t0) // period)
    sgn = 1.0 if idx % 2 == 0 else -1.0
    if ph < 0.6:
        k = ph / 0.6
    elif ph < 2.1:
        k = 1.0
    else:
        k = 1.0 - (ph - 2.1) / 0.4
    e = k * k * (3.0 - 2.0 * k)
    return sgn * amp * e


def _to6(p):
    """生成器输出统一成 6 元组 (neck_pan, neck_tilt, eye_pan, eye_tilt, lid, roll)"""
    return p if len(p) >= 6 else tuple(p) + (0.0,) * (6 - len(p))


def _at6(p, i):
    """安全取第 i 个分量（兼容旧的 5 元组快照）"""
    return p[i] if len(p) > i else 0.0


DIM_KEYS = ("arousal", "load", "valence", "intent", "stability", "fatigue")
SPEC_RANGES = {    "SOCIAL_GAZE":  {"arousal": (0.4, 0.6), "load": (0.1, 0.3), "valence": (0.5, 0.8),
                     "intent": (0.8, 1.0), "stability": (0.7, 0.9), "fatigue": (0.1, 0.3)},
    "SOCIAL_AVOID": {"arousal": (0.3, 0.5), "load": (0.1, 0.2), "valence": (0.3, 0.5),
                     "intent": (0.0, 0.3), "stability": (0.6, 0.8), "fatigue": (0.1, 0.3)},
    "LISTENING":    {"arousal": (0.3, 0.5), "load": (0.2, 0.4), "valence": (0.5, 0.7),
                     "intent": (0.7, 0.9), "stability": (0.8, 0.95), "fatigue": (0.1, 0.4)},
    "SCANNING":     {"arousal": (0.4, 0.6), "load": (0.1, 0.2), "valence": (0.4, 0.6),
                     "intent": (0.2, 0.4), "stability": (0.6, 0.8), "fatigue": (0.1, 0.4)},
    "SEEKING":      {"arousal": (0.6, 0.9), "load": (0.2, 0.4), "valence": (0.3, 0.5),
                     "intent": (0.3, 0.6), "stability": (0.5, 0.7), "fatigue": (0.1, 0.3)},
    "THINKING":     {"arousal": (0.3, 0.6), "load": (0.4, 0.9), "valence": (0.4, 0.6),
                     "intent": (0.1, 0.3), "stability": (0.3, 0.9), "fatigue": (0.2, 0.5)},
    "RECOVERING":   {"arousal": (0.1, 0.3), "load": (0.1, 0.3), "valence": (0.2, 0.4),
                     "intent": (0.0, 0.2), "stability": (0.8, 0.95), "fatigue": (0.6, 0.9)},
    "IDLE":         {"arousal": (0.1, 0.3), "load": (0.0, 0.2), "valence": (0.4, 0.6),
                     "intent": (0.0, 0.2), "stability": (0.9, 1.0), "fatigue": (0.3, 0.7)},
}

PRESETS = {
    "THINKING_HARD": ("THINKING", {"load": (0.85, 0.95), "arousal": (0.6, 0.8)}),
    "DOZING":        ("IDLE",     {"fatigue": (0.80, 0.90), "arousal": (0.10, 0.20)}),
}

FAMILY_MAX_S = {
    "SOCIAL_GAZE":  180.0,
    "SOCIAL_AVOID":  30.0,
    "LISTENING":    300.0,
    "SCANNING":      60.0,
    "SEEKING":       180.0,
    "THINKING":     400.0,
    "RECOVERING":   800.0,
    "IDLE":         900.0,
}
NEXT_FAMILY = {
    "SEEKING": "THINKING", "THINKING": "RECOVERING",
    "RECOVERING": "IDLE", "SOCIAL_AVOID": "SOCIAL_GAZE",
    "SCANNING": "SEEKING", "SOCIAL_GAZE": "SOCIAL_AVOID",
    "LISTENING": "SOCIAL_GAZE", "IDLE": "SCANNING",
}

def preset_ranges(name):
    base, over = PRESETS[name]
    r = dict(SPEC_RANGES[base])
    r.update(over)
    return r


class Dims:
    """6 个连续维度，0~1，方向写死：
    arousal 越高越快越猛 / load 越高越认真 / valence 越高越积极抬头
    intent 越高越接近人 / stability 越高越平滑 / fatigue 越高越疲惫
    """
    DIM_TAU_S = 6.0        # 回归时间常数
    DIM_SIGMA_RATIO = 0.40  # 稳态标准差 = 0.40 × 半宽

    def __init__(self, arousal=0.5, load=0.3, valence=0.5,
                 intent=0.5, stability=0.8, fatigue=0.2):
        self.arousal = arousal
        self.load = load
        self.valence = valence
        self.intent = intent
        self.stability = stability
        self.fatigue = fatigue
        self._center = {k: getattr(self, k) for k in DIM_KEYS}

    def resample(self, ranges, rng=None):
        """进入族时采样一次，并把采样值记为漂移中心"""
        rng = rng or random
        for k in DIM_KEYS:
            lo, hi = ranges[k]
            v = rng.uniform(lo, hi)
            setattr(self, k, v)
            self._center[k] = v

    def drift(self, dt_sec, ranges, rng=None):
        """OU 过程：向进入时的设定点回归 + 白噪声（精确离散）"""
        rng = rng or random
        dt = max(0.0, min(float(dt_sec), 0.5))
        if dt <= 0.0:
            return
        alpha = math.exp(-dt / self.DIM_TAU_S)
        k_noise = math.sqrt(max(0.0, 1.0 - alpha * alpha))
        for k in DIM_KEYS:
            lo, hi = ranges[k]
            sigma = (hi - lo) * 0.5 * self.DIM_SIGMA_RATIO
            v = getattr(self, k)
            v = self._center[k] + (v - self._center[k]) * alpha \
                + sigma * k_noise * rng.gauss(0.0, 1.0)
            setattr(self, k, max(lo, min(hi, v)))

    def as_dict(self):
        return {"arousal": round(self.arousal, 2), "load": round(self.load, 2),
                "valence": round(self.valence, 2), "intent": round(self.intent, 2),
                "stability": round(self.stability, 2), "fatigue": round(self.fatigue, 2)}

    def centers(self):
        return {k: round(v, 3) for k, v in self._center.items()}


# ---------------- 状态族 generator ----------------

# 8 个看向：(pan_sign, tilt_sign)，tilt>0 = 低头
_LOOK_DIRS = ((1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))


def _nod_side(t, period, amp, t0=0.0):
    """与 _nod_curve 同相同相位：低头时顺手往一侧看（同侧），每次事件换边。"""
    return _nod_curve(t, period, amp, t0)


def _look_raw(i):
    """每段随便挑一个方向（由段号哈希决定，可复现、不循环）"""
    r = random.Random((abs(i) * 2654435761 + 12345) & 0x7FFFFFFF)
    return r.randrange(8)


def _look_target(i):
    """避免连续两次盯同一个方向"""
    k = _look_raw(i)
    if i > 0 and k == _look_raw(i - 1):
        k = (k + 3) % 8
    return k

def gen_seeking(dims, t):
    """SEEKING：随机挑方向看（左右上下任意组合），过渡平滑、驻留长短不齐"""
    pan_amp = lerp(11, 20, dims.arousal)
    tilt_amp = lerp(4.0, 7.0, dims.arousal)
    seg = 1.35
    i = int(t / seg)
    ph = t - i * seg
    move = 0.25 + 0.25 * (((i * 37) % 100) / 100.0)      # 0.25-0.50s，驻留随之长短不一
    a = _LOOK_DIRS[_look_target(i)]
    b = _LOOK_DIRS[_look_target(i + 1)]
    e = 0.0 if ph < seg - move else _ease_over((ph - (seg - move)) / move, 0.08)
    pan = pan_amp * (a[0] + (b[0] - a[0]) * e)
    tilt = tilt_amp * (a[1] + (b[1] - a[1]) * e)
    roll = _roll_event(t, 12.0, 1.5)
    return pan * 0.3, tilt * 0.55, pan * 0.7, tilt * 0.45, 0.0, roll


def _fresh_think_state():
    """THINKING 持续状态的初始值。所有重置路径都调这里，避免字段清单漂移。"""
    return {
        "last": None, "left": 0.0, "tgt": (0.0, 0.0),
        "eye": (0.0, 0.0), "base": (0.0, 0.0), "neck": (0.0, 0.0),
        "roll": 0.0, "roll_t0": -99.0, "roll_next": 3.0, "roll_sign": 1,
        "vdir": 0.0, "tgt_t0": 0.0, "insight": False,
    }


def gen_thinking(dims, t):
    """THINKING（S2）：跳视-驻留模型。四种外观由维度连续决定，不做状态分支：

      ① 权衡: 中 load + 低 stability + 高 arousal → 停得短、跳得碎、幅度小
      ② 锁定: 高 load + 高 stability + 低 arousal → 停得久、侧上、头缓慢跟上
      ③ 徘徊: 中 load + 低 stability            → 停得短但幅度大（在几个点之间跳）
      ④ 走神: 低 load + 高 stability            → 不选点，连续慢漂

    眼先动、颈滞后跟随（share / tau 由 stability·load 连续给出）。
    返回 (neck_pan, neck_tilt, eye_pan, eye_tilt, lid)。
    """
    st = getattr(dims, "_think_state", None)
    if st is None:
        st = dims._think_state = _fresh_think_state()
    rng = getattr(dims, "_think_rng", None)
    if rng is None:
        # 用 GAZE_SEED 环境变量注入的种子，不复用 Random(0)
        seed = None
        try:
            import os
            seed = int(os.environ.get("GAZE_SEED", "0")) or None
        except (ValueError, TypeError):
            pass
        rng = dims._think_rng = random.Random(seed)
    if st["last"] is None:                               # 首次调用（_begin_transition 已主动重置）
        st.update(_fresh_think_state())
        st["last"] = t
        st["tgt_t0"] = t
        # 本轮"思考方向"：锁定态会偏上看（回忆）或偏下看（内部对话）
        st["vdir"] = rng.choice([-1.0, 1.0]) * rng.uniform(1.5, 3.0)
        dt = 0.0
    else:
        dt = max(0.0, min(0.25, t - st["last"]))
    st["last"] = t

    load, stab, ar, val = dims.load, dims.stability, dims.arousal, dims.valence

    dwell = clamp(0.5 + 3.2 * load * (1.9 - ar) ** 2, 0.9, 7.5) * lerp(1.0, 0.12, 1.0 - stab)
    amp = lerp(5.0, 22.0, stab) * lerp(1.10, 0.70, ar)
    vbias = st["vdir"] + (val - 0.5) * 8.0      # 本轮抽定的思考方向(±3~6°) + 情绪微调
    share = clamp(0.04 + 0.46 * stab * load + 0.18 * stab * (1.0 - load) * (1.0 - ar),
                  0.04, 0.45)
    tau = 0.30 + 1.20 * stab * stab
    drifting = (stab > 0.75) and (load < 0.35)
    micro_amp = lerp(0.30, 0.06, load * stab)

    # ---- 注视点 ----
    if drifting:
        # 走神：OU 慢漂，横向为主，垂直夹 ±8
        sig = amp * 0.33
        a = math.exp(-dt / 6.0) if dt > 0 else 1.0
        b = math.sqrt(max(0.0, 1.0 - a * a))
        tx = a * st["tgt"][0] + b * rng.gauss(0.0, sig)
        ty = a * st["tgt"][1] + b * rng.gauss(0.0, sig * 0.45)
        st["tgt"] = (max(-20.0, min(20.0, tx)), max(-8.0, min(8.0, ty)))
    else:
        st["left"] -= dt
        if st["left"] <= 0.0:
            prev_tgt_t0 = st["tgt_t0"]            # 上一个目标的到位时刻
            prev_age = t - prev_tgt_t0            # 在前一个目标上停留了多久
            tx = max(-amp, min(amp, rng.gauss(0.0, amp * 0.45)))
            # 垂直：本轮偏置 + 9s 周期的缓慢上下摆（"抬头想想 / 低头再想想"）
            ty = vbias + 6.0 * math.sin(2 * math.pi * t / 9.0) + rng.gauss(0.0, max(0.5, amp * 0.08))
            st["tgt"] = (tx, max(-10.0, min(10.0, ty)))
            st["tgt_t0"] = t                      # ① 记下到位时刻，用于"真静止"
            st["left"] = dwell
            # ② 想完一段：在前一个目标上停留 >2s 才可能"想到"。
            #    prev_tgt_t0<=0 排除进入 THINKING 的首次选点（那时还没真正思考过）。
            if (load > 0.55 and prev_tgt_t0 > 0 and prev_age > 2.0
                    and rng.random() < 0.25):
                st["insight"] = True

    # ---- 眼：约 3 帧到位（跳视）；base 追目标，微扫视只叠在输出上（不回灌）----
    bx, by = st["base"]
    k = 1.0 - math.exp(-dt / 0.022) if dt > 0 else 0.0
    bx += (st["tgt"][0] - bx) * k
    by += (st["tgt"][1] - by) * k
    st["base"] = (bx, by)
    settled = abs(st["tgt"][0] - bx) < 0.4 and abs(st["tgt"][1] - by) < 0.4
    # ① 真静止：到位后 1.5s 内不动，再用 1s 缓慢恢复微扫视
    age = t - st.get("tgt_t0", t)
    gain = clamp((age - 1.5) / 1.0) if settled else 0.0
    mx = math.sin(2 * math.pi * 0.35 * t) * micro_amp * gain
    my = math.cos(2 * math.pi * 0.30 * t) * micro_amp * 0.6 * gain
    ex, ey = bx + mx, by + my

    # ---- 颈：跟随 share/(1-share)·base（不含微扫视），一阶滞后 tau ----
    nk = 1.0 - math.exp(-dt / tau) if dt > 0 else 0.0
    share_tilt = min(0.45, share + 0.15)          # 垂直方向头参与更多，抬头/低头才看得见
    nx, ny = st["neck"]
    nx += (share / max(1e-6, 1.0 - share) * bx - nx) * nk
    ny += (share_tilt / max(1e-6, 1.0 - share_tilt) * by - ny) * nk
    st["neck"] = (nx, ny)

    # ---- 歪头（roll）：锁定/徘徊区的事件——歪一下、保持 2s、回正，不是持续摆动 ----
    if stab > 0.55 and load > 0.45:
        if t >= st["roll_next"]:
            st["roll_t0"] = t
            st["roll_next"] = t + rng.uniform(4.0, 7.0)
            st["roll_sign"] = 1 if rng.random() < 0.5 else -1
        ph = t - st["roll_t0"]
        if 0.0 <= ph < 0.6:
            rk = ph / 0.6                      # 0.6s 缓入
        elif ph < 2.6:
            rk = 1.0                           # 保持 2s
        elif ph < 3.0:
            rk = (3.0 - ph) / 0.4              # 0.4s 回正
        else:
            rk = 0.0
        st["roll"] = lerp(0.6, 2.5, stab) * rk * st["roll_sign"]
    else:
        st["roll"] = 0.0

    return nx, ny, ex, ey, 0.0, st["roll"]


def gen_recovering(dims, t):
    """RECOVERING（S3）：低头+慢呼吸+轻微晃头，眼睛保持睁开（模型眼睛小，不做半闭）"""
    head_low = lerp(6, 12, dims.fatigue)
    hold = lerp(3.0, 6.0, dims.fatigue)
    k = min(1.0, t / hold)
    # 眼睛保持睁开（lid=0 表示无额外闭合），只低头+慢晃
    return 0.0, head_low * k * 0.3 + math.sin(2*math.pi*t*0.16)*0.4*k, 0.0, 2.0 * k, 0.0, 0.0


def gen_idle(dims, t):
    """IDLE：呼吸+慢摆+微扫视，fatigue 半睁"""
    breath = math.sin(2 * math.pi * t * 0.2) * lerp(0.3, 0.8, dims.arousal)
    sway = math.sin(2 * math.pi * t * 0.07) * 1.0
    # idle_micro_gap：15-30s 一次 0.2° 微动
    gap_cyc = lerp(30.0, 15.0, dims.arousal)
    gap_t = t % gap_cyc
    micro = 0.2 * math.sin(math.pi * gap_t / 0.5) if gap_t < 0.5 else 0.0
    # 眼睛保持睁开（模型眼睛小，不做半闭）
    lid = 0.0
    pan = 0.0
    if dims.fatigue > 0.70:
        ph = t % 25.0
        amp = 4.0 if int(t / 25.0) % 2 == 0 else -4.0
        if ph < 2.5: k = ph / 2.5
        elif ph < 3.5: k = 1.0
        elif ph < 6.0: k = 1.0 - (ph - 3.5) / 2.5
        else: k = 0.0
        k = k*k*(3-2*k)
        pan = amp * k
    # 缓慢抬头/低头：0.2Hz（5s周期），叠加nod后峰值约±6.5°
    pitch = 2.0 * math.sin(2 * math.pi * t / 5.0) + _nod_curve(t, 8.0, 4.5, 2.0)
    # 歪头事件：15-25s 一次，0.8s 入 / 1.4s 保持 / 0.8s 回正
    rcyc = lerp(25.0, 15.0, dims.arousal)
    rph = t % rcyc
    if rph < 0.8:
        rk = rph / 0.8
    elif rph < 2.2:
        rk = 1.0
    elif rph < 3.0:
        rk = (3.0 - rph) / 0.8
    else:
        rk = 0.0
    roll = 2.5 * rk * (1 if int(t / rcyc) % 2 == 0 else -1)
    side_pan = _nod_side(t, 8.0, 2.0, 2.0)          # 低头时顺手往同侧看
    return ((sway + pan + side_pan) * 0.3, breath + pitch * 0.55,
            (sway + pan + side_pan) * 0.7 + micro, pitch * 0.45, lid, roll)


def gen_social_gaze(dims, t):
    """SOCIAL_GAZE：看人，目光稳定，偶尔扫视别处再回来（reorient_gap）"""
    micro = math.sin(2 * math.pi * t * lerp(0.5, 2.0, dims.arousal)) * lerp(0.6, 0.2, dims.stability)
    gap_cyc = lerp(45.0, 28.0, dims.arousal)
    gap_t = t % gap_cyc
    gap = 0.0
    if gap_t > gap_cyc - 0.3:
        gap = math.sin((gap_t - (gap_cyc - 0.3)) / 0.3 * math.pi) * 2.0
    # gap 主要给眼睛
    return micro * 0.3, 0.0, micro + gap * 0.7, 0.0, 0.0, 0.0


def gen_social_avoid(dims, t):
    """SOCIAL_AVOID：被看久了移开，方向进族时定一次"""
    center_v = dims._center.get("valence", 0.4)
    side = -1 if center_v > 0.4 else 1
    amp = lerp(1.5, 3.0, dims.arousal)
    return side * amp * 0.3, 0.0, side * amp * 0.7, lerp(0.5, 1.5, dims.valence), 0.0, 0.0


def gen_listening(dims, t):
    """LISTENING：听人，每 3-5s 一次微点头"""
    cyc = t % 4.0  # 4s 周期
    nod = 0.0
    if cyc < 0.3:  # 0.3s 内完成一次点头
        nod = math.sin(cyc / 0.3 * math.pi) * lerp(1.0, 3.0, dims.load)
    return 0.0, nod, 0.0, 1.0, 0.0, 0.0


def gen_scanning(dims, t):
    """SCANNING：随机小步环顾（眼睛先到位，头慢慢划过去）"""
    pan_amp = lerp(8, 12, dims.arousal)
    tilt_amp = lerp(3.0, 5.0, dims.arousal)
    seg = 1.75
    i = int(t / seg)
    ph = t - i * seg
    move = 0.30 + 0.35 * (((i * 53) % 100) / 100.0)      # 0.30-0.65s，驻留长短不一
    a = _LOOK_DIRS[_look_target(i)]
    b = _LOOK_DIRS[_look_target(i + 1)]
    e = 0.0 if ph < seg - move else _ease_over((ph - (seg - move)) / move, 0.05)
    pan = pan_amp * (a[0] + (b[0] - a[0]) * e)
    tilt = tilt_amp * (a[1] + (b[1] - a[1]) * e)
    roll = _roll_event(t, 10.0, 1.2)
    return pan * 0.3, tilt * 0.55, pan * 0.7, tilt * 0.45, 0.0, roll

FAMILIES = {
    "SOCIAL_GAZE":  {"priority": 1, "dims": Dims(), "gen": gen_social_gaze,
                     "range": SPEC_RANGES["SOCIAL_GAZE"]},
    "SOCIAL_AVOID": {"priority": 1, "dims": Dims(), "gen": gen_social_avoid,
                     "range": SPEC_RANGES["SOCIAL_AVOID"]},
    "LISTENING":    {"priority": 1, "dims": Dims(), "gen": gen_listening,
                     "range": SPEC_RANGES["LISTENING"]},
    "SCANNING":     {"priority": 3, "dims": Dims(), "gen": gen_scanning,
                     "range": SPEC_RANGES["SCANNING"]},
    "SEEKING":      {"priority": 2, "dims": Dims(), "gen": gen_seeking,
                     "range": SPEC_RANGES["SEEKING"]},
    "THINKING":     {"priority": 4, "dims": Dims(), "gen": gen_thinking,
                     "range": SPEC_RANGES["THINKING"]},
    "RECOVERING":   {"priority": 5, "dims": Dims(), "gen": gen_recovering,
                     "range": SPEC_RANGES["RECOVERING"]},
    "IDLE":         {"priority": 6, "dims": Dims(), "gen": gen_idle,
                     "range": SPEC_RANGES["IDLE"]},
}


# ---- 过渡表：所有可达的跨族切换，时长来自规格 §5.2 / 阶段 2 ----
TRANSITIONS = {
    # 无人时间线（链式）
    ("IDLE", "SCANNING"):          0.6,
    ("IDLE", "SEEKING"):           0.4,
    ("SCANNING", "SEEKING"):       0.6,
    ("SEEKING", "SCANNING"):       0.6,
    ("SEEKING", "THINKING"):       1.5,
    ("THINKING", "SCANNING"):      1.0,
    ("IDLE", "THINKING"):        1.0,
    ("THINKING", "SEEKING"):     1.0,
    ("RECOVERING", "THINKING"):    1.5,
    ("RECOVERING", "SEEKING"):    1.5,
    ("THINKING", "RECOVERING"):    2.5,
    ("RECOVERING", "IDLE"):        1.2,
    # 交互打断
    ("IDLE", "SOCIAL_GAZE"):       0.6,
    ("SCANNING", "SOCIAL_GAZE"):   0.6,
    ("SEEKING", "SOCIAL_GAZE"):    0.6,
    ("THINKING", "SOCIAL_GAZE"):   0.6,
    ("SOCIAL_AVOID", "SOCIAL_GAZE"): 0.6,
    ("SOCIAL_GAZE", "LISTENING"):  0.5,
    ("SOCIAL_GAZE", "SOCIAL_AVOID"): 0.4,
    # 闭眼被打断：强制睁眼
    ("RECOVERING", "SOCIAL_GAZE"): 1.0,
    ("RECOVERING", "LISTENING"):   1.0,
    ("RECOVERING", "SOCIAL_AVOID"): 1.0,
    # 人走了，回无人
    ("SOCIAL_GAZE", "IDLE"):        0.6,
    ("SOCIAL_AVOID", "IDLE"):       0.6,
    ("LISTENING", "SOCIAL_GAZE"):   0.6,
    ("LISTENING", "IDLE"):          0.6,
    # 回无人
    ("THINKING", "IDLE"):          1.0,
    ("SCANNING", "IDLE"):          0.8,
    ("SEEKING", "IDLE"):           0.8,
}
DEFAULT_TRANSITION = 1.0
# 看门狗由过渡表推导：最长过渡 + 0.5s 余量
# （规格 §4 写 2s，但 §5.2 允许 THINKING→RECOVERING 2.5s，取后者更稳）
WATCHDOG_S = max(2.0, max(TRANSITIONS.values()) + 0.5)
_MISSING_LOGGED = set()

def transition_duration(frm, to):
    key = (frm, to)
    if key in TRANSITIONS:
        return TRANSITIONS[key]
    if key not in _MISSING_LOGGED:
        _MISSING_LOGGED.add(key)
        print(f"[presence][warn] 未登记的过渡 {frm}→{to}，用默认 {DEFAULT_TRANSITION}s")
    return DEFAULT_TRANSITION


# ---- M2.5 插曲：叠加在 gen() 输出之上的微动序列 ----
# 每个 keyframe = (time_s, pan, tilt, e_pan, e_tilt, lid, roll)
# 值为相对偏移量，smootherstep 插值

STRETCH_KEYFRAMES = [
    (0.0,   0.0,  0.0,  0.0,  0.0,  0.0,  0.0),   # 开始，睁眼
    (0.2,   0.0,  0.0,  0.0,  0.0,  0.0,  0.0),   # 定住0.2s（准备）
    (0.5,   0.0,  1.5,  0.0,  0.0,  0.75, 0.0),   # 稍微低头，开始闭眼
    (1.2,   0.0,  3.0, -3.0,  0.0,  1.0, -30.0),  # 往左歪+低头，眼睛全闭
    (1.4,   0.0,  3.75,-3.0,  0.0,  1.0, -33.0),  # 再沉一下，拉伸感
    (2.2,   0.0,  3.75,-3.0,  0.0,  1.0, -33.0),  # 左歪停0.8s，闭眼拉伸
    (2.8,   0.0,  1.5,  0.0,  0.0,  0.75, 0.0),   # 慢慢回正，眼睛开始睁开
    (3.0,   0.0,  1.5,  0.0,  0.0,  0.0,  0.0),   # 中间停0.2s，眼睛睁开
    (3.7,   0.0,  3.0,  3.0,  0.0,  1.0, 24.0),   # 往右歪+低头，眼睛又闭上
    (3.9,   0.0,  3.45, 3.0, 0.0,  1.0, 27.0),    # 再沉一下
    (4.6,   0.0,  3.45, 3.0, 0.0,  1.0, 27.0),    # 右歪停0.7s
    (5.2,   0.0,  0.0,  0.0,  0.0,  0.0,  0.0),   # 回正，眼睛睁开
]
STRETCH_DUR = 5.2

SLOW_NOD_KEYFRAMES = [
    (0.0,  0.0,  0.0,  0.0,  0.0,  0.0, 0.0),
    (0.5,  0.0,  2.0,  0.0,  0.0,  0.0, 0.0),   # 低头2°
    (0.9,  0.0,  0.0,  0.0,  0.0,  0.0, 0.0),   # 回正
    (1.4,  0.0,  2.0,  0.0,  0.0,  0.0, 0.0),   # 再低头
    (1.8,  0.0,  0.0,  0.0,  0.0,  0.0, 0.0),   # 回正
]
SLOW_NOD_DUR = 1.8

HEAD_COCK_KEYFRAMES = [
    (0.0,  0.0,  0.0,  0.0,  0.0,  0.0, 0.0),
    (0.4,  0.0,  0.0,  0.0,  0.0,  0.0, 3.0),   # 歪头3°
    (0.7,  0.0,  0.0,  0.0,  0.0,  0.0, 3.0),   # 保持
    (1.0,  0.0,  0.0,  0.0,  0.0,  0.0, 0.0),   # 回正
]
HEAD_COCK_DUR = 1.0

LOOK_UP_RECALL_KEYFRAMES = [
    (0.0,  0.0,  0.0,  0.0,  0.0,  0.0, 0.0),
    (0.4,  0.0,  0.0,  5.0, -5.0,  0.0, 0.0),   # 眼右上看5°
    (1.0,  0.0,  0.0,  5.0, -5.0,  0.0, 0.0),   # 保持
    (1.5,  0.0,  0.0,  0.0,  0.0,  0.0, 0.0),   # 回正
]
LOOK_UP_RECALL_DUR = 1.5

GLANCE_DOWN_KEYFRAMES = [
    (0.0,  0.0,  0.0,  0.0,  0.0,  0.0, 0.0),
    (0.3,  0.0,  0.0,  0.0,  5.0,  0.0, 0.0),   # 眼下看5°
    (0.7,  0.0,  0.0,  0.0,  5.0,  0.0, 0.0),   # 保持
    (1.0,  0.0,  0.0,  0.0,  0.0,  0.0, 0.0),   # 回正
]
GLANCE_DOWN_DUR = 1.0


def _interlude_sample(elapsed_s, keyframes, duration):
    """在关键帧之间做 smootherstep 插值，返回6元组偏移量"""
    if elapsed_s <= 0:
        return keyframes[0][1:]
    if elapsed_s >= duration:
        return keyframes[-1][1:]
    # 找当前区间
    for i in range(len(keyframes) - 1):
        t0 = keyframes[i][0]
        t1 = keyframes[i + 1][0]
        if t0 <= elapsed_s <= t1:
            k = (elapsed_s - t0) / max(0.001, t1 - t0)
            k = k * k * k * (k * (k * 6.0 - 15.0) + 10.0)  # smootherstep
            a = keyframes[i][1:]
            b = keyframes[i + 1][1:]
            return tuple(a[j] + (b[j] - a[j]) * k for j in range(6))
    return keyframes[-1][1:]


class PresenceController:
    """L0 调度层：优先级 + 过渡表 + 看门狗"""

    def __init__(self, seed=None):
        self._rng = random.Random(seed)
        # 每个 controller 独立一份 dims 副本；gen 函数共享（无状态部分）。
        # THINKING 的持续状态挂在 dims._think_state 上，随 dims 一起 per-instance。
        self.fams = {}
        for name, fam in FAMILIES.items():
            d = copy.deepcopy(fam["dims"])
            d._think_state = _fresh_think_state()
            d._think_rng = random.Random(seed)
            self.fams[name] = {**fam, "dims": d}
        self.current = "IDLE"
        self.t = 0.0
        self._idle_t = 0.0
        self._prev_person = False
        self._transition = None
        self._family_t = 0.0
        self._last_out = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        self._preset = None
        self._active_ranges = None  # 预设生效时覆盖族默认范围
        self._insight = False       # controller 级 eureka 事件（与 current 解耦）
        # M2.5 插曲状态：叠加在 gen() 输出之上，不替换
        self._interlude = None      # (elapsed_s, duration, keyframes_list)
        self._interlude_name = None
        self._next_stretch_t = self._rng.uniform(60, 120)
        self._next_nod_t = self._rng.uniform(30, 60)
        self._next_cock_t = self._rng.uniform(40, 80)
        self._next_recall_t = self._rng.uniform(40, 90)
        self._next_glance_t = self._rng.uniform(60, 120)
        # 时间轴阈值（带±25%抖动，每次跨阶段重新抖动，防止固定节奏）
        self._t_seeking = self._rng.uniform(90, 150)     # 原120s
        self._t_thinking = self._rng.uniform(240, 360)   # 原300s
        self._t_dozing = self._rng.uniform(480, 720)     # 原600s
        # 初始采样
        self.fams["IDLE"]["dims"].resample(SPEC_RANGES["IDLE"], self._rng)

    def _begin_transition(self, name, ranges=None):
        """收口：所有过渡都走这里，过渡开始前重采样目标族"""
        frm = self.current
        if ranges is None:
            ranges = SPEC_RANGES[name]
            self._active_ranges = None
            self._preset = None  # 默认过渡清掉预设标记
        if name == frm:
            self.fams[name]["dims"].resample(ranges, self._rng)
            self._active_ranges = ranges
            return
        self.fams[name]["dims"].resample(ranges, self._rng)
        # 主动重置 THINKING 的持续状态：每次"开始想事"都是新一段，
        # 不依赖 gen_thinking 里 t 回绕的隐式重置（避免过渡被打断后隔很久还沿用半截状态）。
        if name == "THINKING":
            td = self.fams["THINKING"]["dims"]._think_state
            td.update(_fresh_think_state())
            self._insight = False   # 清掉 controller 级残留
        self._active_ranges = ranges if ranges != SPEC_RANGES[name] else None
        dur = transition_duration(frm, name)
        from_pose = self._last_out if self._transition else None
        self._transition = (frm, name, self.t, dur, from_pose)

    def update(self, dt_ms, person_present=False):
        dt = dt_ms / 1000.0
        self.t += dt
        # 无人时累加 idle_t，过渡期间也加
        if not person_present:
            self._idle_t += dt

        # 过渡中：眼先回头延迟 0.2s
        if self._transition:
            frm, to, t0, dur = self._transition[:4]
            from_pose = self._transition[4] if len(self._transition) > 4 else None
            k = (self.t - t0) / dur
            elapsed_to = self.t - t0
            if k >= 1.0 or (self.t - t0) > WATCHDOG_S:
                self._transition = None
                self.current = to
                self._family_t = elapsed_to
            else:
                if from_pose is not None:
                    a = from_pose
                else:
                    a = self.fams[frm]["gen"](self.fams[frm]["dims"], self._family_t)
                b = self.fams[to]["gen"](self.fams[to]["dims"], elapsed_to)
                # 三段式：眼先回中 → 眼去目标 → 头延迟跟
                INTERACTIVE = ("SOCIAL_GAZE", "SOCIAL_AVOID", "LISTENING")
                CENTER_PHASE = 0.45
                HEAD_START = 0.30
                center_first = (to in INTERACTIVE) and (frm not in INTERACTIVE)
                # RECOVERING 闭眼被打断：lid 跟随整个过渡时长渐变张开
                lid_k = k
                # RECOVERING 闭眼被打断：lid 从当前值渐开，不硬跳
                def blend_eye(av, bv):
                    if not center_first:
                        return av + (bv - av) * min(1.0, k * 1.3)
                    if k < CENTER_PHASE:
                        t = k / CENTER_PHASE
                        return av + (0.0 - av) * t
                    t = (k - CENTER_PHASE) / (1.0 - CENTER_PHASE)
                    return 0.0 + (bv - 0.0) * t

                eye_delay = HEAD_START if center_first else 0.2
                k_head = max(0.0, (k - eye_delay) / (1.0 - eye_delay))
                out = (
                    a[0] + (b[0] - a[0]) * k_head,
                    a[1] + (b[1] - a[1]) * k_head,
                    blend_eye(a[2], b[2]),
                    blend_eye(a[3], b[3]),
                    a[4] + (b[4] - a[4]) * lid_k,
                    _at6(a, 5) + (_at6(b, 5) - _at6(a, 5)) * k_head,   # roll
                )
                # ⑤ 回神：从 THINKING 出来时的 0.9s 组合动作（抬眼 0.35s → 点头 0.4s）
                # 眨眼那一下等 BlinkMachine 接上 load 后再补（这次先不做）。
                if frm == "THINKING" and elapsed_to < 0.9:
                    lift = -2.2 * min(1.0, elapsed_to / 0.30)          # tilt<0 = 抬眼
                    nod = 0.0
                    if 0.35 <= elapsed_to < 0.75:
                        nod = 3.5 * math.sin(math.pi * (elapsed_to - 0.35) / 0.4)   # tilt>0 = 点头
                    off = lift + nod
                    out = (out[0], out[1] + off * 0.4, out[2], out[3] + off * 0.6,
                           out[4], out[5])
                self._last_out = out
                return out

        # 族超时：有人无人都要查，防止卡在交互态
        limit = FAMILY_MAX_S.get(self.current, 1e9)
        if self._family_t > limit and not self._transition:
            nxt = NEXT_FAMILY.get(self.current)
            # 疲劳度加权：越累越往前走，越精神越容易跳回IDLE
            # 疲劳度 = idle_t / t_dozing（0=刚坐下，1=坐了很久）
            fatigue = min(1.0, self._idle_t / max(1.0, self._t_dozing))
            idle_fams = {"IDLE", "SCANNING", "SEEKING", "THINKING", "RECOVERING"}
            if self.current in idle_fams:
                # 跳回IDLE的概率：疲劳度低时60%，中时40%，高时20%
                back_prob = 0.6 - 0.4 * fatigue
                if self._rng.random() < back_prob:
                    nxt = "IDLE"
            if nxt and nxt != self.current:
                self._begin_transition(nxt)
            else:
                # 自超时（如 IDLE）：只复位时钟，不重采样、不清预设
                self._family_t = 0.0

        # 有人时清掉预设（DOZING 疲态不该带到有人）
        if person_present and self._preset:
            self.clear_preset()
        # 人来：重置无人时间轴（被唤醒后从精神期重新开始），清掉进行中的插曲
        if person_present and not self._prev_person:
            self._idle_t = 0.0
            self._interlude = None
            self._interlude_name = None
            self._next_stretch_t = self._rng.uniform(60, 120)
            self._next_nod_t = self._rng.uniform(30, 60)
            self._next_cock_t = self._rng.uniform(40, 80)
            self._next_recall_t = self._rng.uniform(40, 90)
            self._next_glance_t = self._rng.uniform(60, 120)
            # 重置时间轴阈值（重新无人时节奏重新随机）
            self._t_seeking = self._rng.uniform(90, 150)
            self._t_thinking = self._rng.uniform(240, 360)
            self._t_dozing = self._rng.uniform(480, 720)
        self._prev_person = person_present

        # 有人：只推进过渡，不动时间线
        if person_present:
            return self._emit_current(dt)

        # 无人时：疲劳度很高时自动进入DOZING预设（在IDLE族内，不强制切族）
        fatigue = min(1.0, self._idle_t / max(1.0, self._t_dozing))
        if fatigue > 0.8 and self.current == "IDLE" and not self._preset and not self._transition:
            self._active_ranges = preset_ranges("DOZING")
            self._preset = "DOZING"

        fam = self.fams[self.current]
        ranges = self._active_ranges or fam["range"]
        fam["dims"].drift(dt, ranges, self._rng)
        self._family_t += dt
        base_out = _to6(fam["gen"](fam["dims"], self._family_t))

        # ---- M2.5 插曲触发（仅无人时，且不在过渡中）----
        if not self._transition:
            # 检查该不该触发新插曲
            if self._interlude is None:
                self._try_trigger_interlude()
            # 进行中的插曲：叠加在gen输出之上
            if self._interlude is not None:
                elapsed, dur, kfs = self._interlude
                elapsed += dt
                if elapsed >= dur:
                    self._interlude = None
                    self._interlude_name = None
                else:
                    self._interlude = (elapsed, dur, kfs)
                    off = _interlude_sample(elapsed, kfs, dur)
                    # lid_off是闭眼程度（0=不闭，1=全闭），lid_up要减，其他都是加
                    base_out = (
                        base_out[0] + off[0],  # pan
                        base_out[1] + off[1],  # tilt
                        base_out[2] + off[2],  # epan
                        base_out[3] + off[3],  # etilt
                        base_out[4] - off[4],  # lid_up（off是闭眼程度，减）
                        base_out[5] + off[5],  # roll
                    )

        self._last_out = base_out
        return self._last_out

    def _emit_current(self, dt):
        """输出当前族姿态，不推进时间线"""
        fam = self.fams[self.current]
        ranges = self._active_ranges or fam["range"]
        fam["dims"].drift(dt, ranges, self._rng)
        self._family_t += dt
        out = _to6(fam["gen"](fam["dims"], self._family_t))
        self._last_out = out
        return out

    def _next_interval(self, lo, hi):
        """下次间隔：幂律分布（有时很近，有时很久），80%正常区间，20%很短（连续动作），模拟真人聚类效应。"""
        if self._rng.random() < 0.2:
            # 20% 很短：聚类效应（连续动作）
            return self._rng.uniform(lo * 0.3, lo * 0.8)
        # 80% 幂律：大部分在低端，少数远离中心（重尾）
        # paretovariate(alpha) 生成 >=1 的幂律，缩放后截断
        alpha = 2.5
        raw = self._rng.paretovariate(alpha)  # >= 1，重尾
        val = lo * raw
        val = max(lo, min(hi * 2.5, val))   # 截断，保留长尾偶尔出现
        return val

    def _try_trigger_interlude(self):
        """根据当前族和时间，决定是否触发插曲。触发后记录下次时间。80%触发，20%跳过。"""
        c = self.current
        # STRETCH：IDLE期（停顿中扭脖子），每60-120s
        if c == "IDLE" and self._idle_t >= self._next_stretch_t:
            if self._rng.random() < 0.8:
                self._interlude = (0.0, STRETCH_DUR, STRETCH_KEYFRAMES)
                self._interlude_name = "STRETCH"
            # 重新抖动下一阶段阈值，防止固定节奏
            self._next_stretch_t = self._idle_t + self._next_interval(60, 120)
            return
        # SLOW_NOD：THINKING期，每30-60s
        if c == "THINKING" and self._idle_t >= self._next_nod_t:
            if self._rng.random() < 0.8:
                self._interlude = (0.0, SLOW_NOD_DUR, SLOW_NOD_KEYFRAMES)
                self._interlude_name = "SLOW_NOD"
            # 重新抖动下一阶段阈值
            self._next_nod_t = self._idle_t + self._next_interval(30, 60)
            return
        # HEAD_COCK：SEEKING期，每40-80s
        if c == "SEEKING" and self._idle_t >= self._next_cock_t:
            if self._rng.random() < 0.8:
                self._interlude = (0.0, HEAD_COCK_DUR, HEAD_COCK_KEYFRAMES)
                self._interlude_name = "HEAD_COCK"
            # 重新抖动下一阶段阈值
            self._next_cock_t = self._idle_t + self._next_interval(40, 80)
            return
        # LOOK_UP_RECALL：THINKING期，每40-90s（想起什么事，眼右上看）
        if c == "THINKING" and self._idle_t >= self._next_recall_t:
            if self._rng.random() < 0.8:
                self._interlude = (0.0, LOOK_UP_RECALL_DUR, LOOK_UP_RECALL_KEYFRAMES)
                self._interlude_name = "LOOK_UP_RECALL"
            # 重新抖动下一阶段阈值
            self._next_recall_t = self._idle_t + self._next_interval(40, 90)
            return
        # GLANCE_DOWN：IDLE/SCANNING期，每60-120s（低头看一眼下面）
        if c in ("IDLE", "SCANNING") and self._idle_t >= self._next_glance_t:
            if self._rng.random() < 0.8:
                self._interlude = (0.0, GLANCE_DOWN_DUR, GLANCE_DOWN_KEYFRAMES)
                self._interlude_name = "GLANCE_DOWN"
            # 重新抖动下一阶段阈值
            self._next_glance_t = self._idle_t + self._next_interval(60, 120)
            return

    def take_insight(self):
        """消费一次"想到"事件（供 BlinkMachine 做双连眨）。
        insight 是 controller 级事件：无论 current 在哪个族、是否在过渡，
        都从 THINKING dims 上轮询，并搬到 self._insight。"""
        td = self.fams["THINKING"]["dims"]._think_state
        if td.get("insight"):
            td["insight"] = False
            self._insight = True
        if self._insight:
            self._insight = False
            return True
        return False

    def sample(self):
        """获取上一次 update 的结果，不推进时钟"""
        return getattr(self, "_last_out", (0, 0, 0, 0, 0, 0))

    def clear_preset(self):
        if self._preset is None:
            return
        self._preset = None
        self._active_ranges = None
        if not self._transition:
            self.fams[self.current]["dims"].resample(SPEC_RANGES[self.current], self._rng)
        return True

    def request(self, name, force=False):
        """优先级打断：高优先级可打断低优先级"""
        nxt = self.fams[name]
        if not force and nxt["priority"] >= self.fams[self.current]["priority"]:
            return
        if self._transition and self._transition[1] == name:
            return
        self._begin_transition(name)

    def apply_preset(self, name):
        """应用预设：同族只换参数，跨族走过渡"""
        base, _ = PRESETS[name]
        if base == self.current:
            _r = preset_ranges(name)
            self.fams[base]["dims"].resample(_r, self._rng)
            self._active_ranges = _r
        else:
            self._begin_transition(base, preset_ranges(name))
        self._preset = name
        return True

    def debug_info(self):
        """调试面板数据"""
        fam = self.fams[self.current]
        tr = self._transition
        return {
            "family": self.current,
            "priority": fam["priority"],
            "family_t": round(self._family_t, 2),
            "idle_t": round(self._idle_t, 2),
            "dims": fam["dims"].as_dict(),
            "ranges": self._active_ranges or fam["range"],
            "center": fam["dims"].centers(),
            "preset": self._preset,
            "transition": None if not tr else {
                "from": tr[0], "to": tr[1],
                "progress": round(min(1.0, (self.t - tr[2]) / max(1e-6, tr[3])), 3),
            },
        }


if __name__ == "__main__":
    pc = PresenceController()
    for t in range(0, 60000, 1000):
        out = pc.update(1000, False)
        print(f"t={t/1000:.0f}s state={pc.current:12s} n_pan={out[0]:+.2f} lid={out[4]:.2f}")
