# 拟人注视系统 · 完整架构

## P1 感知层 `p1_perception/`

**输入**：摄像头帧（30fps）
**输出**：tracks[]（bbox/4锚点/关键点/embedding/conf）→ ZMQ PUB :5551（IF-1a）与 :5552（IF-1b）

| 模块 | 职责 |
|---|---|
| `pc_models.py` | YuNet人脸检测 + 478关键点 + ArcFace嵌入（NPU/RKNN推理） |
| `tracker.py` | 多脸追踪 + Kalman滤波 + track_id跨帧保持 |
| `embedder.py` | ArcFace 512维嵌入，身份识别（相似度阈值0.85） |
| `distance_kalman.py` | 深度估计 + Kalman平滑 + 逼近检测(LoomingGate) |
| `face_db.py` | 已知人脸库存储/比对 |
| `main.py` | 采集线程→NPU线程→CPU线程→ZMQ发布（IF-1a高频/IF-1b事件） |

---

## P2 决策层 `p2_decision/`

**输入**：tracks[]
**输出**：g dict（body_pan/tilt/roll + eye_pan/tilt + lid_up + offset_deg）

### ① 注视FSM（4状态）`state_machine.py`

```
                    检测到脸
                    +230ms确认+150ms注意
SCANNING ──────────────────────→ LOOKING_AT_PERSON
  ↑                                  │
  │                                  ├─ 看人一段时间后随机瞟开
  │                                  ↓
  │                             GAZE_AWAY
  │                             (20%连续瞟开)
  │                                  │
  └──(丢脸>1670ms)── RETURN ────────┘
                   (眼先回人，脖子慢转)
```

丢脸三段退出：0-167ms保持 → 167-667ms预测追踪 → 667-1670ms回中 → >1670ms扫描

注意力模式：FOCUSED / LISTENING / NEUTRAL（控制看人时长和瞟开频率）

### ② Presence拟人状态机（8族）`presence.py`

无人时自动切换：
```
IDLE → SCANNING → SEEKING → THINKING → RECOVERING
```
有人时：SOCIAL_GAZE / LISTENING / SOCIAL_AVOID

**6维Dims参数化**（每族有不同取值范围）：

| 维度 | 含义 | 驱动什么 |
|---|---|---|
| arousal 唤醒度 | 高=快/猛 | 摆头幅度、扫视频率 |
| load 认知负荷 | 高=认真 | 点头幅度、眨眼变稀 |
| valence 效价 | 高=积极 | 抬头倾向 |
| intent 意图 | 高=接近人 | 目光聚焦程度 |
| stability 稳定性 | 高=平滑 | 眼颈分配比、抖动抑制 |
| fatigue 疲劳度 | 高=疲惫 | 半垂睑、低头、慢摆 |

每个gen_*函数根据6维算出6通道微动作叠加（pan/tilt/roll/eye_pan/eye_tilt/lid）

### ③ 几何引擎 `geometry.py`
- `pixels_to_angle`：像素偏移→世界角度
- `EyeNeckSplitter`：眼颈分配（小角度眼睛先动，大角度脖子补位，带回滞）
- `TriangularSaccade`：注视点在双眼/鼻/嘴间跳
- `vergence_offset`：距离→会聚角（双眼内八）

### ④ 眼动/眨眼 `oculomotor.py` / `dynamics.py`
- 注视漂移 + 微扫视（saccade）
- `BlinkMachine`：IDLE→CLOSING→CLOSED→OPENING，随机间隔+认知负荷调节

### ⑤ 开机编排 `boot_choreography.py`
- 27关键帧 + smoothstep头通道缓动 + 0.2Hz呼吸叠加
- 17秒：低头闭眼→睁眼直视→看右上→扫视→看左→慢眨→收尾警觉

---

## P3 执行层 `driver.py` / `joint_mapper.py` / `codec.py`

**输入**：g dict
**输出**：11通道舵机指令（RS485）

| 模块 | 职责 |
|---|---|
| `driver.py` | L3限速：颈50°/s、眼200°/s、睑400°/s、roll 200°/s |
| `arbitration.py` | SoftLimiter软限位：颈yaw±45/pitch±30/roll±15，眼±25 |
| `joint_mapper.py` | 几何→11舵机（CH0颈yaw/CH1颈pitch/CH2颈roll/CH3-4眼yaw/CH5-6眼pitch/CH7-8上睑/CH9-10下睑），量化到器件分辨率 |
| `codec.py` | Protobuf编解码（IF-1a感知/IF-2决策/IF-4关节） |

---

## 数据流总览

```
摄像头帧
  │
  ▼ P1: 检测→追踪→嵌入→距离
  │   ZMQ PUB :5557
  ▼
  │ P2-FSM: 4状态判定（SCANNING/LOOKING/AWAY/RETURN）
  │ P2-Presence: 8族 × 6维Dims → 微动作叠加
  │ P2-Geometry: 眼颈分配 + 会聚 + 扫视
  │ P2-Blink: 眨眼状态机
  ▼
  │ P3-Driver: 限速（颈50/眼200/睑400 °/s）
  │ P3-Arbitration: 软限位（±45°等）
  │ P3-JointMapper: → 11舵机角度
  ▼
真机RS485  或  ws_bridge:9000 → VRM 3d/index.html 渲染
```

**调试模式**：`python test_harness.py --cam 0` 单进程整合P1+P2+VRM发布。
**真机部署**：P1跑RKNN、P2跑ARM、P3走RS485。
