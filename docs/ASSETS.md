# 素材与许可说明

本仓库只放代码、文档和可独立复现的数值结果。下面这些文件**有意未入库**，
需要按自己的许可自行准备。

| 类别 | 位置（本地） | 数量/体积 | 未入库原因 | 怎么用 |
|---|---|---|---|---|
| 推理权重 | `models/*.onnx`、`face_landmarker.task` | 3 个文件，约 250 MB | 体积超 GitHub 单文件 100 MB 硬限；各有权重许可 | 见 `models/README.md` |
| VRM 数字人 | `3d/*.vrm` | 4 个，约 27 MB | 第三方角色模型，多数禁止再分发 | 自备任意 `.vrm` 放入 `3d/`，默认读 `nova_model.vrm`，可用 `?model=xxx.vrm` 指定 |
| 测试视频 | `test_videos/*.mp4` | 7 个，约 53 MB | 含真人面部，肖像权 | 见 `test_videos/README.md` |
| 运行日志 | `runtime_log.csv`、`boot_analysis.csv` | — | 机器相关的运行产物 | 跑起来会自动生成 |

## 如果你要恢复完整演示

```bash
# 1) 权重：按 models/README.md 下载三个文件
# 2) 模型：把任意 .vrm 放进 3d/nova_model.vrm
# 3) 视频：把自己的 mp4 放进 test_videos/
```

## 已入库的第三方代码

`3d/vendor/` 里是 three.js 0.160.0 与 @pixiv/three-vrm 的部分文件（约 2.3 MB），原样拷入、
未做修改，用于离线渲染。两者均为 MIT，版本与来源逐条列在 `3d/vendor/README.md`。
这是本仓库唯一的第三方源码；其余依赖都由 `requirements.txt` 交给包管理器安装。

## 关于专利

`docs/` 下曾有专利交底书草稿，**已从本仓库移除**：公开披露会构成现有技术，
影响后续申请的新颖性。专利申请日之后再考虑是否公开。
