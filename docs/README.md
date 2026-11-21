# Docs

The design notes are written in Chinese and were never meant to ship, so they are dense, use
project shorthand in places, and occasionally assume the original hardware. This page is the
map: what each document actually contains, in one English line.

| File | 内容 | In English |
|---|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | 三层数据流、FSM 4 状态、丢脸三段退出 | Layer-by-layer data flow, the 4-state gaze FSM, and the three-stage exit when a face is lost |
| [CALIBRATION_SPEC.md](CALIBRATION_SPEC.md) | C1–C7 标定流程与系数，`calibration/active.json` 契约 | The C1–C7 calibration procedure and coefficient contract, shared by real servos and the VRM mirror |
| [BOOT_SEQUENCE.md](BOOT_SEQUENCE.md) | 上电 17 秒确定性编排，27 个关键帧 | The deterministic 17 s power-on choreography (27 keyframes) that hands over to the live FSM |
| [THINKING_DESIGN.md](THINKING_DESIGN.md) | THINKING 的四外观、六元组契约、回神与歪头事件 | The THINKING micro-behaviour model: four appearances, the 6-tuple generator contract, "snap back" and head-tilt events |
| [action_architecture_design.md](action_architecture_design.md) | 动作库分层、调度器、眨眼链、族状态机（设计稿，未实施） | Action-library architecture: scheduler, blink chain, per-family state machine (design only, not implemented) |
| [IDLE_PRESET_PLAN.md](IDLE_PRESET_PLAN.md) | 无人时的 S1/S2/S3/IDLE 预设与过渡表 | The no-person-present preset set (S1/S2/S3A-C/IDLE) and the transition table back into the FSM |
| [ASSETS.md](ASSETS.md) | 哪些素材有意不入库、为什么 | What is deliberately excluded from the repo (weights, avatars, footage) and why |

Two things to know before reading:

- Numbers in these documents are the values that were tuned on the original rig. The code is
  the source of truth; if a table here disagrees with `p2_decision/config.py`, believe the code.
- Screenshots and plots referenced by the older notes live in this directory; anything that
  would have required redistributing third-party footage or avatar models was dropped (see
  [ASSETS.md](ASSETS.md)).

English translations of any of these are welcome as a PR — see `CONTRIBUTING.md`.
