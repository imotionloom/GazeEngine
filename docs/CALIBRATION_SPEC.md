# 注视系统 · 标定规范 v1.0（C1–C7）

> 适用：真机（RS485 舵机 + 编码器）与数字人（VRM 镜像）**同一套流程**。
> 产物：`calibration/active.json`，运行时只认它。

---

## 0. 设计原则

| # | 原则 | 含义 |
|---|---|---|
| **P1** | **一套流程** | 7 个标定项固定顺序（C3 内含 3a 极限 + 3b 零点两个阶段），真机与数字人完全一致；只有"后端实现"不同 |
| **P2** | **只测不读** | 所有量通过运行时接口（编码器回传 / 渲染端回传）测量；**禁止解析 `.vrm` / `.onnx` 等资产推断参数** |
| **P3** | **自动优先、手动兜底** | 每步都有「自动执行」与「手动确认」两种模式 |
| **P4** | **配置驱动** | 标定产物合并成 `calibration/active.json`；程序启动只加载它，缺项用安全默认值 |
| **P5** | **可断点** | 每步单独落盘（`steps/cN_*.json`），随时中止、随时续标 |
| **P6** | **可追溯** | 每次标定留 `history/<ts>/` 快照，带日期、后端类型、固件/模型版本 |

### 0.0 标定项一览

| # | 标定 | 谁需要 | 产物 | 开机复验 |
|---|---|---|---|---|
| C1 | 相机内参 | 两者 | `steps/c1_camera.json` | — |
| C2 | 手眼零位 | 两者 | `steps/c2_hand_eye.json` | — |
| **C3a** | **极限（成对）** | **两者** | `steps/c3_servo.json → limits` | **C3-quick**（0.9× 可达性） |
| C3b | 零点/方向/线性 | 两者 | `steps/c3_servo.json → channels` | — |
| C4 | 距离标定 | 两者 | `steps/c4_distance.json` | — |
| C5 | 延迟自标定 | 两者 | `steps/c5_latency.json` | 每次开机 |
| C6 | 量程与饱和 | 两者 | `steps/c6_look.json` | — |
| C7 | 精度 | 两者 | `steps/c7_precision.json` | **C7-quick** |



### 0.1 为什么数字人也要走同一套流程

数字人不是"另一套系统"，而是**执行后端的一个实现**。如果数字人用"解析模型文件"绕过标定，就会出现两个后果：

1. 数字人上调通的行为，接真机时参数不成立（历史上已经踩过：lid、preset、roll 都各断过一次）；
2. 标定工具本身得不到验证——真机第一次跑时才发现流程有 bug。

因此：**数字人只报告它真实存在的限制**（blendshape 量化、平滑滞后、饱和点），**不注入假噪声、不读文件**。C7 在数字人上得到的结论会是"分辨率受 0.01 权重量化限制 ≈0.25°、无回差、无噪声"——这是真话，也是它作为调试替身的已知短板。

---

## 1. 统一测量接口

### 1.1 数据结构

```python
{'ts_ms': 123456789,
 'joints_deg': {'neck_yaw': 0.0, 'neck_pitch': -1.2, 'neck_roll': 0.0,
                'eye_yaw_L': 3.1, 'eye_yaw_R': -0.9,
                'eye_pitch_L': 2.0, 'eye_pitch_R': 2.0,
                'lid_up_L': 0.0, 'lid_up_R': 0.0,
                'lid_low_L': 0.0, 'lid_low_R': 0.0},
 'source': 'encoder' | 'renderer',
 'confidence': 0.98}
```

### 1.2 后端契约（标定用，非 RT 路径）

```python
class ICalibBackend:
    def command(self, joints_deg: dict, ts_ms: int) -> None: ...
    def read_pose(self) -> 'PoseSample': ...          # 实际值，不是指令值
    def read_frame(self) -> 'np.ndarray | None': ...  # 相机帧（数字人可返回渲染帧）
    def capabilities(self) -> dict: ...               # 见 1.4
```

| 实现 | `read_pose` 来源 | 说明 |
|---|---|---|
| `ServoBusBackend` | IF-6 编码器（500–1000 Hz） | 真值 |
| `DigitalHumanBackend` | 渲染端每帧回传**实际应用**的关节角 | 必须包含 blendshape 量化与平滑后的结果，不能回传指令值 |

