# 拟人注视系统 - 动作库架构设计文档

> 版本：v2.0 定稿
> 日期：2026-11-04
> 状态：设计完成，待实施

---

## 一、背景与目标

### 现状问题
1. 动作定义散落在 `presence.py`、`actions.py`、`test_interlude.py` 三处，改一处要同步三处
2. 基础运动参数硬编码在 gen 函数里，调参数要改代码重启
3. 加新动作要改多个文件，效率低
4. 编辑器和正式系统动作不同步

### 设计目标
- **可可视化编辑**：插曲动作和基础运动参数都能在编辑器里调
- **注册即使用**：加新动作/新运动模式，注册一下就能用，不用改主流程代码
- **数据驱动**：动作参数、触发规则、时间节奏都从配置文件读
- **插件化架构**：生成器可插拔，主流程只认统一接口

---

## 二、核心设计原则

### 一句话总结
> **数据负责"可编辑的值和片段"，代码负责"状态、耦合、顺序和安全"。**

### 三类东西，三种处理方式

| 类型 | 内容 | 处理方式 |
|------|------|----------|
| 插曲动作 | STRETCH、点头、歪头小段动作 | 完全数据化（JSON keyframe） |
| 基础运动 | 呼吸、慢摆、扫视等底色运动 | 抽参数和波形，公式留在代码 |
| 状态机/几何 | leave/pause/saccade/tracking | 只抽阈值，逻辑留在代码 |

---

## 三、时间基准（统一约定）

| 模块 | 时间单位 | 说明 |
|------|----------|------|
| 主循环 dt | 秒 | PresenceController 传入 |
| PoseClip.elapsed | 秒 | 动作播放进度 |
| ActionManager | 秒 | 插曲调度 |
| BaseMotion.sample(t) | 秒 | 基础运动时间 |
| BlinkMachine | 毫秒 | 保持现有接口，调用点转换 |
| Driver | 秒 | 限速单位 deg/s |

**规则：**
- 全系统统一用秒作为内部时间单位
- BlinkMachine 保持 ms 接口，DynamicsLayer 调用时 `ts_sec * 1000` 转换
- PoseClip / ActionManager / BaseMotion 全部用秒
- 配置文件里所有时间字段都是秒

---

## 四、数据契约（Pose / Offset 定义）

### 4.1 Pose（绝对姿态）

```python
@dataclass
class Pose:
    neck_pan: float = 0.0      # 脖子左右转头，单位：度，正=右
    neck_tilt: float = 0.0     # 脖子上下点头，单位：度，正=低头
    neck_roll: float = 0.0     # 脖子左右歪头，单位：度，正=右倾
    eye_pan: float = 0.0       # 眼睛左右偏移，单位：度，正=右
    eye_tilt: float = 0.0      # 眼睛上下偏移，单位：度，正=下
    lid_closure: float = 0.0   # 眼皮闭合度，0=全开，1=全闭

    def blend(self, other: "Pose", k: float) -> "Pose":
        """线性混合，k=0 用 self，k=1 用 other"""
        k = max(0.0, min(1.0, k))  # clamp
        return Pose(
            neck_pan=self.neck_pan * (1-k) + other.neck_pan * k,
            neck_tilt=self.neck_tilt * (1-k) + other.neck_tilt * k,
            neck_roll=self.neck_roll * (1-k) + other.neck_roll * k,
            eye_pan=self.eye_pan * (1-k) + other.eye_pan * k,
            eye_tilt=self.eye_tilt * (1-k) + other.eye_tilt * k,
            lid_closure=self.lid_closure * (1-k) + other.lid_closure * k,
        )

    def apply_offset(self, offset: "Offset") -> "Pose":
        """按channels和mode叠加单个偏移"""
        result = Pose(**self.__dict__)  # 拷贝
        fade = max(0.0, min(1.0, offset.fade))  # clamp
        
        if not offset.channels:
            return result  # 空集合，不生效
        
        for ch in offset.channels:
            # 逻辑通道名 → 实际数值
            if ch == "lid_closure":
                val = offset.lid_delta
            else:
                val = getattr(offset, ch)
            
            old_val = getattr(result, ch)
            
            # 按mode执行，两种模式都应用fade插值
            if offset.mode == "add":
                # add：在旧值基础上叠加
                setattr(result, ch, old_val + val * fade)
            else:  # override
                # override：fade=0保持旧值，fade=1完全覆盖
                setattr(result, ch, old_val + (val - old_val) * fade)
        
        return result

    def apply_offsets(self, offsets: list) -> "Pose":
        """按规则叠加多个偏移：先所有add（按priority低→高），最后所有override（按priority低→高）"""
        result = self
        
        # 先应用add类
        add_offsets = [o for o in offsets if o.mode == "add"]
        for offset in sorted(add_offsets, key=lambda o: o.priority):
            result = result.apply_offset(offset)
        
        # 最后应用override类（高优先级override覆盖低优先级）
        override_offsets = [o for o in offsets if o.mode == "override"]
        for offset in sorted(override_offsets, key=lambda o: o.priority):
            result = result.apply_offset(offset)
        
        return result
```

