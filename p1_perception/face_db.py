"""
简单人脸库：内存存储 + 余弦匹配
- 注册：register(name, embedding)
- 识别：identify(embedding) → (name, confidence)
- 阈值：>0.9 熟人，<0.7 陌生人，中间不确定
"""
import numpy as np


KNOWN_THRESH = 0.90
UNKNOWN_THRESH = 0.70


def _cosine(a, b):
    a, b = np.asarray(a).flatten(), np.asarray(b).flatten()
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-6 or nb < 1e-6:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


class FaceDB:
    def __init__(self):
        self.known = {}  # {name: [embedding, ...]}

    def register(self, name, embedding):
        if name not in self.known:
            self.known[name] = []
        self.known[name].append(np.asarray(embedding).flatten())

    def identify(self, embedding):
        """
        返回 (name, confidence)
        最高 cosine > KNOWN_THRESH → (name, sim)
        UNKNOWN_THRESH < sim < KNOWN_THRESH → ("unknown", sim)
        sim < UNKNOWN_THRESH → ("stranger", sim)
        embedding 为 None → ("unknown", 0.0)
        """
        if embedding is None or not self.known:
            return "unknown", 0.0
        best_name, best_sim = "unknown", 0.0
        for name, vectors in self.known.items():
            for v in vectors:
                sim = _cosine(embedding, v)
                if sim > best_sim:
                    best_sim, best_name = sim, name
        if best_sim >= KNOWN_THRESH:
            return best_name, best_sim
        elif best_sim >= UNKNOWN_THRESH:
            return "unknown", best_sim
        else:
            return "stranger", best_sim
