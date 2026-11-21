"""感知层（pc_models.PcPerception）的回归测试。

背景：`process()` 的"有脸路径"此前没有任何测试覆盖，连续踩了三次同类崩溃：
  1. `bboxes` 在定义前被引用  → UnboundLocalError
  2. `tracks` 列表被补丁覆盖掉 → NameError
  3. `tracker.tracks` 里 key 是 "id" 却按 "track_id" 取 → KeyError
共同点：空帧测试能过，一出现人脸就崩。这里用"假检测器"把有脸路径跑通。

性能说明：不构造真实的 FaceLandmarker / ArcFace。
  · mediapipe 的 `FaceLandmarker.close()` 在本机要 ~42 秒，会把整个测试套拖慢 20 倍；
  · 这两个模型是第三方集成，不是本项目的逻辑，stub 掉不影响回归价值。
YuNet 保留真实实现（加载 ~0.5s，且 `det` 本来就会被替换成替身）。
"""
import os
import types

import numpy as np
import pytest

from p1_perception import pc_models as M

BLANK = np.full((480, 640, 3), 180, np.uint8)


class _FakeDet:
    """替身：按需返回固定 bbox（格式与 YuNet 一致：bbox4 + 关键点10 + score1）"""

    def __init__(self, faces=None):
        self.faces = faces
        self.calls = 0

    def setInputSize(self, size):
        pass

    def detect(self, img):
        self.calls += 1
        return True, self.faces


class _FakeLM:
    """替身：不加载 mediapipe 图，永远返回"没有关键点"（走 bbox 兜底分支）"""

    def detect_for_video(self, img, ts):
        return types.SimpleNamespace(face_landmarks=None)

    def close(self):
        pass


class _FakeEmb:
    def embed(self, crop):
        return np.zeros(512, dtype=np.float32)


class _StubFaceLandmarker:
    @staticmethod
    def create_from_options(opts):
        return _FakeLM()


class _StubRunningMode:
    VIDEO = 2


class _StubOptions:
    def __init__(self, **kw):
        self.kw = kw


class _StubMp:
    """只满足 pc_models 用到的两个名字：Image / ImageFormat"""

    class ImageFormat:
        SRGB = 1

    class Image:
        def __init__(self, image_format=None, data=None):
            self.image_format = image_format
            self.data = data


class _StubMpPython:
    class BaseOptions:
        def __init__(self, **kw):
            self.kw = kw


class _StubVision:
    """整体替换 mediapipe.tasks.python.vision：只要 FaceLandmarker 是假的，
    枚举与 Options 用轻量替身即可，不必加载真实模块。"""

    RunningMode = _StubRunningMode
    FaceLandmarkerOptions = _StubOptions
    FaceLandmarker = _StubFaceLandmarker


def _fake_face(x=200, y=150, w=120, h=140, score=0.95):
    return np.array([[x, y, w, h] + [0] * 10 + [score]], dtype=np.float32)


YUNET = os.path.join(os.path.dirname(M.__file__), "..", "models",
                     "face_detection_yunet_2023mar.onnx")
pytestmark = pytest.mark.skipif(
    not os.path.exists(YUNET),
    reason="YuNet weights absent (models/ is git-ignored); see models/README.md")


@pytest.fixture(scope="module")
def pc():
    saved = (M.mp, M.mp_python, M.vision, M.ArcFaceEmbedder)
    M.mp, M.mp_python, M.vision = _StubMp, _StubMpPython, _StubVision
    M.ArcFaceEmbedder = _FakeEmb
    try:
        try:
            yield M.PcPerception()
        except RuntimeError as e:      # OpenCV 读不进 YuNet onnx（版本相关）
            pytest.skip(f"YuNet weights present but unusable by this OpenCV build: {e}")
    finally:
        M.mp, M.mp_python, M.vision, M.ArcFaceEmbedder = saved


@pytest.fixture(autouse=True)
def _reset(pc):
    """每个用例前重置 tracker / 锚点缓存，避免用例间互相污染"""
    from p1_perception.tracker import SimpleTracker
    pc.tracker = SimpleTracker()
    pc._last_anchors.clear()
    pc._last_faces = None
    pc._det_done = False
    pc.stats.update({"det": 0, "lm": 0, "emb": 0})
    yield


def test_blank_frame_no_crash(pc):
    """空帧：不崩，且不跑 FaceLandmarker（无人时的性能优化）"""
    pc.det = _FakeDet(None)
    out = pc.process(BLANK)
    assert out == [], f"空帧不应产出 track，实际 {out}"
    assert pc.stats["lm"] == 0, "无脸时不应调用 FaceLandmarker"


def test_face_path_returns_track(pc):
    """有脸路径：返回 1 个 track，锚点齐全（防 UnboundLocalError / NameError / KeyError）"""
    pc.det = _FakeDet(_fake_face())
    out = pc.process(BLANK)
    assert len(out) == 1, f"应检出 1 张脸，实际 {len(out)}"
    assert set(out[0]["anchors"]) == {"eye_L", "eye_R", "nose", "mouth"}
    assert out[0]["track_id"] >= 1


def test_face_path_runs_landmarker(pc):
    """有脸时应调用 FaceLandmarker（与空帧用例互为对照）"""
    pc.det = _FakeDet(_fake_face())
    pc.process(BLANK)
    assert pc.stats["lm"] > 0


def test_anchor_cache_pruned(pc):
    """孤儿锚点（track 已消失）应被清掉，活着的 track 不受影响"""
    pc.det = _FakeDet(_fake_face())
    pc._last_anchors[999] = {
        "anchors": {"eye_L": (0, 0), "eye_R": (0, 0), "nose": (0, 0), "mouth": (0, 0)},
        "bbox": [0, 0, 1, 1],
    }
    pc.process(BLANK)
    assert 999 not in pc._last_anchors, "孤儿锚点未被清理（内存泄漏）"


def test_face_lost_then_reacquired(pc):
    """脸消失后 track 保留在 ReID 窗口内；超过窗口才回收"""
    pc.det = _FakeDet(_fake_face())
    assert pc.process(BLANK)[0]["track_id"] >= 1

    pc.det = _FakeDet(None)
    for _ in range(30):            # ReID 窗口内，track 应保留
        pc.process(BLANK)
    assert len(pc.tracker.tracks) == 1, "ReID 窗口内应保留 track 用于重新识别"

    for _ in range(40):            # 超过 reid_max_lost=60 才回收
        pc.process(BLANK)
    assert pc.tracker.tracks == [], "超过 ReID 窗口后 track 应被回收"

    pc.det = _FakeDet(_fake_face(x=210, y=160))
    assert len(pc.process(BLANK)) == 1, "脸重新出现时应能重新检出"
