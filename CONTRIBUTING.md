# Contributing

欢迎提 issue 和 PR。这个项目的核心是"看起来像人"，所以讨论改动时，
先说明它影响哪一层（P1 感知 / P2 决策 / P3 执行 / 渲染），再说明对人的观感有什么变化。

## 环境

```bash
conda create -n gaze python=3.10
conda activate gaze
pip install -r requirements.txt
```

模型权重按 `models/README.md` 准备。没有权重时 `tests/test_perception.py` 的 5 个用例会
自动 skip，其余用例不受影响。

## 提交前

```bash
pytest tests -q          # 83 项：无权重时 78 passed + 5 skipped（属预期）
node tests/js_smoke.mjs 3d/index.html   # 可选：渲染端冒烟
```

## 代码约定

- 新增可调参数一律进 `p2_decision/config.py`，不要散落在调用方。
- 时序相关的改动必须用 `GAZE_SEED` 固定随机种子，否则回归无法比对。
- 改 `geometry.py` / `presence.py` 的行为时，同步更新 `docs/ARCHITECTURE.md` 或对应设计文档。
- 中文注释可以保留；新增面向外部读者的文档建议用英文，或在中文文档里给一句英文摘要。

## 不要提交

模型权重、`.vrm`、含真人面部的视频、运行日志。`.gitignore` 已覆盖，提交前
`git status` 过一眼即可。

## PR 描述

写清三件事：改了什么、为什么（观感问题还是性能问题）、怎么验证的
（测试/截图/轨迹 CSV 前后对比都可以）。

## 贡献的许可

本项目以 MIT 发布（见 `LICENSE`），不要求签 CLA。提交 PR 即表示你同意该贡献按本仓库的
MIT 条款授权；请只在你有权提交的代码上提 PR（不要贴入第三方非 MIT 代码）。
版权人署名为笔名，处理方式见 `NOTICE`。
