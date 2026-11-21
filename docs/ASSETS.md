# 素材与许可说明

本仓库只放代码、文档和可独立复现的数值结果。下面这些文件**有意未入库**，
需要按自己的许可自行准备。

| 类别 | 位置（本地） | 数量/体积 | 未入库原因 | 怎么用 |
|---|---|---|---|---|
| 推理权重 | `models/*.onnx`、`face_landmarker.task` | 3 个文件，约 250 MB | 体积超 GitHub 单文件 100 MB 硬限；各有权重许可 | 见 `models/README.md` |
| VRM 数字人 | `3d/*.vrm` | 4 个，约 27 MB | 体积大，且角色形象另有许可，不入库 | 见下节「演示用的 VRM 模型」 |
| 测试视频 | `test_videos/*.mp4` | 7 个，约 53 MB | 含真人面部，肖像权 | 见 `test_videos/README.md` |
| 运行日志 | `runtime_log.csv`、`boot_analysis.csv` | — | 机器相关的运行产物 | 跑起来会自动生成 |

## 如果你要恢复完整演示

```bash
# 1) 权重：按 models/README.md 下载三个文件
# 2) 模型：把任意 .vrm 放进 3d/nova_model.vrm
# 3) 视频：把自己的 mp4 放进 test_videos/
```

## 演示用的 VRM 模型

README 顶部的动画用的是 VTubeMe 免费库里的 **Nova**，也即 `3d/index.html` 的默认模型
`nova_model.vrm`。渲染画面可以随本仓库分发，模型文件本身不在仓库里。

| 项目 | 说明 |
|---|---|
| 来源 | https://vtubeme.com/free-vrm-avatars（Nova / Kai / Sky / Ember 四款） |
| 许可 | CC BY 4.0，个人与商用皆可，允许修改与再分发 |
| 我们的义务 | 署名 VTubeMe 并链回 vtubeme.com（已写在 README 图片下方） |

本地另有 `seed-san.vrm`（作者 VirtualCast, Inc.，VRM 1.0 元数据为 `creditNotation: required`、
`allowRedistribution: true`），其官方条款可能另有附加说明，未经核实，故不用于公开素材。

## 已入库的第三方代码

`3d/vendor/` 里是 three.js 0.160.0 与 @pixiv/three-vrm 的部分文件（约 2.3 MB），原样拷入、
未做修改，用于离线渲染。两者均为 MIT，版本与来源逐条列在 `3d/vendor/README.md`。
这是本仓库唯一的第三方源码；其余依赖都由 `requirements.txt` 交给包管理器安装。

## 关于专利

`docs/` 下曾有专利交底书草稿，**已从本仓库移除**：公开披露会构成现有技术，
影响后续申请的新颖性。专利申请日之后再考虑是否公开。