### 4.2 Offset（相对偏移）

```python
@dataclass
class Offset:
    # 数值字段
    neck_pan: float = 0.0      # 脖子pan偏移，度
    neck_tilt: float = 0.0     # 脖子tilt偏移，度
    neck_roll: float = 0.0     # 脖子roll偏移，度
    eye_pan: float = 0.0       # 眼睛pan偏移，度
    eye_tilt: float = 0.0      # 眼睛tilt偏移，度
    lid_delta: float = 0.0     # 闭眼增量，正=更闭
    
    # 合成配置
    channels: set = None        # 生效通道名（逻辑名），空集合=不生效，schema要求必须非空
    mode: str = "add"           # "add" 叠加 / "override" 覆盖
    priority: int = 1           # 优先级，数字越大越优先
    fade: float = 1.0           # 淡入淡出系数，0~1
```

**channels语义：**
- 空集合：不生效
- 非空：只作用于列出的通道
- JSON schema 校验要求 channels 必须非空，不允许空动作

**通道名映射：**

| 逻辑通道名 (channels里写的) | Offset数值字段 | Pose数值字段 |
|---------------------------|---------------|-------------|
| `neck_pan` | `neck_pan` | `neck_pan` |
| `neck_tilt` | `neck_tilt` | `neck_tilt` |
| `neck_roll` | `neck_roll` | `neck_roll` |
| `eye_pan` | `eye_pan` | `eye_pan` |
| `eye_tilt` | `eye_tilt` | `eye_tilt` |
| `lid_closure` | `lid_delta` | `lid_closure` |

**注意：** lid的逻辑通道名是`lid_closure`，Offset的数值字段是`lid_delta`（增量），Pose的数值字段是`lid_closure`（绝对值）。

### 4.3 macro / micro 拆分

**不是独立子结构，而是通道分类 + 显式合成函数：**

```python
# macro通道：主姿态，决定头往哪看、眼睛往哪看
MACRO_CHANNELS = {"neck_pan", "neck_tilt", "eye_pan", "eye_tilt"}

# micro通道：小偏移，呼吸/眨眼/微晃，不改变注视目标
MICRO_CHANNELS = {"neck_roll", "neck_tilt", "eye_tilt", "lid_closure"}
```

**注意：** neck_tilt 和 eye_tilt 同时属于 macro 和 micro。
- macro 部分：大的姿态变化（比如低头看）
- micro 部分：小的呼吸偏移（比如呼吸引起的 ±2° tilt）

### 4.4 合成规则

**定义：**
- `tracking_pose`：Geometry输出的跟踪姿态，**只包含macro通道**（头往哪看、眼睛往哪看），不包含micro小偏移
- `procedural.sample_micro()`：基础运动生成器输出的micro小偏移（呼吸、微晃、眨眼基线）

```python
def add_micro_offset(base: Pose, micro: Pose) -> Pose:
    """把micro小偏移叠加到base姿态上，只加micro通道"""
    return Pose(
        neck_pan=base.neck_pan,  # macro通道，不加
        neck_tilt=base.neck_tilt + micro.neck_tilt,  # micro部分
        neck_roll=base.neck_roll + micro.neck_roll,
        eye_pan=base.eye_pan,  # macro通道，不加
        eye_tilt=base.eye_tilt + micro.eye_tilt,  # micro部分
        lid_closure=base.lid_closure + micro.lid_closure,
    )
```

