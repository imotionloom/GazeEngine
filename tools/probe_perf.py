"""性能探针：把「读相机」「模型推理」「完整循环」分开计时，直接定位瓶颈。

用法（在仓库根目录下）：
    python tools/probe_perf.py
    ... --frames 90 --backend msmf

判读：
    [1] 纯读帧  ≈ 30ms  → 相机 30fps，正常
    [1] 纯读帧  ≈ 90ms  → 相机只有 11fps（暗光长曝光 / 驱动限速）
    [2] 推理    6~10ms  → 无脸（Landmarker 已跳过）
    [2] 推理   20~30ms  → 有脸
    两者谁大，瓶颈就在谁身上。
"""
import argparse
import os
import statistics
import sys
import time

import cv2

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def pct(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, max(0, int(len(v) * p)))]


def sweep(cam=0, frames=40):
    """相机模式扫描：backend × 像素格式 × 分辨率，找出能跑满 30fps 的组合。

    很多 USB 摄像头在 DSHOW 默认 YUY2 + 640x480 下只能跑 10fps，
    切成 MJPG 后立刻能到 30fps——这是最常见的原因。
    """
    combos = [
        ("dshow", None,      (640, 480)),
        ("dshow", "MJPG",    (640, 480)),
        ("dshow", "MJPG",    (480, 360)),
        ("dshow", "MJPG",    (320, 240)),
        ("dshow", "YUY2",    (640, 480)),
        ("msmf",  None,      (640, 480)),
        ("msmf",  "MJPG",    (640, 480)),
    ]
    be_map = {"dshow": cv2.CAP_DSHOW, "msmf": cv2.CAP_MSMF}
    print("backend  fourcc  res        读帧p50   有效fps")
    print("-" * 52)
    for name, fourcc, (w, h) in combos:
        cap = cv2.VideoCapture(cam, be_map[name])
        if not cap.isOpened():
            print("%-8s %-7s %dx%-8d 打不开" % (name, fourcc or "-", w, h))
            continue
        if fourcc:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        cap.set(cv2.CAP_PROP_FPS, 30)
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        for _ in range(8):
            cap.read()
        ts = []
        for _ in range(frames):
            t = time.perf_counter()
            ok, _f = cap.read()
            ts.append((time.perf_counter() - t) * 1000)
        cap.release()
        m = pct(ts, 0.5)
        print("%-8s %-7s %dx%-8d %7.1fms  %6.1f" % (name, fourcc or "-", w, h, m, 1000.0 / m))
    print("-" * 52)
    print("挑每秒帧率最高的那组，把 backend/fourcc/分辨率 写进 test_harness.py")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam", type=int, default=0)
    ap.add_argument("--frames", type=int, default=60)
    ap.add_argument("--backend", default="dshow", choices=["dshow", "msmf", "any"])
    ap.add_argument("--sweep", action="store_true", help="扫描相机模式组合，找最快的一组")
    args = ap.parse_args()
    if args.sweep:
        return sweep(args.cam)

    be = {"dshow": cv2.CAP_DSHOW, "msmf": cv2.CAP_MSMF, "any": cv2.CAP_ANY}[args.backend]
    cap = cv2.VideoCapture(args.cam, be)
    if not cap.isOpened():
        print(f"打不开相机 {args.cam}（backend={args.backend}）—— 可能被占用或远程会话下不可见")
        return 1
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    cap.set(cv2.CAP_PROP_FPS, 30)
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass
    for _ in range(10):          # 预热
        cap.read()

    # ---- 1) 纯读帧（不做任何推理）----
    reads, frame = [], None
    for _ in range(args.frames):
        t = time.perf_counter()
        ok, frame = cap.read()
        reads.append((time.perf_counter() - t) * 1000)
    r50, r95 = pct(reads, 0.5), pct(reads, 0.95)
    print("[1] 纯读帧        p50=%5.1f p95=%5.1f ms  → %5.1f fps" % (r50, r95, 1000.0 / r50))
    print("    相机: %dx%d  报告fps=%.0f  自动曝光=%.2f  曝光值=%.1f" % (
        int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        cap.get(cv2.CAP_PROP_FPS), cap.get(cv2.CAP_PROP_AUTO_EXPOSURE),
        cap.get(cv2.CAP_PROP_EXPOSURE)))

    # ---- 2) 推理（同一帧重复跑，完全排除相机）----
    from p1_perception.pc_models import PcPerception
    pc = PcPerception()
    algs, n_face = [], 0
    for _ in range(20):
        t = time.perf_counter()
        tracks = pc.process(frame)
        algs.append((time.perf_counter() - t) * 1000)
        n_face = max(n_face, len(tracks))
    a50, a95 = pct(algs, 0.5), pct(algs, 0.95)
    print("[2] 推理(单帧)    p50=%5.1f p95=%5.1f ms  检出人脸=%d  stats=%s" % (
        a50, a95, n_face, dict(pc.stats)))

    # ---- 3) 完整循环（读 + 推理 + 空闲）----
    loop = []
    for _ in range(args.frames):
        t = time.perf_counter()
        ok, f2 = cap.read()
        pc.process(f2)
        loop.append((time.perf_counter() - t) * 1000)
    l50 = pct(loop, 0.5)
    print("[3] 读+推理循环   p50=%5.1f ms  → %5.1f fps" % (l50, 1000.0 / l50))

    # ---- 判读 ----
    print()
    if r50 > 2.0 * a50:
        print("→ 瓶颈是【相机】：读一帧 %.1fms，推理只要 %.1fms" % (r50, a50))
        print("   · 开灯 / 白天再测；暗光下自动曝光会把帧率拉到 10fps 左右")
        print("   · 仍慢就关自动曝光：cap.set(CAP_PROP_AUTO_EXPOSURE, 0.25) + 手动 EXPOSURE")
    elif a50 > 2.0 * r50:
        print("→ 瓶颈是【推理】：推理 %.1fms，读帧只要 %.1fms" % (a50, r50))
        print("   · Landmarker 降到每 2 帧一次（见 tools/probe_perf.py 顶部注释）")
        print("   · 或把送进 Landmarker 的图缩到 320x240（关键点是归一化坐标，缩回不变）")
    else:
        print("→ 两者相当（读 %.1fms / 推理 %.1fms），一起优化才有明显收益" % (r50, a50))

    cap.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