> **关键**：数字人回传的必须是"渲染端真正用来驱动的值"。若回传指令值，C7 会测出"零误差"，流程就失效了。

### 1.3 时间与同步

- 所有 `ts_ms` 用同一时钟（`time.time_ns()//1e6`），跨进程偏差由 C5 校验；
- 每次 `command` 后等待 `3 × t90`（来自 C5）再 `read_pose`，不足时重测。

### 1.4 能力声明（决定哪些步骤可跳过）

```json
{"actuators": ["neck_yaw", "..."], "has_encoder": true, "has_renderer": false,
 "eye_range_deg": 25.0, "lid_range_deg": 90.0}
```

数字人：`has_encoder=false, has_renderer=true`；真机反之。

---

## 2. C1 相机内参

| 项 | 内容 |
|---|---|
| **目的** | 把 `u,v ↔ 角度` 从"假设 90°"变成实测 |
| **前置** | 无 |
| **需要** | `read_frame()` |
| **依赖** | — |

**模型**：`u − cx = f·tan(pan)`，`v − cy = f·tan(tilt)`（方形像素、忽略畸变）。

### 自动流程（有标定板）
1. 提示"把棋盘格依次放到 9 个位置"（网格叠加在画面上，到位自动采集）；
2. 每帧检测角点 → 收集 12–15 张；
3. `cv2.calibrateCamera` → `fx, fy, cx, cy, k1`，RMS < 1 px。

### 手动流程（无标定板，两点法）
1. 页面叠加**角度刻度尺**（以画面中心为 0，每 5° 一条线）；
2. 操作者把标记放到 `±15° / ±25° / ±35°` 的实际位置（页面提示目标角度）；
3. 每次按空格采集 `(tan θ, u)`；
4. 最小二乘 `u = f·tanθ + cx`，垂直方向同理。

**误差控制**：位置误差 1 cm @1.5 m ≈ 0.38°；用大角度点（`tan` 放大）更准。

### 输出 `steps/c1_camera.json`
```json
{"fx": 452.3, "fy": 450.8, "cx": 321.5, "cy": 238.2, "k1": 0.0,
 "fov_h_deg": 70.4, "rms_px": 0.8, "method": "angle_ruler",
 "samples": [[-26.6, 118], [0, 320], [26.6, 522]]}
```

### 判定
| 通过条件 | 失败处理 |
|---|---|
| RMS < 1.5 px 且 `fov_h` ∈ [55°, 85°] | 若解出 90°±1 → 提示"仍是假设值，请重测"；若 `f` 与上次偏差 >10% → 标 `unstable` |

---

## 3. C2 手眼零位（3 距离）

| 项 | 内容 |
|---|---|
| **目的** | 修正"相机在胸口、眼睛在头上"的固定差与视差 |
| **前置** | C1 |
| **需要** | `read_frame()` + `command()` + `read_pose()` |
| **依赖** | C1 |

**模型**：
```
off_pan  = pan_dial  − pan_cam  ≈ yaw0                    （常数）
off_tilt = tilt_dial − tilt_cam ≈ pitch0 + Δy·(1/d)       （随距离）
```
未知量 3 个：`yaw0`、`pitch0`、`Δy`（眼-相机高度差）。

### 流程（自动记录 + 手动对位）
1. 页面用 **3×3 网格**提示当前点（第 n/9 点：左上/上中/…/右下），点格子可直接跳点；
2. 提示站位到给定距离，把对位杆放到该格对应位置；
3. 操作者用方向键调整，直到**视线正对标记**；
4. 按「采集」记录 `(point, d, pan_cam, tilt_cam, pan_dial, tilt_dial, neck_yaw, eye_yaw, mode)`；
5. 自动跳到下一点；3 个距离 × 9 点 = 27 组。

### 脖子怎么处理（关键）

`pan_dial` 记录的是**总注视角**，不是眼球角。因为运行时的注视 = 颈 + 眼，标定必须命令同一种东西，否则测出来的偏置对不上。

页面上有三种控制模式：