两条路径：
```
有人路径：
  tracking_pose（只macro：头往哪看、眼睛往哪看）
  + add_micro(procedural.sample_micro())  # 叠加呼吸/微晃
  + social OffsetClips

无人路径：
  procedural.sample()（完整macro + micro）
  + idle OffsetClips
```

**关键：**
- 有人时，tracking的macro完整保留（头往哪看、眼睛往哪看）
- procedural只叠加micro通道的小偏移（呼吸、微晃），不改变注视目标
- micro只加一次，不会重复
- 眼睛始终跟人脸，不会丢跟踪

### 4.5 lid 字段统一映射

| 旧命名 | 含义 | 新命名 | 转换 |
|--------|------|--------|------|
| `lid_off` | 0=不闭，1=全闭 | `lid_delta` | 直接映射，正=更闭 |
| `lid_up` | 1=全睁，0=全闭 | `lid_closure` | `lid_closure = 1 - lid_up` |
| BlinkMachine输出 | `lid_up` (1=全睁，0=全闭) | `lid_delta` | `lid_delta = 1 - blink_lid_up` |

**JointMapper映射：**
```python
lid_up_L = (1.0 - lid_closure) * 90.0   # 0~1 → 0~90度
lid_up_R = (1.0 - lid_closure) * 90.0
```

---

## 五、最终分层架构

```
FamilyScheduler
  → 选当前族、族时长、过渡时长（从timing.json读参数）
       ↓
BaseMotion
  → 当前族的完整姿态（macro + micro）
       ↓
TransitionMixer
  → 旧/新 BaseMotion 混合（眼先/头延迟）
  → 旧实例保留到过渡结束才销毁
       ↓
ActionManager
  → PoseClip (Boot/IdleSequence)
  → OffsetClip (STRETCH/点头/歪头等)
       ↓
PoseSelector:
  ┌─ PoseClip active?
  │   ├─ 是 → 用PoseClip.pose + allow_offset_clips
  │   └─ 否 → 继续
  ├─ 有人脸?
  │   ├─ 是 → tracking_pose + add_micro(procedural.micro) + social OffsetClips
  │   └─ 否 → procedural.macro + procedural.micro + idle OffsetClips
       ↓
DynamicsLayer
  → BlinkMachine 眨眼叠加
  → lid envelope 眼皮包络
  → clamp 到 0~1
       ↓
Safety (Driver限速 + SoftLimiter, actual空间，唯一一次)
       ↓
JointMapper (C3b逆变换 + 量化 + cmd限位，不重复SoftLimiter)
       ↓
codec → 输出
```

---

## 六、三类生成器定义

### 6.1 BaseMotion（基础运动）
- **输出**：Pose（绝对姿态，macro + micro）
- **生命周期**：
  - 进入族时：创建新实例
  - 过渡中：旧实例保留到过渡结束
  - 过渡结束：销毁旧实例
  - 过渡被打断：保留最新旧实例快照，重新计时
- **特点**：持续运行，有内部状态

```python
class BaseMotion:
    def __init__(self, params, rng):
        self.params = params
        self.rng = rng
        self._state = {}
    
    def sample(self, dims, t, dt) -> Pose:
        """返回完整姿态（macro + micro）"""
        ...
    
    def sample_micro(self, dims, t, dt) -> Pose:
        """只返回micro小偏移（只填micro通道，其余为0）
        有人路径时用这个，避免覆盖tracking的macro
        """
        ...
    
    def reset(self):
        """进入族时重置状态"""
        self._state = {}
```

### 6.2 PoseClip（绝对姿态剪辑）
- **输出**：Pose（绝对姿态，覆盖式）
- **例子**：Boot开机编排、IdleSequence序列
- **特点**：高优先级，覆盖一切

