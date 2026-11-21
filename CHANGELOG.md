# Changelog

Versions below are the internal milestones this repository was developed against — the code
arrived as one snapshot, so there are no dated public releases yet. From here on, entries
follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) with an `Unreleased` section
on top.

## Unreleased

- Public-release cleanup: test suite skips gracefully without the model weights, `mediapipe`
  is imported lazily, three.js and three-vrm are vendored so the viewer works offline,
  port table and "what is real vs. scaffold" added to the README, repository meta-files
  (SECURITY / CODE_OF_CONDUCT / issue forms) in place.

## 2.0 — 开机编排与眼颈方向修正

- `boot_choreography.py`：17 秒开机序列，27 关键帧 + smoothstep 缓动 + 0.2Hz 呼吸叠加
- 眼颈分配方向修正（C3b 逆变换，补偿 scale 误差而非 forward 相乘）
- 架构文档补齐（`docs/ARCHITECTURE.md`、`docs/BOOT_SEQUENCE.md`）

## 1.0 — 首个可用版本

- P1：YuNet 检测 + 478 关键点 + ArcFace 身份，Kalman 距离估计，ZMQ 发布
- P2：4 态注视 FSM、5 种注意力模式、Presence 8 族 × 6 维参数、眼颈滞回分配、扫视过冲、眨眼与呼吸
- P3：限速、软限位、11 通道舵机映射（`rt_loop.hpp` 仍为骨架）
- 工具：WS 桥、标定向导、动作编辑器、随机审计
- 测试：FSM / 几何 / 不变量 / 标定 / Presence / schema / 渲染冒烟