| 模式 | 行为 | 用途 |
|---|---|---|
| **联动**（默认） | `ey = clamp(total, ±25)`，`neck = total − ey`，与运行时 `EyeNeckSplitter` 一致 | 正式标定 |
| 眼睛 | 只动眼球（±30°） | 查眼动范围/抖动 |
| 脖子 | 只动脖子（±45°） | 查头部行程 |

> 数字人尤其需要「联动」：它的眼球表情只有 ±30°，超过就必须靠头转，否则大角度永远对不准。

### 拟合
最小二乘解 `yaw0`、`pitch0`、`Δy`；残差 > 2° 的点在页面上标红，支持单点重测。

### 输出 `steps/c2_hand_eye.json`
```json
{"yaw0_deg": 0.8, "pitch0_deg": -2.1, "dy_m": 0.31, "dz_m": 0.04,
 "residual": {"rms_pan": 0.4, "rms_tilt": 0.9, "max": 1.6},
 "points": [{"d": 0.8, "pan_cam": 12.3, "tilt_cam": -4.1,
             "pan_dial": 13.1, "tilt_dial": 16.9}]}
```

### 判定：任意点残差 < 2°；否则提示"检查站位/相机是否松动"

---

## 4. C3 舵机标定（3a 极限 → 3b 零点/方向/线性）

| 项 | 内容 |
|---|---|
| **目的** | 3a：把每根轴的**成对物理极值**摸出来（左/右、上/下）；3b：让"指令 12.0°"等于"关节真的在 12.0°" |
| **前置** | 无（3a 必须最先做，是后续所有大角度动作的安全前提） |
| **需要** | `command()` + `read_pose()`（编码器，或数字人渲染端回读） |
| **依赖** | 3b 依赖 C5 的 `t90` 决定等待时间 |

> **为什么 3a 必须在 3b 之前**：3b 要扫 ±20°，如果某轴实际只有 15°，没摸极限就会直接顶到机械限位。真机上这是"出问题"的主要来源（堵转、过流、齿轮打齿）。

### 4.0 三个不同的"极值"（不要混）

| 概念 | 定义 | 谁用 |
|---|---|---|
| **机械硬限位** `raw` | 再走就不动（卡住/堵转） | 安全上限，绝不越过 |
| **可用极值** `usable` | `raw − LIMIT_MARGIN_DEG`（默认 1.5°） | 运行时软限位 |
| **有效注视量程** | 角度还"看得见人"的范围 | 行为层（C6 单独测） |

### 4a. 极限（成对）

**探测方式**（每轴、每个方向分别做一次）：

```
从中心回 0，稳定 4·t90
循环：指令 += step（eye 1°、neck/lid 2°），等 2·t90，读编码器
停止判据（任一）：
  ① 编码器增量 < 20% × step，连续 2 步   → 机械卡住
  ② 后端回传 stall / 过流                → 立即停
  ③ 到达名义值 ×1.15（eye 32° / neck 55° / lid 95°）→ 防一直顶
可用极值 = 最后到达位置 − 1.5° 余量
```

**成对记录**：`pos`（右/上）、`neg`（左/下）分别存，因为机械行程通常**不对称**；
`asym_deg = raw_pos − raw_neg` 直接暴露偏装/歪轴。

**单向轴**（眼睑：0 = 全开）：负侧天然为 0，不判异常，只记录 `[0, raw_pos]`。

**边界情况**：

- 某一侧 `usable < LIMIT_MIN_USABLE_DEG`（5°）→ 该轴判废，运行时冻结；
- `usable_pos ≤ usable_neg` → 数据非法，整份丢弃并回退 `LIMIT_FALLBACK`。

### 4b. 零点 / 方向 / 线性

1. 在 `[neg, pos]` 区间内按 10/30/50/70/90% 取 5 个点（**不再用固定 ±20°**）；
2. 每点等 `3·t90` 后读回；
3. 线性拟合 `actual = scale·cmd + offset`，`sign = sign(scale)`，记 `r2`。

### 眼睑单独处理
`lid_up` 的 0–90 是"闭合程度"而非几何角：扫 0/15/30/45/60/75/90，读回并记录 `blink_threshold`（编码器上"瞳孔刚好被完全遮住"的值，由人工目视确认一次）。