```python
class PoseClip:
    def __init__(self, config, rng):
        self.config = config
        self.rng = rng
        self.elapsed = 0.0
        self._fade_out = 1.0  # 1.0=正常，0.0=完全淡出
        self._interrupting = False
    
    def start(self):
        """开始播放"""
        self.elapsed = 0.0
        self._fade_out = 1.0
        self._interrupting = False
    
    def update(self, dt):
        """推进时间，由ActionManager调用"""
        self.elapsed += dt
        if self._interrupting:
            self._fade_out = max(0.0, self._fade_out - dt / 0.2)  # clamp
    
    def sample(self, dims) -> Pose:
        """根据elapsed返回原始姿态（不含fade），用内部elapsed"""
        return self._sample_raw(dims, self.elapsed)
    
    def interrupt(self):
        """被打断时调用，开始200ms crossfade"""
        self._interrupting = True
    
    def is_finished(self) -> bool:
        return self.elapsed >= self.config.duration or self._fade_out <= 0.0
    
    @property
    def is_interrupting(self) -> bool:
        """是否正在crossfade退出"""
        return self._interrupting
    
    @property
    def fade_out(self) -> float:
        """淡出系数，1.0=完整，0.0=消失"""
        return self._fade_out
    
    @property
    def interruptible(self) -> bool:
        return self.config.interruptible  # Boot=false, IdleSequence=true
    
    @property
    def allow_offset_clips(self) -> bool:
        return self.config.allow_offset_clips  # 默认false
    
    @property
    def allow_blink(self) -> bool:
        return self.config.allow_blink  # Boot=false, IdleSequence=true
```

**PoseSelector里的crossfade实现：**
```python
if pose_clip.is_interrupting:
    clip_pose = pose_clip.sample()
    track_pose = geometry.tracking_pose()  # 只macro
    
    # 给tracking侧加上procedural micro（和正常有人路径一致）
    track_pose = add_micro_offset(track_pose, procedural.sample_micro())
    
    # 先blend两边的base pose
    k = 1.0 - pose_clip.fade_out  # 0=全用clip，1=全用tracking
    k = max(0.0, min(1.0, k))
    final_pose = clip_pose.blend(track_pose, k)
    
    # 最后统一应用offsets，保证crossfade全过程中offsets连续
    if pose_clip.allow_offset_clips:
        final_pose = final_pose.apply_offsets(active_offsets)
```

**注意：**
- offsets在crossfade全过程中保持不变，不会先消失再淡入
- procedural micro只在tracking侧加，clip侧是纯编排
- 这样打断时不会有offset闪动

### 6.3 OffsetClip（偏移剪辑）
- **输出**：Offset（相对偏移，叠加式）
- **例子**：STRETCH、慢点头、歪头
- **特点**：叠加在基础运动上

```python
class OffsetClip:
    def __init__(self, config, rng):
        self.config = config
        self.rng = rng
        self.elapsed = 0.0
    
    def start(self):
        self.elapsed = 0.0
    
    def update(self, dt):
        self.elapsed += dt
    
    def sample(self, dims) -> Offset:
        """根据elapsed返回偏移量，用内部elapsed"""
        ...
    
    def is_finished(self) -> bool:
        return self.elapsed >= self.config.duration
```

**统一约定：** PoseClip 和 OffsetClip 的 `is_finished` 都是方法，ActionManager 统一调用 `clip.is_finished()`。

---

## 七、ActionManager 与触发规则

### 7.1 触发规则 Schema

每个 OffsetClip 带触发配置：

```json
{
  "id": "stretch_tired",
  "generator": "keyframe_clip",
  "config": { ... },
  "trigger": {
    "families": ["IDLE", "RECOVERING"],
    "min_fatigue": 0.3,
    "weight": 1.0,
    "probability": 0.8,
    "cooldown_sec": 60.0,
    "min_interval_sec": 30.0,
    "max_interval_sec": 120.0
  },
  "mix": {
    "mode": "add",
    "channels": ["neck_roll", "neck_tilt", "lid_closure"],
    "priority": 1,
    "max_instances": 1,
    "overflow": "drop"
  }
}
```

### 7.2 两个并发概念，分开定义

