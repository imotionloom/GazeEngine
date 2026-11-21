"""全局配置：所有阈值/比例/上限集中在这里。"""
import random

# 相机
CAM_W = 640
CAM_H = 480
FOV_H_DEG = 90.0

# 眼颈分配
EYE_MAX_DEG = 25.0
# 眼颈分配阈值：眼 5° 后开始转头，3° 内头回中
ENTER_PAN_DEG = 5.0
EXIT_PAN_DEG = 3.0
ENTER_TILT_DEG = 6.0
EXIT_TILT_DEG = 4.0
EYE_REST_PAN_DEG = 8.0
EYE_REST_TILT_DEG = 6.0
NECK_MAX_YAW = 35.0
NECK_MAX_PITCH = 8.0

# 死区
DEADZONE_OFFSET_DEG = 0.3

# 距离标定
PD_MM = 63.0
DIST_CALIB_K = 48.0   # d = K / eye_dist_px
DIST_MIN_M = 0.5
DIST_MAX_M = 3.0

# 三角扫视
SACCADE_WEIGHTS = (0.7, 0.2, 0.1)
SACCADE_INTERVAL_MIN_MS = 1200
SACCADE_INTERVAL_MAX_MS = 2400
SACCADE_LONGTAIL_P = 0.1
SACCADE_LONGTAIL_MIN_MS = 3000
SACCADE_LONGTAIL_MAX_MS = 5000

# SCANNING
SCAN_AMP_DEG = 5.0
SCAN_INTERVAL_MIN_MS = 2000
SCAN_INTERVAL_MAX_MS = 5000

# 灵动性
BREATH_AMP = 0.5
BREATH_TAU_MS = 2500
NOD_AMP = 0.3
NOD_TAU_MS = 4000
TREMOR_P_AMP = 0.3
TREMOR_P_TAU_MS = 500
TREMOR_T_AMP = 0.15
TREMOR_T_TAU_MS = 600
OVERSHOOT_MIN = 1.2
OVERSHOOT_MAX = 1.4
OVERSHOOT_MS = 80
TILT_ROLL_DEG = 4.0
TILT_ROLL_DURATION_MS = 1500
TILT_ROLL_INTERVAL_MIN_MS = 10000
TILT_ROLL_INTERVAL_MAX_MS = 20000

# 社交凝视
SOCIAL_GAZE_MIN_MS = 4000
SOCIAL_GAZE_MAX_MS = 8000
SOCIAL_GAZE_UP_DEG = 4.0
SOCIAL_GAZE_DURATION_MS = 1500

# 目光撤回（gaze_limit 到期后的"松掉"序列）
RETIRE_EYE_SACCADE_MS = 80      # 眼 saccade 到偏方向的时长
RETIRE_HOLD_MS = 220            # 眼停在偏方向，脖子不动
RETIRE_NECK_DRIFT_MS = 1800     # 脖子慢回中时长
RETIRE_AWAY_PAN_DEG = 15.0      # 眼扫到的偏方向幅度
RETIRE_AWAY_TILT_DEG = -5.0     # 眼扫到的偏方向（向下看）
RETIRE_BLINK_AT = 300           # 撤回开始后多少 ms 触发眨眼（停住后）
RETIRE_NECK_DPS = 25.0          # 脖子回程限速 °/s

# smooth pursuit
SP_FAST_DEG_PER_S = 25.0
SP_SLOW_DEG_PER_S = 12.0
SP_SLOW_GAIN = 0.5

# 渲染端
NECK_SHARE = 0.75
HEAD_SHARE = 0.25

# ---------------------------------------------------------------------------
# 标定（docs/CALIBRATION_SPEC.md C1–C7）
# ---------------------------------------------------------------------------
CALIB_DIRNAME = "calibration"
CALIB_ACTIVE = "active.json"

# 数字人眼球满量程（C6 标定；默认 30°，即指令 30° 对应权重 1.0）
LOOK_DEG_PER_WEIGHT = 30.0

# C7 精度指标：眼球 0.5° 是"会聚在 1.8 m 内有效"倒推出来的底线；
# 颈部是大关节放宽到 1.0°；眼睑不参与注视指向，2.0° 足够。
PRECISION_REQUIREMENT_DEG = {"eye": 0.5, "neck": 1.0, "lid": 2.0}

# 会聚需要"角度 ≥ N 倍分辨率"才有意义（低于此距离才做会聚）
VERGENCE_MIN_SNR = 3.0

# 缺少 active.json 时的保守默认（宁可迟钝，不可乱动）
CALIB_FALLBACK = {
    "deadzone_deg": {"eye": 1.0, "neck": 1.5},
    "micro_amp_range_deg": [0.0, 0.0],      # 关掉微扫视
    "vergence_max_distance_m": 0.0,          # 不做会聚
    "watchdog_max_age_ms": 100,
    "neck_delay_ms": 200,
}

# ---------------------------------------------------------------------------
# C3a 极限标定（每轴左右/上下分别测，成对）
# ---------------------------------------------------------------------------
# 探测方式：从中心按 LIMIT_STEP 向外走，编码器增量 < LIMIT_STALL_GAIN×步长
# 连续 2 步（或收到 stall/过流）即判定到硬限位，然后回退 LIMIT_MARGIN 作为可用极值。
LIMIT_STEP_DEG = 1.0
LIMIT_MARGIN_DEG = 1.5
LIMIT_PROBE_MAX_DEG = {"eye": 32.0, "neck": 55.0, "lid": 95.0}
LIMIT_STALL_GAIN = 0.20
LIMIT_MIN_USABLE_DEG = 5.0          # 可用行程小于它 → 该轴判废

# 没有标定结果时，禁止越过的保守边界（成对，非对称也支持）
LIMIT_FALLBACK = {
    "neck_yaw": {"neg": -35.0, "pos": 35.0},
    "neck_pitch": {"neg": -20.0, "pos": 20.0},
    "neck_roll": {"neg": -10.0, "pos": 10.0},
    "eye_yaw_L": {"neg": -18.0, "pos": 18.0},
    "eye_yaw_R": {"neg": -18.0, "pos": 18.0},
    "eye_pitch_L": {"neg": -18.0, "pos": 18.0},
    "eye_pitch_R": {"neg": -18.0, "pos": 18.0},
    "lid_up_L": {"neg": 0.0, "pos": 80.0},
    "lid_up_R": {"neg": 0.0, "pos": 80.0},
    "lid_low_L": {"neg": 0.0, "pos": 20.0},
    "lid_low_R": {"neg": 0.0, "pos": 20.0},
}

# 人离开后的"回神"细节
DEPART_OVER_DEG   = 1.5
DEPART_OVER_RATIO = 0.09
DEPART_OVER_MS    = 400.0
DEPART_SAC_DEG    = (2.5, 3.5)
DEPART_SAC_MS     = (90.0, 140.0)
DEPART_SAC_AT     = 0.35