### 输出 `steps/c3_servo.json`
```json
{"limits": {
   "neck_yaw":  {"neg": -43.6, "pos": 43.5, "raw_neg": -45.1, "raw_pos": 45.0,
                 "margin": 1.5, "asym_deg": -0.1, "method": "stall", "usable": true},
   "lid_up_L":  {"neg": 0.0, "pos": 88.5, "raw_neg": 0.0, "raw_pos": 90.0,
                 "margin": 1.5, "asym_deg": 90.0, "method": "stall", "usable": true}},
 "channels": {
   "neck_yaw": {"sign": 1, "scale": 1.02, "offset": -0.4, "min": -43.6, "max": 43.5,
                "r2": 0.99999}},
 "backend": "real"}
```

### 判定
- **3a**：`usable` 全为 true，且非眼睑轴 `|asym_deg| < 3°`（超过说明装配偏了，先修机械再标）；
- **3b**：每通道 `r2 > 0.999`；零位误差容差按通道分组（眼/颈 0.3°、眼睑 1.0°，因为眼睑 LSB 就有 0.5°）；否则标 `degraded`。

### 运行时接线（代码侧判定）
1. `SoftLimiter.set_limits(limits)` 注入**非对称** `[neg, pos]`；未标定通道回退对称默认；
2. `SoftLimiter.clamp()` 用 `limits_of(k)` 的 `[lo, hi]` 钳位，**不再假设 ±45/±25**；
3. `active.json` 缺 `limits_deg` → 用 `config.LIMIT_FALLBACK`（颈 ±35、眼 ±18、睑 0–80），并记 `fallback_used`；
4. 上电 `SELFTEST` 跑 **C3-quick**：验证 `0.9×` 极值仍可达，失败标 `degraded`；连续 3 次不响应 → `fault` + 回安全位。

### 快速复验（开机，~15 s）
不重新摸极限，只把每轴推到 `0.9×usable` 再回来，判断是否仍可达：
`got > 0.85 × target` 视为通过。碰撞、撞机、换电池后都会在这里暴露。

---

## 5. C4 距离标定

| 项 | 内容 |
|---|---|
| **目的** | `eye_dist_px → 米` |
| **前置** | C1（像素才有意义） |
| **需要** | `read_frame()` |
| **依赖** | C1 |

### 流程
人站 0.5 / 1.0 / 2.0 m 各 10 s → 记录 `eye_dist_px` 中位数。

### 拟合
`d = K / p`，`K = median(d_i·p_i)`；三点偏差 > 10% 时改用 `d = A/p + B`。

### 输出 `steps/c4_distance.json`
```json
{"model": "d=K/p", "K": 48.6, "samples": [[0.5, 96], [1.0, 49], [2.0, 24]],
 "err_pct": {"0.5m": 3.2, "1.0m": 1.8, "2.0m": 5.0}}
```

### 判定：0.5–2 m 相对误差 < 10%

---

## 6. C5 延迟自标定

| 项 | 内容 |
|---|---|
| **目的** | 给看门狗、颈延迟定时器、jitter buffer 提供实测值 |
| **前置** | 通信链路可用 |
| **需要** | 传输 ping-pong + `command()` / `read_pose()` |
| **依赖** | — |

| 阶段 | 测什么 | 方法 |
|---|---|---|
| C5.1 | 时钟一致性 | 各进程写 `CLOCK_MONOTONIC`，比对 |
| C5.2 | IPC 延迟 | IF-5/IF-6/control ping-pong ×1000 |
| C5.3 | 机械延迟 | 每关节 ±1° 阶跃 ×20，量首响应与 90% |
| C5.4 | 数字人 jitter | 渲染端 echo |

### 输出 `steps/c5_latency.json`
```json
{"ipc": {"if5_ms": {"mean": 0.4, "p99": 1.1}},
 "mech_ms": {"neck_pitch": {"first": 38, "t90": 210},
             "eye_pitch_L": {"first": 6, "t90": 24}},
 "digital_human": {"rtt_ms": 12, "jitter_p99": 8},
 "watchdog_max_age_ms": 100, "neck_delay_ms": 172}
```

