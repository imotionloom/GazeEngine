"""
PC 端感知模型（调试用）——替代板端 RKNN
- YuNet：人脸检测（opencv 自带 onnx）
- FaceLandmarker：478 关键点（mediapipe task）
- embedding：ArcFace（ONNXRuntime，512 维）身份特征，按 track 缓存复用
板端迁移时：本类同名方法换成 RKNN 调用，main.py 不改。
"""
import os
import time
import cv2
import numpy as np
from .embedder import ArcFaceEmbedder
from .tracker import SimpleTracker
from .face_db import FaceDB

EMB_REFRESH_FRAMES = 30      # 身份向量刷新周期（帧）
DET_EVERY_FRAMES = 2         # YuNet 检测周期（帧）：中间帧沿用上次 bbox
MODEL_DIR = os.path.join(os.path.dirname(__file__), "..", "models")

# mediapipe 延迟加载。
# 直接 `import mediapipe` 会初始化 PortAudio（sounddevice），在没有音频设备的
# 服务器/CI 容器上抛 PortAudioError，连纯逻辑测试都收集不了。所以改成首次
# 构造 PcPerception 时才加载，并缓存到模块级变量（测试可以替换这三个名字）。
mp = None
mp_python = None
vision = None


def _ensure_mp():
    """首次调用时加载 mediapipe，返回 (mp, mp_python, vision)。"""
    global mp, mp_python, vision
    if vision is None:
        import mediapipe as _mp
        from mediapipe.tasks import python as _mp_python
        from mediapipe.tasks.python import _vision
        mp, mp_python, vision = _mp, _mp_python, _vision
    return mp, mp_python, vision

# FaceMesh 关键点索引（478 点模型，含虹膜）
IDX_IRIS_L = 468      # 左虹膜中心（画面左）
IDX_IRIS_R = 473      # 右虹膜中心
IDX_NOSE_TIP = 1      # 鼻尖
IDX_UPPER_LIP = 13    # 上唇中心
IDX_LOWER_LIP = 14    # 下唇中心
IDX_FACE_LEFT = 234   # 左脸轮廓（耳前）
IDX_FACE_RIGHT = 454  # 右脸轮廓（耳前）
IDX_CHIN = 152        # 下巴尖
IDX_FOREHEAD = 10     # 额头中心
# EAR（眼睛开合度）关键点——当前未使用：镜像眨眼取消后无人消费。
# 恢复眨眼检测时按此索引重算 _ear()（见 git 历史）。
# IDX_EYE_L = [33, 160, 158, 133, 153, 144]
# IDX_EYE_R = [362, 385, 387, 263, 373, 380]




