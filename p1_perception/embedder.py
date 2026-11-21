"""
ArcFace embedding（onnxruntime 直跑，不依赖 insightface 目录结构）
输入：112x112 RGB 对齐人脸 crop；输出：512 维 L2 归一化向量
身份锁：两向量余弦相似度 >0.85 视为同一人。
"""
import os
import cv2
import numpy as np
import onnxruntime as ort

MODEL = os.path.join(os.path.dirname(__file__), "..", "models", "arcfaceresnet100-8.onnx")


class ArcFaceEmbedder:
    def __init__(self):
        self.sess = ort.InferenceSession(MODEL, providers=["CPUExecutionProvider"])
        self.inp = self.sess.get_inputs()[0].name
        self.out = self.sess.get_outputs()[0].name

    def embed(self, bgr_face_crop):
        """bgr_face_crop: 112x112 BGR。返回 512 维归一化向量。"""
        img = cv2.resize(bgr_face_crop, (112, 112))
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32)
        img = (img - 127.5) / 128.0
        img = img.transpose(2, 0, 1)[np.newaxis]          # 1x3x112x112
        vec = self.sess.run([self.out], {self.inp: img})[0][0]
        return vec / (np.linalg.norm(vec) + 1e-9)


def cosine(a, b):
    return float(np.dot(a, b))


# ---------------- 自测 ----------------
if __name__ == "__main__":
    e = ArcFaceEmbedder()
    fake_face = np.random.randint(0, 255, (112, 112, 3), dtype=np.uint8)
    v = e.embed(fake_face)
    print(f"embedding 维度={v.shape[0]}  L2范数={np.linalg.norm(v):.3f}")
    v2 = e.embed(fake_face)
    print(f"同一张图余弦={cosine(v, v2):.4f}（应≈1.0）")