### 判定：`p99(IPC) < 5 ms`；`neck_delay_ms = 目标时序 + (颈 t90 − 眼 t90)`

---

## 7. C6 眼睛量程与饱和（look 标定）

> 历史上这一步曾用"解析 `.vrm` 的 morph target"来做——**已废弃**（违反 P2）。
> 现在改为：**通过渲染端/编码器回传，实测"指令 → 实际偏转"在哪里饱和**。

| 项 | 内容 |
|---|---|
| **目的** | 实测眼睛有效量程、饱和点、四方向符号与增益 |
| **前置** | C3（真机）/ C5（数字人） |
| **需要** | `command()` + `read_pose()`（+ 可选的渲染帧截图） |
| **依赖** | C3 |

### 自动流程
1. 从 0 起，每次 +2° 命令 `eye_yaw_L`（单眼），等 `3·t90`，读回实际角；
2. 当"实际角增量 < 0.5 × 指令增量"连续 2 次 → 判定饱和，记 `saturation_deg`；
3. 四方向（左/右/上/下）各测一次；
4. 数字人额外读回 `expression_weight`（渲染端随 pose 一起回传），得到 `deg_per_weight`。

### 输出 `steps/c6_look.json`
```json
{"eye_range_deg": {"L": 24.8, "R": 25.1, "U": 22.0, "D": 21.5},
 "sign": {"pan": 1, "tilt": 1},
 "deg_per_weight": 30.0,
 "saturation_flags": {"U": true}}
```

### 判定：量程 ≥ 20°；四方向符号与约定一致；数字人 `deg_per_weight ∈ [26, 34]`（默认 30°）

---

## 8. C7 精度标定

| 项 | 内容 |
|---|---|
| **目的** | 实测"能不能分到 0.5°"，并由精度**反推运行时参数** |
| **前置** | C3、C5 |
| **需要** | `command()` + `read_pose()` |
| **依赖** | C3、C5 |

### 8.1 指标（逐通道）

| 指标 | 定义 |
|---|---|
| 分辨率 `r` | 能被检出的最小指令步长 |
| 重复性 `σ` | 同向 20 次到同一点的标准差 |
| 回差 `b` | 上/下两条路径的落点差 |
| 带宽 `t90` | ±2° 阶跃到 90% 的时间（复用 C5） |
| 噪声 `n` | 静止 2 s 的标准差 |

```
r_eff = max(r, 3n, b/2, encoder_LSB)      # 编码器 LSB 是硬下限
```

### 8.2 测量序列

**M1 分辨率**（每通道）
```
for δ in [0.05, 0.1, 0.2, 0.3, 0.5, 1.0]:
    for trial in 1..5:
        回 base → 稳定 → 读 e0
        发 base+δ → 等 3·t90 → 读 e1
    r = 满足 median(e1−e0) > 3n 的最小 δ
```
**M2 重复性**：从下方接近 +1°，20 次。
**M3 回差**：`−1°→+1°` 与 `+3°→+1°` 落点差。
**M4 阶跃**：`0→+2°`，记 `t10/t90/过冲/稳定`。
**M5 噪声**：保持 0°，2 s。

### 8.3 两种模式

| 模式 | 时机 | 内容 | 耗时 |
|---|---|---|---|
| **C7-quick** | 每次开机 | 每通道一次 ±0.5° 往返，与 `active.json` 比对 | ~5 s |
| **C7-full** | 首次 / 人工触发 / 换硬件 | 完整 M1–M5 | ~2 min |

quick 判定：`|实测 − profile| > max(2σ, 0.3°)` → 标 `degraded`；连续 3 次不响应 → 标 `fault`，该轴冻结。

### 8.4 由精度反推参数

| 参数 | 公式 |
|---|---|
| `cmd_quantum` | `= r_eff` |
| `deadzone` | 眼 `2·r_eff` / 颈 `1.5·r_eff` |
| `micro_amp_min` | `= r_eff` |
| `micro_amp_max` | `= min(1.0°, 10·r_eff)` |
| `smooth_tau` | `≈ t90/2.3` |
| `min_saccade_amp` | `= r_eff` |
| `vergence_resolution` | `= sqrt(r_L² + r_R²)` |
| `vergence_max_distance` | `= PD / (2·tan(3·vergence_resolution))` |

