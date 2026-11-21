# 测试视频（不入库）

`test_videos/` 里的 mp4 是拍摄的真人画面，涉及肖像权，**不放进公开仓库**。
已保留的是 `trajectories/*.csv`——每帧的检出框、锚点、距离、注视角度等数值结果，
不含人像，可直接用于回放和回归对比。

## 想跑视频回放

1. 自备素材：任意包含人脸的 mp4，拖进本目录（文件名随意）。
2. 单条回放：

```bash
python test_videos/play_video.py 你的视频.mp4
```

3. 批量生成轨迹（会写到 `trajectories/<视频名>.csv`）：

```bash
python test_videos/test_videos.py
```

4. 网页回放：`test_videos/replay.html` 里的清单是按原视频文件名硬编码的，
换成自己的素材时需要同步改 `VIDEOS` 表里的文件名。

## 已有轨迹

`trajectories/` 下 17 份 CSV 对应原素材的不同场景（多人、单人、无人、短片段等），
逐条说明（帧数、时长、状态分布）见 [trajectories/INDEX.md](trajectories/INDEX.md)。
它们能单独回放，用来对比你的改动是否让轨迹发生漂移：

```bash
python test_videos/replay_server.py     # 起 WS + HTTP
```

然后浏览器打开 `replay.html`（CSV 能加载，缺 mp4 时只是没有画面底图）。
