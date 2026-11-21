"""
P1 感知进程骨架（Ubuntu + RKNN 已通）
线程模型：
  采集线程   —— V4L2 抓帧
  NPU 线程   —— 检测/tracker → 4锚点 → 嘴部CNN
  CPU 线程   —— Kalman/窗口/身份（distance_kalman.py 已实现）
  发布线程   —— ZMQ 发 IF-1a(高频) / IF-1b(事件)
RKNN 模型加载处留 TODO，先用 mock 接口跑通流程。
"""
import time
import zmq
from .distance_kalman import DistanceKalman, SlidingWindowAverager, LoomingGate


class QualityGate:
    """M1.2 图像质量闸门：亮度均值 + Laplacian 方差判定 good/poor。"""
    BRIGHT_MIN = 20.0
    BRIGHT_MAX = 240.0
    BLUR_MIN = 50.0  # Laplacian 方差低于此视为模糊

    def evaluate(self, frame_gray) -> str:
        import cv2
        import numpy as np
        if frame_gray is None or frame_gray.size == 0:
            return "poor"
        bright = float(np.mean(frame_gray))
        if bright < self.BRIGHT_MIN or bright > self.BRIGHT_MAX:
            return "poor"
        blur = float(cv2.Laplacian(frame_gray, cv2.CV_64F).var())
        if blur < self.BLUR_MIN:
            return "poor"
        return "good"


class RknnModels:
    """M1.3/M1.4/M1.7：RKNN 推理封装（TODO 接入真实 .rknn）。"""
    def load(self, model_dir: str):
        # TODO: rknnlite.load(det.rknn / anchor478.rknn / mouth_cnn.rknn)
        pass

    def detect_tracks(self, frame):
        # TODO -> [(track_id, bbox, anchors4, conf, embedding)]
        return []


class PerceptionProcess:
    FPS = 30

    def __init__(self):
        self.gate = QualityGate()
        self.models = RknnModels()
        # 每个 track_id 一份 Kalman + 窗口 + LoomingGate；换 track_id 重建
        self.kf = {}
        self.win = {}
        self.looming = {}
        ctx = zmq.Context()
        self.pub_light = ctx.socket(zmq.PUB)   # IF-1a
        self.pub_meta = ctx.socket(zmq.PUB)   # IF-1b
        # 板端用 ipc:///tmp/gaze_if1a；Windows 自测用 tcp（与 p2/main.py env 对齐）
        import os
        if1a = os.environ.get("GAZE_IF1A", "tcp://127.0.0.1:5551")
        if1b = os.environ.get("GAZE_IF1B", "tcp://127.0.0.1:5552")
        self.pub_light.bind(if1a)
        self.pub_meta.bind(if1b)

    def on_frame(self, frame, ts_ms: int):
        q = self.gate.evaluate(frame)
        if q == "poor":
            return  # 差帧冻结，不发 IF-1a（下游 max_age 兜底）

        tracks = self.models.detect_tracks(frame)
        out_tracks = []
        for tid, bbox, anchors, conf, emb in tracks:
            if tid not in self.kf:
                self.kf[tid] = DistanceKalman()
                self.win[tid] = SlidingWindowAverager(1000)
                self.looming[tid] = LoomingGate(n_frames=4)
            d_raw = 1.2   # TODO: 测距 MLP
            st = self.kf[tid].update(d_raw, ts_ms)
            self.win[tid].add(st["est_m"], ts_ms)
            looming = self.looming[tid].feed(st["dot_mps"])
            out_tracks.append({
                "track_id": tid, "bbox": bbox, "anchors": anchors,
                "conf": conf, "distance": st, "looming": looming,
            })
        # 清理已消失 track 的 Kalman/win/looming，防内存泄漏
        alive = {t[0] for t in tracks}
        for dead in (set(self.kf) - alive):
            self.kf.pop(dead, None); self.win.pop(dead, None); self.looming.pop(dead, None)
        return out_tracks

    def run(self):
        print("P1 感知进程启动（mock 模式）")
        while True:
            ts = time.time_ns() // 1_000_000
            # frame = capture.read()  # TODO V4L2
            # self.on_frame(frame, ts)
            time.sleep(1 / self.FPS)


if __name__ == "__main__":
    PerceptionProcess().run()