> 例：`r_eff = 0.24°` → `vergence_resolution ≈ 0.34°` → `vergence_max_distance ≈ 1.8 m`。
> 即"2 m 以外不再做会聚"——比乱调更像人。

### 8.5 输出 `steps/c7_precision.json`
```json
{"channels": {"eye_pitch_L": {"encoder_lsb_deg": 0.09, "noise_deg": 0.04,
   "resolution_deg": 0.20, "repeatability_deg": 0.06, "backlash_deg": 0.12,
   "effective_step_deg": 0.24,
   "step": {"t10_ms": 12, "t90_ms": 26, "overshoot_pct": 4, "settle_ms": 45},
   "requirement_deg": 0.5, "pass": true}},
 "derived": {"deadzone_deg": {"eye": 0.48, "neck": 0.9, "lid": 1.2},
   "cmd_quantum_deg": {"eye": 0.24, "neck": 0.6},
   "micro_amp_range_deg": [0.24, 1.0],
   "smooth_tau_ms": {"eye": 11, "neck": 9},
   "vergence_resolution_deg": 0.34, "vergence_max_distance_m": 1.8},
 "overall": {"pass": false, "failed": ["neck_yaw"]}}
```

### 8.6 判定与降级

| 结果 | 处置 |
|---|---|
| `r_eff ≤ 0.5°` | ✅ 精细参数组 |
| `0.5° < r_eff ≤ 1.0°` | ⚠️ 死区放大、微扫视下限提高、**关闭会聚** |
| `r_eff > 1.0°` | ❌ degraded；该轴亚度特性全关 |
| 单次不响应 | fault；P2 冻结该轴，P3 回安全位 |

可选提精度手段：**抖动平均**（dither）、**单侧接近**（把回差变固定偏差）、**降指标**。

---

## 9. 配置文件

### 9.1 目录

```
calibration/
├── active.json          # 运行时唯一入口（合并产物）
├── meta.json            # 版本 / 日期 / 后端 / 校验
├── steps/
│   ├── c1_camera.json
│   ├── c2_hand_eye.json
│   ├── c3_servo.json
│   ├── c4_distance.json
│   ├── c5_latency.json
│   ├── c6_look.json
│   └── c7_precision.json
└── history/
    └── 20261028_1130_real/   # 每次完整标定的快照
```

### 9.2 合并格式 `active.json`

```json
{"schema": 1, "backend": "real", "generated_at": "2026-10-28T11:30:00+08:00",
 "complete": {"c1": true, "c2": true, "c3": true, "c4": true,
              "c5": true, "c6": true, "c7": true},
 "camera":     { "from": "c1" },
 "hand_eye":   { "from": "c2" },
 "servo":      { "from": "c3" },
 "distance":   { "from": "c4" },
 "latency":    { "from": "c5" },
 "look":       { "from": "c6" },
 "precision":  { "from": "c7" },
 "runtime": {
   "fov_h_deg": 70.4, "fx": 452.3, "fy": 450.8, "cx": 321.5, "cy": 238.2,
   "yaw0_deg": 0.8, "pitch0_deg": -2.1, "dy_m": 0.31,
   "dist_k": 48.6,
   "deadzone_deg": {"eye": 0.48, "neck": 0.9},
   "micro_amp_range_deg": [0.24, 1.0],
   "smooth_tau_ms": {"eye": 11, "neck": 9},
   "vergence_max_distance_m": 1.8,
   "watchdog_max_age_ms": 100, "neck_delay_ms": 172,
   "look_deg_per_weight": 30.0,
   "limits_deg": {
     "neck_yaw": {"neg": -43.6, "pos": 43.5},
     "eye_yaw_L": {"neg": -23.5, "pos": 23.5},
     "lid_up_L": {"neg": 0.0, "pos": 88.5}
   }
  }
}
```

- `runtime` 是**扁平化后的有效参数**，程序只读这一段；
- `runtime.limits_deg` 由 C3a 产生，P2 启动时经 `calibration.apply()` 注入 `SoftLimiter`；
- `steps/*` 保留原始数据，供追溯与重拟合。