| 概念 | 位置 | 含义 |
|------|------|------|
| `mix.max_instances` | 每个动作自己的config | 这个动作最多同时跑几个实例 |
| `channel_capacity` | action_manager全局配置 | 每个通道全局最多几个add动作 |

**channel_capacity 配置：**
```json
"channel_capacity": {
  "neck_roll": 1,
  "lid_closure": 1,
  "neck_pan": 2,
  "neck_tilt": 2,
  "eye_pan": 1,
  "eye_tilt": 1
}
```

### 7.3 触发选择逻辑

1. 当前族匹配 `trigger.families`
2. 检查 `min_fatigue` 等条件
3. 检查 `cooldown_sec`，刚触发过就冷却
4. 检查这个动作自己的 `max_instances`，已经跑满就跳过
5. 检查通道容量，通道满了就按overflow处理
6. 符合条件的动作，按 `weight` 加权随机选
7. 到点了，`probability` 概率真触发，否则跳过

### 7.4 冲突规则

| 情况 | 规则 |
|------|------|
| 同通道多个 override | 只保留 priority 最高的，低优先级 fade_out |
| 同通道多个 add | 最多 channel_capacity 个，超出的按 overflow 处理 |
| overflow: "drop" | 超出的直接丢弃，不排队 |
| overflow: "queue" | 超出的按 priority 排队，前一个结束后再启动 |
| overflow: "interrupt" | 新动作打断旧动作，旧的 fade_out |
| 新 override 触发 | 同通道旧 override 立即 fade_out |
| 族切换 | 当前 OffsetClip 默认 fade_out 200ms |
| PoseClip active | 按 `allow_offset_clips` 配置决定是否叠加 |

### 7.5 PoseClip 启动时机

```json
{
  "id": "idle_sequence",
  "generator": "idle_sequence_clip",
  "config": { ... },
  "start_condition": {
    "type": "idle_after_sec",
    "seconds": 30
  },
  "interruptible": true,
  "allow_offset_clips": false,
  "allow_blink": true
}
```

- **Boot**：系统启动时自动启动，不可打断
- **IdleSequence**：无人状态持续 `start_condition.seconds` 秒后自动启动
- 启动条件由 FamilyScheduler 控制

### 7.6 allow_offset_clips 合成规则

```
PoseClip active + allow_offset_clips = true:
  base = pose_clip.sample()  # 绝对姿态
  base = base.apply_offsets(active_offsets)  # 按优先级叠加
  # 然后进DynamicsLayer
```

OffsetClip 正常按mix规则叠加到PoseClip的绝对姿态上，所有通道都生效。

---

## 八、6个关键决策点（已定稿）

### 决策1：Geometry顺序 + macro/micro拆分
**结论：方案B + macro/micro拆分**

- **macro 通道**：neck_pan, neck_tilt, eye_pan, eye_tilt（主姿态 + 眼睛目标）
- **micro 通道**：neck_roll, neck_tilt, eye_tilt, lid_closure（小偏移）

**tracking_pose 定义：** 只包含macro通道，不包含micro小偏移。

两条路径：
```
有人路径：tracking_pose + add_micro(procedural.sample_micro()) + social OffsetClips
无人路径：procedural.sample() + idle OffsetClips
```

**关键：**
- 有人时，tracking的macro完整保留（头往哪看、眼睛往哪看）
- procedural只叠加micro通道的小偏移（呼吸、微晃），不改变注视目标
- micro只加一次，不会重复
- 眼睛始终跟人脸，不会丢跟踪

---

### 决策2：PoseSelector优先级
**结论：PoseClip > tracking > procedural**

PoseClip分两种：
- **Boot型**：`interruptible=false`，不可被人脸打断
- **Sequence型**：`interruptible=true`，用户一出现就退出

```
PoseSelector:
  if PoseClip active:
      if interruptible and person:
          PoseClip.interrupt() → 200ms crossfade blend 到 tracking
      else:
          base = PoseClip.pose
          if allow_offset_clips:
              base = base.apply_offsets(active_offsets)
  elif person:
      tracking_pose + add_micro(procedural.sample_micro()) + social OffsetClips
  else:
      procedural.sample() + idle OffsetClips
```

---

### 决策3：insight事件出口
**结论：方法调用 + controller暂存**

