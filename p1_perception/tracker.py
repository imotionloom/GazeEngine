"""
IoU + embedding ReID tracker
- 正常帧：IoU>0.3 匹配，ID 稳定
- IoU 没匹配上：和丢失中的 track 比 cosine，>0.85 复活旧 ID（2秒内）
- 多人交叉：embedding 辅助防错配
"""
import numpy as np


def iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x1, y1 = max(ax, bx), max(ay, by)
    x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def cosine(a, b):
    if a is None or b is None:
        return 0.0
    a, b = np.asarray(a).flatten(), np.asarray(b).flatten()
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-6 or nb < 1e-6:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


class SimpleTracker:
    def __init__(self, iou_thresh=0.3, reid_max_lost=60, reid_thresh=0.7):
        self.iou_thresh = iou_thresh
        self.reid_max_lost = reid_max_lost  # ReID 窗口（~2秒），超时硬删除
        self.reid_thresh = reid_thresh
        self.tracks = []      # [{id, bbox, lost, embedding}]
        self.next_id = 1

    def update(self, det_bboxes, embeddings=None):
        """
        det_bboxes: [[x,y,w,h], ...]
        embeddings: [np.array(512), ...] 与 bboxes 一一对应，可为 None
        返回: [track_id, ...]
        """
        if embeddings is None:
            embeddings = [None] * len(det_bboxes)

        old_tracks = list(self.tracks)
        matched_ids = set()
        out = [None] * len(det_bboxes)

        # 第1级：IoU 贪心匹配
        for j, det in enumerate(det_bboxes):
            best_iou, best_t = 0.0, None
            for t in old_tracks:
                if t["id"] in matched_ids:
                    continue
                v = iou(t["bbox"], det)
                if v > best_iou:
                    best_iou, best_t = v, t
            if best_t is not None and best_iou >= self.iou_thresh:
                best_t["bbox"] = det
                best_t["lost"] = 0
                if embeddings[j] is not None:
                    best_t["embedding"] = embeddings[j]
                matched_ids.add(best_t["id"])
                out[j] = best_t["id"]

        # 第2级：IoU 没匹配上的检测 → ReID 找丢失中的 track
        for j in range(len(det_bboxes)):
            if out[j] is not None:
                continue
            if embeddings[j] is None:
                # 非 embedding 帧：用最近中心匹配，避免 ID 抖动
                best_dist, best_t = 1e9, None
                dx_j = det_bboxes[j][0] + det_bboxes[j][2] / 2
                dy_j = det_bboxes[j][1] + det_bboxes[j][3] / 2
                for t in old_tracks:
                    if t["id"] in matched_ids:
                        continue
                    if t["lost"] > 5:
                        continue  # 丢失太久不硬匹配
                    ox = t["bbox"][0] + t["bbox"][2] / 2
                    oy = t["bbox"][1] + t["bbox"][3] / 2
                    d = ((dx_j - ox) ** 2 + (dy_j - oy) ** 2) ** 0.5
                    if d < best_dist:
                        best_dist, best_t = d, t
                if best_t is not None and best_dist < 100:  # 100px 内算同一人
                    best_t["bbox"] = det_bboxes[j]
                    best_t["lost"] = 0
                    matched_ids.add(best_t["id"])
                    out[j] = best_t["id"]
                else:
                    tid = self.next_id
                    self.next_id += 1
                    self.tracks.append({"id": tid, "bbox": det_bboxes[j], "lost": 0,
                                        "embedding": None})
                    matched_ids.add(tid)
                    out[j] = tid
                continue
            # 和丢失中的 track 比 cosine
            best_sim, best_t = 0.0, None
            for t in old_tracks:
                if t["id"] in matched_ids:
                    continue
                if t["lost"] > self.reid_max_lost:
                    continue
                sim = cosine(embeddings[j], t.get("embedding"))
                if sim > best_sim:
                    best_sim, best_t = sim, t
            if best_t is not None and best_sim >= self.reid_thresh:
                best_t["bbox"] = det_bboxes[j]
                best_t["lost"] = 0
                best_t["embedding"] = embeddings[j]
                matched_ids.add(best_t["id"])
                out[j] = best_t["id"]
            else:
                tid = self.next_id
                self.next_id += 1
                self.tracks.append({"id": tid, "bbox": det_bboxes[j], "lost": 0,
                                    "embedding": embeddings[j]})
                matched_ids.add(tid)
                out[j] = tid

        # 累计未匹配 track 的 lost
        for t in list(self.tracks):
            if t["id"] in matched_ids:
                continue
            t["lost"] += 1
            if t["lost"] > self.reid_max_lost:
                self.tracks.remove(t)
        return out


# ---------------- 自测 ----------------
if __name__ == "__main__":
    tr = SimpleTracker()
    emb1 = np.random.randn(512); emb1 /= np.linalg.norm(emb1)
    emb2 = np.random.randn(512); emb2 /= np.linalg.norm(emb2)

    print("== 一个脸从左往右滑，ID 应保持不变 ==")
    for x in range(100, 400, 20):
        ids = tr.update([[x, 200, 80, 100]], [emb1])
        print(f"bbox_x={x:3d} → track_id={ids[0]}")

    print("\n== 脸消失30帧后用同一 embedding 回来 → 应复活旧 ID ==")
    for _ in range(30):
        tr.update([])
    ids = tr.update([[200, 200, 80, 100]], [emb1])
    print(f"回来 → track_id={ids[0]}（应为旧ID）")

    print("\n== 脸消失30帧后用不同 embedding 回来 → 新 ID ==")
    for _ in range(30):
        tr.update([])
    ids = tr.update([[200, 200, 80, 100]], [emb2])
    print(f"新人 → track_id={ids[0]}（应为新ID）")
