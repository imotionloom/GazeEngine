# 模型权重（不入库）

仓库不含模型文件。三个文件缺任意一个，`PcPerception` 构造时就会失败（`models/*` 已被
`.gitignore` 忽略，不要提交进来）。

| 文件 | 大小 | 用途 | 下载 |
|---|---|---|---|
| `face_detection_yunet_2023mar.onnx` | 232,589 B | YuNet 人脸检测（BSD-3-Clause） | <https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx> |
| `face_landmarker.task` | ~3.6 MB | MediaPipe 478 关键点（Apache-2.0） | <https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task> |
| `arcfaceresnet100-8.onnx` | 261,036,388 B | ArcFace 512 维身份特征 | <https://github.com/onnx/models/raw/main/validated/vision/body_analysis/arcface/model/arcfaceresnet100-8.onnx> |

YuNet 与 ArcFace 的字节数是实测值（`curl -I` 的 `content-length`），ArcFace 约合 249 MiB；
MediaPipe 那个大小取自官方文档，为近似值。

一次拉齐（Git Bash / WSL；Windows 上把 `curl -L` 换成 `curl.exe -L` 也行）：

```bash
cd models
curl -L -O https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx
curl -L -O https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task
curl -L -O https://github.com/onnx/models/raw/main/validated/vision/body_analysis/arcface/model/arcfaceresnet100-8.onnx
```

目录结构：

```
models/
├── README.md                          ← 本文件
├── face_detection_yunet_2023mar.onnx
├── face_landmarker.task
└── arcfaceresnet100-8.onnx
```

自检（权重就位后）：`python p1_perception/embedder.py` 会喂一张随机噪声图，正常输出
`embedding 维度=512  L2范数=1.000`、同一张图两次余弦 ≈ 1.0。这一步只验证权重能被
onnxruntime 读进来，跟摄像头无关；权重缺失时会直接报 `NoSuchFile`，看到这个说明文件名或
路径不对。核对 Python 侧依赖版本用 `python p1_perception/_env_check.py`（它还会打印
insightface 的版本，没装就跳过它）。

## 关于 ArcFace 权重（重要）

`arcfaceresnet100-8.onnx` 是 **ONNX Model Zoo** 里的 LResNet100E-IR（ArcFace loss，
输入 1×3×112×112，输出 512 维；`p1_perception/embedder.py` 按这个接口写死）。它不是
InsightFace `buffalo_l` 里的文件——那套包里的人脸特征是 `w600k_r50.onnx`，文件名和维度
都不同，不要混用。

许可上有个容易踩的坑：ONNX Model Zoo 的模型卡标注 Apache-2.0，但这套权重训练自 MS1M
数据集，而 InsightFace 对其训练数据与由此得到的权重声明**仅限非商业研究用途**。本项目
以 MIT 发布、不分发该权重，商用前请自行确认上游条款，或换成明确可商用的特征模型
（接口只有 `ArcFaceEmbedder.embed` 一个方法，替换成本很低）。

YuNet（BSD-3-Clause）与 MediaPipe FaceLandmarker（Apache-2.0）本身可用于商业场景。