生成器提供方法：
```python
class ThinkingGenerator(BaseMotion):
    def take_insight(self) -> bool:
        """返回是否有新的insight事件，并清除标记"""
        ...
```

PresenceController层做latch暂存：
```python
# PresenceController
if self.current_thinking_gen:
    if self.current_thinking_gen.take_insight():
        self._insight = True

def take_insight(self):
    if self._insight:
        self._insight = False
        return True
    return False
```

---

### 决策4：PoseClip + OffsetClip 叠加规则
**结论：默认不叠加，做成可配置**

每个PoseClip带配置：
```json
"allow_offset_clips": false
```

- Boot：`false`，完全纯编排
- IdleSequence：默认 `false`，以后想加呼吸/眨眼可以打开
- 普通BaseMotion不是PoseClip，OffsetClip正常叠加

`allow_offset_clips=true` 时：
- OffsetClip按正常mix规则叠加到PoseClip的绝对姿态上
- 所有通道都生效，不只是lid

---

### 决策5：时间参数独立配置
**结论：独立成 `config/timing.json`，值与当前代码对齐**

```json
{
  "version": 1,
  "family_max_s": {
    "SOCIAL_GAZE": 180,
    "SOCIAL_AVOID": 30,
    "LISTENING": 300,
    "SCANNING": 60,
    "SEEKING": 180,
    "THINKING": 400,
    "RECOVERING": 800,
    "IDLE": 900
  },
  "timeline": {
    "seeking": [90, 150],
    "thinking": [240, 360],
    "dozing": [480, 720],
    "backtrack_probability": 0.2
  },
  "transitions": {
    "IDLE->SCANNING": 0.6,
    "IDLE->SEEKING": 0.4,
    "IDLE->THINKING": 1.0,
    "IDLE->SOCIAL_GAZE": 0.6,
    "SCANNING->SEEKING": 0.6,
    "SCANNING->SOCIAL_GAZE": 0.6,
    "SCANNING->IDLE": 0.8,
    "SEEKING->SCANNING": 0.6,
    "SEEKING->THINKING": 1.5,
    "SEEKING->SOCIAL_GAZE": 0.6,
    "SEEKING->IDLE": 0.8,
    "THINKING->SCANNING": 1.0,
    "THINKING->SEEKING": 1.0,
    "THINKING->RECOVERING": 2.5,
    "THINKING->SOCIAL_GAZE": 0.6,
    "THINKING->IDLE": 1.0,
    "RECOVERING->THINKING": 1.5,
    "RECOVERING->SEEKING": 1.5,
    "RECOVERING->IDLE": 1.2,
    "RECOVERING->SOCIAL_GAZE": 1.0,
    "RECOVERING->LISTENING": 1.0,
    "RECOVERING->SOCIAL_AVOID": 1.0,
    "SOCIAL_GAZE->LISTENING": 0.5,
    "SOCIAL_GAZE->SOCIAL_AVOID": 0.4,
    "SOCIAL_GAZE->IDLE": 0.6,
    "SOCIAL_AVOID->SOCIAL_GAZE": 0.6,
    "SOCIAL_AVOID->IDLE": 0.6,
    "LISTENING->SOCIAL_GAZE": 0.6,
    "LISTENING->IDLE": 0.6
  },
  "default_transition": 1.0
}
```

**规则：**
- JSON里显式列出的过渡，用JSON的值
- 未列出的过渡，用 `default_transition` 值
- 所有值与当前代码完全对齐

---

### 决策6：Safety限幅唯一位置
**结论：唯一位置 + actual空间 + C3b之前**

管线顺序：
```
PoseSelector输出 (actual角度)
  → DynamicsLayer（眨眼、lid包络，actual空间）
  → Safety: Driver限速 + SoftLimiter（actual空间，唯一一次）
  → JointMapper: C3b逆变换 → cmd角度
  → codec
```

规则：
- Driver和SoftLimiter只在Safety层做一次，在actual空间
- C3a限位对应actual角度，保证cmd不越界
- JointMapper做C3b + 量化 + cmd范围机械硬限位
- PoseClip/Boot也必须过Safety，不绕过