### 9.3 加载与降级

```
启动 → 读 calibration/active.json
  ├─ 文件缺失 / schema 不符 → 用 Defaults（保守：死区 1.0、微扫视关、会聚关）+ 告警
  ├─ 某步缺 → 只降级相关功能（例：缺 c2 → 只做像素角，不做手眼修正）
  ├─ C7-quick 体检不通过 → 覆盖对应 runtime 字段，降到上一档参数
  └─ C3-quick 极限复验失败 → 该轴标 degraded，运行范围收到 0.85×，连续 3 次失败 → fault
```

加载后把 `runtime` 冻结成不可变对象，运行期不允许再改。

实现：`p2_decision/calibration.py`

```python
cfg = calibration.load()        # 读 active.json，失败回退保守默认（不抛）
issues = calibration.apply(cfg) # 注入 SoftLimiter 等；返回需要人工关注的告警
```

`CalibConfig` 是 frozen dataclass，`geometry.GeometrySolver(cfg=...)`、
`joint_mapper`（经 `SoftLimiter`）都只从它取参数。

---

## 10. 标定向导（页面设计）

### 10.1 布局

```
┌──────────────────────────────────────────────────────────────────────┐
│ 标定向导       后端: [真机 ▼]      进度 3/7        [保存] [导出] [中止] │
├──────────────┬───────────────────────────────────┬───────────────────┤
│ ① 相机内参  ✅ │                                   │ 本步说明           │
│ ② 手眼零位  ✅ │                                   │ ─────────────      │
│ ③ 舵机标定  🔄 │        [ 相机预览 / 3D 视图 ]      │ 站位: 0.8 m 正中   │
│ ④ 距离标定  ○ │        （叠加网格/刻度/期望位置）   │ 第 5/15 点         │
│ ⑤ 延迟自标  ○ │                                   │                    │
│ ⑥ 量程饱和  ○ │                                   │ 判定: ✅ / ⚠️ / ❌ │
│ ⑦ 精度标定  ○ │                                   │ [采集] [重测]      │
├──────────────┴───────────────────────────────────┴───────────────────┤
│ [← 上一步]        [自动执行 ▶]   [手动模式]        [下一步 →]          │
└──────────────────────────────────────────────────────────────────────┘
```

### 10.2 状态机（每步通用）

```
idle → prepared → measuring ⇄ paused
                      ↓
                   fitting → verdict ─┬─ pass  → saved → next
                                      ├─ warn  → saved(降级) → next
                                      └─ fail  → 停在 verdict，可重测
```

页面与后端的交互（WS，`tools/calib_bridge.py`）：

```jsonc
// → 后端
{"cmd": "calib.start",  "step": "c3", "backend": "real"}
{"cmd": "calib.command","joints": {"neck_yaw": 5.0}, "hold_ms": 300}
{"cmd": "calib.read"}
{"cmd": "calib.abort"}
// ← 后端
{"evt": "calib.sample", "ts_ms": 1234, "pose": {...}, "frame_jpeg_b64": "..."}
{"evt": "calib.progress", "step": "c3", "done": 2, "total": 11}
{"evt": "calib.result", "step": "c3", "verdict": "pass", "payload": {...}}
```

### 10.3 每步的自动/手动

| 步 | 自动 | 手动 |
|---|---|---|
| C1 | 棋盘格自动采集 12 张 → 拟合 | 两点法：按空格标点 |
| C2 | 自动记录 + 自动拟合，红点提示重测 | 方向键微调、单点重测 |
| C3a | 全自动扫极限（每轴两个方向） | 人工确认无碰撞后继续 |
| C3b | 全自动扫线性 + 拟合 | 人工确认 R² |
| C4 | 自动采样 10 s | 手动触发采样 |
| C5 | 全自动 | 无需手动 |
| C6 | 自动扫到饱和 | 人工确认饱和点 |
| C7 | 全自动（quick ~5 s / full ~2 min） | 单通道重测 |

### 10.4 交互约定