class PcPerception:
    def __init__(self):
        det_path = os.path.join(MODEL_DIR, "face_detection_yunet_2023mar.onnx")
        self.det = cv2.FaceDetectorYN_create(det_path, "", (640, 480), 0.4, 0.3, 5000)
        lm_path = os.path.join(MODEL_DIR, "face_landmarker.task")
        mp, mp_python, vision = _ensure_mp()
        self._mp = mp
        opts = vision.FaceLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=lm_path),
            running_mode=vision.RunningMode.VIDEO, num_faces=4)
        self.lm = vision.FaceLandmarker.create_from_options(opts)
        self.emb = ArcFaceEmbedder()
        self.tracker = SimpleTracker()
        self.face_db = FaceDB()  # 熟人识别（默认空库，需手动 register）
        self._frame_no = 0
        self._last_faces = None     # YuNet 隔帧检测时复用
        self._det_done = False
        self.stats = {"det": 0, "lm": 0, "emb": 0}   # 累计执行次数（诊断用）
        self._ts = 0
        self._prev_gray = None
        self._last_anchors = {}   # 每 track 上一帧好锚点，丢帧时用 bbox 位移补偿
        self._track_seen = {}     # owner注册计数 {tid: {"frames": int, "embs": []}}
        self._owner_registered = False  # owner是否已注册
        # 环境变量重置：GAZE_RESET_OWNER=1 启动时清空face_db
        if os.environ.get("GAZE_RESET_OWNER") == "1":
            self.face_db.known.clear()
            print("[FaceDB] GAZE_RESET_OWNER=1, 已清空人脸库")
        self._mouth_hist = {}     # 每 track 嘴部开合历史（用于 speaking_prob）

    def detect_motion(self, bgr, face_bboxes):
        """帧差找画面边缘运动，返回 (u,v) 或 None"""
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        if self._prev_gray is None:
            self._prev_gray = gray
            return None
        diff = cv2.absdiff(gray, self._prev_gray)
        self._prev_gray = gray
        diff = cv2.GaussianBlur(diff, (5, 5), 0)
        _, th = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
        # 人脸区域遮掉
        for (x, y, bw, bh) in face_bboxes:
            cv2.rectangle(th, (max(0, x-20), max(0, y-20)), (x+bw+20, y+bh+20), 0, -1)
        cnts, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not cnts:
            return None
        c = max(cnts, key=cv2.contourArea)
        if cv2.contourArea(c) < 200:
            return None
        M = cv2.moments(c)
        if M["m00"] == 0:
            return None
        return (int(M["m10"]/M["m00"]), int(M["m01"]/M["m00"]))

    def process(self, bgr_frame):
        """返回 tracks: [{track_id, bbox, anchors:{eye_L,eye_R,nose,mouth}, conf}]"""
        h, w = bgr_frame.shape[:2]
        self._frame_no += 1
        # YuNet 隔帧跑：中年帧沿用上次 bbox（人移动 ~10cm/帧，滞后一帧可接受）
        run_det = (self._frame_no % DET_EVERY_FRAMES == 0) or (not self._det_done)
        if run_det:
            self.det.setInputSize((w, h))
            _, faces = self.det.detect(bgr_frame)
            self._last_faces = faces
            self._det_done = True
            self.stats["det"] += 1
        else:
            faces = self._last_faces

        tracks = []
        bboxes = []
        if faces is not None:
            for f in faces:
                bboxes.append(list(f[:4].astype(int)))
        # 先算 embedding 供 tracker ReID
        # 优化：只为"新轨迹"算 ArcFace（~200ms），已有 track 复用旧 embedding
        # 每 30 帧强制刷新一次已有 track 的 embedding（身份可能变化）
        raw_embs = []
        emb_is_new = []  # 每张脸是否新算了emb（False=复用旧track的embedding）
        refresh = (self._frame_no % EMB_REFRESH_FRAMES == 0)
        for f in (faces if faces is not None else []):
            fx, fy, fbw, fbh = f[:4].astype(int)
            crop = bgr_frame[max(0, fy):fy+fbh, max(0, fx):fx+fbw]
            # 检查是否已有 track 且有缓存 embedding（IoU>0.2才复用，避免串台）
            existing_emb = None
            best_iou = 0.3  # 与tracker的iou_thresh一致，避免复用emb后tracker判新track
            for t in self.tracker.tracks:
                if t.get("embedding") is None:
                    continue
                if t.get("lost", 0) > 30:
                    continue
                tx, ty, tw, th = t["bbox"]
                # 计算IoU
                ix1 = max(fx, tx); iy1 = max(fy, ty)
                ix2 = min(fx+fbw, tx+tw); iy2 = min(fy+fbh, ty+th)
                iw = max(0, ix2-ix1); ih = max(0, iy2-iy1)
                inter = iw * ih
                union = fbw*fbh + tw*th - inter
                iou = inter / max(1, union)
                if iou > best_iou:
                    best_iou = iou
                    existing_emb = t["embedding"]
            if existing_emb is not None and not refresh:
                raw_embs.append(existing_emb)
                emb_is_new.append(False)
            elif crop.size > 0:
                raw_embs.append(self.emb.embed(crop))
                emb_is_new.append(True)
                self.stats["emb"] += 1
            else:
                raw_embs.append(None)
                emb_is_new.append(False)
        ids = self.tracker.update(bboxes, raw_embs)
        if not bboxes:
            self.motion = self.detect_motion(bgr_frame, [])
            return []
        rgb = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2RGB)
        mp_img = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        now = int(time.time() * 1000)
        self._ts = max(self._ts + 1, now)
        lm_result = self.lm.detect_for_video(mp_img, self._ts)
        self.stats["lm"] += 1

        # 按 bbox 中心距离把 YuNet 检测和 MediaPipe landmarks 配对
        lm_list = lm_result.face_landmarks or []
        lm_centers = []
        for lm in lm_list:
            cx = (lm[IDX_IRIS_L].x + lm[IDX_IRIS_R].x) / 2 * w
            cy = (lm[IDX_IRIS_L].y + lm[IDX_IRIS_R].y) / 2 * h
            lm_centers.append((cx, cy))

        if faces is not None:
            used_lm = set()  # 已配对的 landmark，防止多人复用同一组
            for i, f in enumerate(faces):
                x, y, bw, bh = f[:4].astype(int)
                conf = float(f[-1])
                tid = ids[i]
                anchors = {"eye_L": (x + bw / 2, y + bh / 2),
                           "eye_R": (x + bw / 2, y + bh / 2),
                           "nose": (x + bw / 2, y + bh / 2),
                           "mouth": (x + bw / 2, y + bh / 2)}
                lm_ok = False
                # 找最近的未使用 landmark
                det_cx, det_cy = x + bw/2, y + bh/2
                best_j, best_d = -1, 1e9
                for j, (lmx, lmy) in enumerate(lm_centers):
                    if j in used_lm:
                        continue
                    d = ((det_cx - lmx)**2 + (det_cy - lmy)**2) ** 0.5
                    if d < best_d:
                        best_d, best_j = d, j
                if best_j >= 0 and best_d < 100:  # 100px 内算同一人
                    used_lm.add(best_j)
                    lm = lm_list[best_j]
                    def px(idx): return (lm[idx].x * w, lm[idx].y * h)
                    mouth = ((lm[IDX_UPPER_LIP].x + lm[IDX_LOWER_LIP].x) / 2 * w,
                             (lm[IDX_UPPER_LIP].y + lm[IDX_LOWER_LIP].y) / 2 * h)
                    anchors = {"eye_L": px(IDX_IRIS_L), "eye_R": px(IDX_IRIS_R),
                               "nose": px(IDX_NOSE_TIP), "mouth": mouth}
                    lm_ok = True

                    # --- 嘴部开合度 → speaking_prob ---
                    eye_dist = max(1e-6, abs(lm[IDX_IRIS_R].x - lm[IDX_IRIS_L].x))
                    mouth_open = abs(lm[IDX_LOWER_LIP].y - lm[IDX_UPPER_LIP].y) / eye_dist
                    hist = self._mouth_hist.setdefault(tid, [])
                    hist.append(mouth_open)
                    if len(hist) > 15:
                        hist.pop(0)
                    # 说话判定：最近 15 帧内开合度的方差 > 阈值
                    if len(hist) >= 5:
                        mean_m = sum(hist) / len(hist)
                        var_m = sum((v - mean_m) ** 2 for v in hist) / len(hist)
                        speaking_prob = min(1.0, var_m * 200.0) if mean_m > 0.02 else 0.0
                    else:
                        speaking_prob = 0.0

                    # --- 头部姿态估计（简化几何法） ---
                    face_w = max(1e-6, abs(lm[IDX_FACE_RIGHT].x - lm[IDX_FACE_LEFT].x))
                    face_h = max(1e-6, abs(lm[IDX_CHIN].y - lm[IDX_FOREHEAD].y))
                    nose_x = lm[IDX_NOSE_TIP].x
                    # yaw: 鼻尖在脸宽中的相对位置（0.5=正面，偏左/右=转头）
                    nose_rel = (nose_x - lm[IDX_FACE_LEFT].x) / face_w
                    head_yaw = (nose_rel - 0.5) * 90.0  # ±45°
                    # pitch: 鼻尖在脸高中的相对位置
                    nose_rel_y = (lm[IDX_NOSE_TIP].y - lm[IDX_FOREHEAD].y) / face_h
                    head_pitch = (nose_rel_y - 0.45) * 60.0  # 0.45=正面参考

                    anchors["speaking_prob"] = round(speaking_prob, 3)
                    anchors["head_yaw"] = round(head_yaw, 1)
                    anchors["head_pitch"] = round(head_pitch, 1)

                    # 存好锚点供丢帧补偿
                    self._last_anchors[tid] = {"anchors": anchors, "bbox": [x, y, bw, bh]}
                elif tid in self._last_anchors:
                    # 丢帧：用 bbox 位移补偿上一帧锚点（只平移 tuple 坐标，跳过 float 元数据）
                    old = self._last_anchors[tid]
                    ox, oy, obw, obh = old["bbox"]
                    dx = (x + bw/2) - (ox + obw/2)
                    dy = (y + bh/2) - (oy + obh/2)
                    for k, v in old["anchors"].items():
                        if isinstance(v, tuple) and len(v) == 2:
                            anchors[k] = (v[0] + dx, v[1] + dy)
                        else:
                            anchors[k] = v
                emb = raw_embs[i] if i < len(raw_embs) else None
                identity, _conf = self.face_db.identify(emb)
                # owner自动注册：要求高置信度(>=0.8)连续50帧，且至少有3帧新算emb
                # （第1帧+第30帧refresh+第60帧refresh≈2秒注册）
                if emb is not None and not self._owner_registered:
                    seen = self._track_seen.setdefault(tid, {"frames": 0, "embs": []})
                    if conf >= 0.8:
                        seen["frames"] += 1
                        # 只在新算emb时加入平均（避免复用旧向量）
                        if i < len(emb_is_new) and emb_is_new[i]:
                            seen["embs"].append(emb)
                            if len(seen["embs"]) > 30:
                                seen["embs"].pop(0)
                        if seen["frames"] >= 50 and len(seen["embs"]) >= 3:
                            import numpy as np
                            avg_emb = np.mean(seen["embs"], axis=0)
                            self.face_db.register("owner", avg_emb)
                            self._owner_registered = True
                            identity = "owner"
                            print(f"[FaceDB] 已注册 owner (tid={tid}, {seen['frames']}帧, {len(seen['embs'])}个emb)")
                    else:
                        # 置信度掉下来就重置计数，确保是"连续"高置信度
                        seen["frames"] = 0
                        seen["embs"] = []
                tracks.append({
                    "track_id": ids[i],
                    "bbox": [x, y, bw, bh],
                    "anchors": anchors,
                    "conf": conf,
                    "embedding": emb,
                    "speaking_prob": anchors.get("speaking_prob", 0.0),
                    "identity": identity,
                })
        self.motion = self.detect_motion(bgr_frame, [list(f[:4].astype(int)) for f in (faces if faces is not None else [])])
        alive = {t["id"] for t in self.tracker.tracks}   # tracker 里 key 是 "id"（不是 track_id）
        for k in list(self._last_anchors):
            if k not in alive:
                del self._last_anchors[k]
        for k in list(self._mouth_hist):
            if k not in alive:
                del self._mouth_hist[k]
        for k in list(self._track_seen):
            if k not in alive:
                del self._track_seen[k]
        # 按 bbox 面积降序（大=近），保证 tracks[0] 是最近的人
        tracks.sort(key=lambda t: -(t["bbox"][2] * t["bbox"][3]))
        return tracks


if __name__ == "__main__":
    # 自测：黑色空帧 → 0 脸
    p = PcPerception()
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    out = p.process(frame)
    print(f"空脸帧 tracks={len(out)}（模型加载成功）")