**lid映射：**
```python
lid_up_L = (1.0 - lid_closure) * 90.0   # 0~1 → 0~90度
lid_up_R = (1.0 - lid_closure) * 90.0
```

---

## 九、DynamicsLayer（眨眼层）

### 位置
在 PoseSelector 之后，Safety 之前。

### 职责
- BlinkMachine 眨眼事件叠加
- lid envelope 眼皮平滑包络
- 和 BaseMotion / PoseClip 的 lid 合成

### 合成规则
```python
# base_pose 是 PoseSelector 已经处理好的最终选中姿态，包括：
# - PoseClip（含crossfade混合）
# - allow_offset_clips 叠加的offsets
# - tracking + procedural.micro
# - idle/social OffsetClips
# DynamicsLayer 只负责加 blink，不重复加 offset lid

final_lid = base_pose.lid_closure

# BlinkMachine叠加（注意方向和时间单位）
if not pose_clip or pose_clip.allow_blink:
    ts_ms = t_sec * 1000.0  # 秒转毫秒
    blink_lid_up = blink.update(ts_ms)  # BlinkMachine输出lid_up，1=全睁，0=全闭
    blink_lid_delta = 1.0 - blink_lid_up  # 转换成lid_delta，正=更闭
    final_lid += blink_lid_delta

# 裁剪
final_lid = clamp(final_lid, 0.0, 1.0)
```

**关键：**
- OffsetClip的lid_delta已经在PoseSelector阶段通过`apply_offsets`合进base_pose了
- DynamicsLayer只负责blink，不重复加offset lid
- PoseClip的crossfade已经包含了lid的过渡，不会眼皮跳
- allow_offset_clips的lid也已经合进base_pose了，不会被覆盖

---

## 十、配置文件结构

```
config/
├── timing.json              ← 时间轴/族时长/过渡/回溯概率
├── action_manager.json      ← 动作管理器配置（channel_capacity等）
└── families/
    ├── idle.json            ← IDLE族参数
    ├── seeking.json
    ├── thinking.json
    ├── listening.json
    ├── scanning.json
    ├── social_gaze.json
    ├── social_avoid.json
    └── recovering.json

actions/
├── boot/
│   └── boot_choreography.json   ← Boot编排（PoseClip, interruptible=false）
├── idle_sequence/
│   └── idle_sequence.json        ← IdleSequence（PoseClip, interruptible=true）
└── interludes/
    ├── idle/                     ← 无人时IDLE族的插曲
    │   ├── stretch_tired.json
    │   ├── slow_nod.json
    │   └── ...
    ├── thinking/                 ← 无人时THINKING族的插曲
    │   └── look_up_recall.json
    ├── social/                   ← 有人时社交类的插曲
    │   └── head_cock.json
    └── exploring/                ← 探索类的插曲
        └── quick_glance.json
```

---

## 十一、注册表设计

### 两类注册表
```python
BASE_GENERATORS = {}    # 基础运动生成器
ACTION_GENERATORS = {}  # 动作剪辑生成器（PoseClip + OffsetClip）

def register_base(name):
    def decorator(cls):
        BASE_GENERATORS[name] = cls
        return cls
    return decorator

def register_action(name):
    def decorator(cls):
        ACTION_GENERATORS[name] = cls
        return cls
    return decorator
```

### 注册规则
- `generators/__init__.py` 显式 import 所有生成器
- 重复ID直接报错
- 编辑器下拉只读注册表，不能填未注册的ID
- 启动时打印已注册ID列表
- 加新生成器 = 写文件 + 在 `__init__.py` 加一行 import（不改主流程）

---

## 十二、ConfigStore 与热重载

### 跨进程问题
- editor server 和 P2 是两个独立进程
- ActionLibrary 不能跨进程共享内存
- 热重载需要：文件 mtime/version 轮询 或 IPC reload