- **空格** = 采集当前点；**←/→** = 单步微调；**Enter** = 下一步；**Esc** = 中止（保留已存步骤）；
- 每步完成即写 `steps/cN_*.json`，刷新/崩溃后可**续标**；
- 顶部"后端"切换会**重新加载**对应 `calibration/<backend>/` 目录，两套标定互不覆盖。

### 10.5 完成页

显示 7 步汇总（✅/⚠️/❌）、关键数字（FOV、手眼偏置、分辨率、会聚上限）、"导出"生成 `active.json` 与 `history/<ts>/` 快照。

### 10.6 当前实现（v1）

| 组成 | 文件 | 说明 |
|---|---|---|
| 桥 | `tools/calib_bridge.py` | HTTP 9100（静态）+ WS 9101；`--backend sim\|real` |
| 向导 | `3d/calib.html` | 七步 UI，只负责显示/采集/确认，计算全在桥里 |
| 后端 | `SimBackend` / `RealBackend` | sim 是测试替身（量化/噪声/回差/一阶响应），输出到 `calibration/sim/`；real 在 P3 落地前抛 `BackendUnavailable` |

```bash
python tools/calib_bridge.py --backend sim
python -m tools.calib_bridge --backend sim
# 浏览器： http://127.0.0.1:9100/calib.html
```

协议见 §10.2：手动步骤用 `calib.sample` + `calib.fit`；自动步骤用 `calib.run`，`"quick": true` 可做开机体检（C3 = 极限 0.9× 可达性，C7 = 分辨率抽查）。第 3 步页面上有两个按钮：**开始标定**（极限 → 线性，约 2 min）与 **快速复验**（约 15 s）。缺项在 `active.json` 里用 `CALIB_FALLBACK` 补齐，不留 `null`。

---

## 11. 检查清单（Checks）

| # | 通过条件 | 现场 5 秒验证 |
|---|---|---|
| C1 | RMS < 1.5 px，30° 处读数 30±1° | 标记放 30° |
| C2 | 任意点残差 < 2° | 站 1 m 正中看是否对视 |
| C3a | 成对极值可用、`|asym| < 3°` | 手动推到极值前 1.5° 不撞 |
| C3b | R² > 0.999，零位误差 ≤ 容差（眼颈 0.3° / 眼睑 1.0°） | 发 0°，目视居中 |
| C4 | 0.5–2 m 误差 < 10% | 站 1 m 看 `distance_m` |
| C5 | p99 < 5 ms | 看启动日志 |
| C6 | 量程 ≥ 20°，符号正确 | 拨 ±20° 观察 |
| C7 | 眼球 `r_eff ≤ 0.5°` | 看 `precision.json` 判定 |

**整机通过** = 7 步全 ✅，且 C7 的 `overall.pass = true`。

---

## 12. 附录

### 12.1 错误码

| 码 | 含义 | 处理 |
|---|---|---|
| `E_C1_UNDERCONSTRAINED` | 标定点共线/太少 | 增加角度点 |
| `E_C2_RESIDUAL` | 残差 > 2° | 检查站位、相机松动 |
| `E_C3_NONLINEAR` | R² < 0.999 | 检查机构卡滞 |
| `E_C3_LIMIT_ASYM` | `|asym_deg| > 3°` | 装配偏了，先修机械 |
| `E_C3_LIMIT_NARROW` | 可用行程 < 10° | 该轴判废，冻结 |
| `E_C5_TIMEOUT` | ping-pong 丢包 | 检查总线 |
| `E_C7_NORESP` | 通道不响应 | 标 fault，回安全位 |

### 12.2 何时重测

- 换舵机 / 拆装头部 / 撞机 → C3a（极限必重测）+ C3b + C7
- 移动相机 / 换镜头 → C1 + C2
- 换渲染模型 → C6
- 电池电压明显变化、温差 > 15 °C → C7-full

### 12.3 与设计文档的对应

| 标定 | 对应架构项 |
|---|---|
| C1/C2 | S2 自建模标定、几何层像素→角度 |
| C3/C7 | M0H.2 位置闭环、M0H.3 安全看门狗 |
| C4 | 会聚 `atan(PD/2d)` |
| C5 | §11 启动延迟自标定（C1–C4 阶段） |
| C6 | M1H.2 眼动量程 |