### 不可变快照
```python
class ConfigStore:
    def __init__(self, config_dir):
        self._config_dir = config_dir
        self._snapshot = None
        self._version = 0
        # 初始化时立即加载，失败直接抛异常
        self.reload()
        if self._snapshot is None:
            raise RuntimeError("initial config load failed, snapshot is None")
    
    def get_snapshot(self):
        """返回不可变快照，运行时持有引用，不直接改"""
        if self._snapshot is None:
            raise RuntimeError("config not loaded")
        return self._snapshot
    
    def reload(self):
        """重新加载所有配置，生成新快照"""
        try:
            # 1. 读所有JSON
            # 2. 校验schema
            # 3. 构建新快照对象
            new_snapshot = self._build_snapshot()
            
            # 校验：构建返回None也当失败处理，不清空旧快照
            if new_snapshot is None:
                raise RuntimeError("snapshot build returned None")
            
            # 4. 原子替换（对象引用替换，不是逐字段修改）
            self._snapshot = new_snapshot
            self._version += 1
        except Exception as e:
            # 失败不影响当前运行，继续用旧配置
            # （初始化时第一次失败，会直接抛异常）
            log.error(f"Config reload failed, keeping old config: {e}")
```

### 规则
- 运行时持有快照引用，不直接改配置字典
- 编辑器改配置后，写文件 + 更新version
- P2 每N秒检查mtime，有变化就reload
- 运行时reload失败不影响当前运行，继续用旧配置
- 初始化时reload失败直接抛异常，不允许带病启动

---

## 十三、落地顺序

| 优先级 | 做什么 | 产出 | 风险 |
|--------|--------|------|------|
| **P0** | ConfigStore + JSON Schema + KeyframeClip + 注册表骨架 | 数据格式定下来，5个插曲抽成JSON | 低 |
| **P1** | BaseMotion包装 + 状态归位 + macro/micro拆分 | gen函数包成类，状态从dims移出来 | 中 |
| **P2** | TransitionMixer + PoseClip支持 + PoseSelector | 过渡、Boot、IdleSequence接入 | 中 |
| **P3** | ActionManager + InterludeManager + 触发规则数据化 | 插曲调度 | 中 |
| **P4** | WaveSource（正弦/三角/OU可选） | 运动模式可切换 | 高 |
| **P5** | 编辑器完善 + 热重载 | 可视化调参 | 高 |

---

## 十四、关键边界总结

### 数据负责什么
- 可编辑的值：幅度、频率、周期、概率、冷却时间
- 动作片段：keyframes、时长、通道、触发条件
- 时间调度：族时长、过渡时长、回溯概率

### 代码负责什么
- 状态机逻辑：族切换、过渡、pause
- 数学公式：正弦波、OU过程、lerp位置
- 安全：Driver限速、SoftLimiter限幅
- 几何：跟踪、leave/fade、saccade
- 顺序和耦合：眼先动头后随、通道叠加顺序

---

## 十五、编辑器规划

### 三个编辑页面
1. **插曲动作编辑器**：keyframe表格、曲线预览、触发条件配置
2. **基础运动编辑器**：参数滑块分组、随机模式选择、实时预览
3. **时间节奏编辑器**：族时长、过渡时长、回溯概率

### 预览规则
- 克隆生成器实例，不碰运行中的
- 使用独立RNG，时间从0开始
- 预览结束丢弃实例
- 曲线和真机走同一sample()

---

## 十六、实施注意事项（P0/P2顺手补）

### 1. PoseClip结束时offsets的恢复
- `allow_offset_clips=false` 的 PoseClip 结束时，active offsets 会瞬间恢复
- 建议：PoseClip退出后，给活跃offsets加200ms fade-in，避免跳变
- 或者：明确接受这个行为（Boot/IdleSequence默认false，影响不大）

### 2. 时间源区分
| 时间源 | 用途 | 说明 |
|--------|------|------|
| 全局单调时间 t_sec | DynamicsLayer/BlinkMachine | 族切换不重置，眨眼节奏连续 |
| 族内时间 family_t | BaseMotion.sample(t) | 进入族时从0开始 |

**注意：** BlinkMachine必须用全局时间，否则族切换会导致眨眼节奏跳变。

### 3. channels的schema强制
- JSON schema: `channels` 设为 `minItems: 1`
- 代码加载时校验：channels为空的动作直接拒绝加载，报错
- 避免配置写错导致动作静默不生效
