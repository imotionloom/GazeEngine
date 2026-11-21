"""
批量测试视频：跑 P1→P2 pipeline，记录轨迹到 CSV
用法: python test_videos.py [video_name.mp4]
不带参数则跑全部
"""
import sys, os, csv, time, json
import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from p1_perception.pc_models import PcPerception
from p2_decision.state_machine import GazeStateMachine
from p2_decision.geometry import GeometrySolver
from p2_decision.dynamics import BlinkMachine
from p2_decision.preset import PresetScheduler
from p2_decision import calibration as calib
from p2_decision.codec import encode_perception_tracks, decode_perception_tracks

VIDEOS_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(VIDEOS_DIR, "trajectories")
os.makedirs(OUT_DIR, exist_ok=True)


def run_video(video_path, max_frames=900):
    name = os.path.splitext(os.path.basename(video_path))[0]
    print(f"\n{'='*60}")
    print(f"测试: {name}")
    print(f"{'='*60}")

    pc = PcPerception()
    fsm = GazeStateMachine()
    sch = PresetScheduler()
    blink = BlinkMachine()

    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    frame_ms = int(1000 / fps)
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    vw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    vh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    # 统一缩放到640x480，用真机标定内参
    geom = GeometrySolver(cam_w=640, cam_h=480, cfg=None)
    geom.fx = 451.77
    geom.cx = 320.06
    geom.cy = 240.0
    print(f"  {n} frames @ {fps:.0f}fps = {n/fps:.1f}s ({vw}x{vh} -> 640x480)")

    vw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    vh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    portrait = vh > vw
    print(f"  {vw}x{vh} {'竖屏→中心裁剪' if portrait else '横屏'}")

    rows = []
    t0 = time.time()
    ts = 0
    frame_idx = 0

    while frame_idx < min(n, max_frames):
        ok, frame = cap.read()
        if not ok:
            break
        # 方案B：竖屏中心裁剪正方形再缩放，避免脸变形（不旋转）
        if portrait:
            h, w = frame.shape[:2]
            side = min(w, h)
            x0 = (w - side) // 2
            y0 = (h - side) // 2
            frame = frame[y0:y0+side, x0:x0+side]
        frame = cv2.resize(frame, (640, 480))
        frame_idx += 1
        ts = int((frame_idx - 1) * frame_ms)

        # P1: perception
        try:
            tracks = pc.process(frame)
        except Exception as e:
            tracks = []
            if frame_idx <= 3:
                print(f"  P1 error: {e}")

        # 补全 codec 需要的字段
        for t in tracks:
            if "distance" not in t:
                t["distance"] = {"est_m": 1.5, "dot_mps": 0.0}

        # P2: encode→decode→geometry
        buf = encode_perception_tracks(tracks, ts)
        decoded = decode_perception_tracks(buf)
        tracks_d = decoded.get("tracks", [])

        anchors_map = {}
        for t in tracks_d:
            # 从 bbox 计算 bbox_center（FSM 需要）
            bx, by, bw, bh = t.get("bbox", [0,0,0,0])
            t["bbox_center"] = (bx + bw/2, by + bh/2)
            anc = dict(t.get("anchors", {}))
            anc["speaking_prob"] = t.get("speaking_prob", anc.get("speaking_prob", 0.0))
            anc["identity"] = t.get("identity", anc.get("identity", "unknown"))
            anchors_map[t["track_id"]] = anc

        intent = fsm.update(tracks_d, ts)
        has_person = len(tracks_d) > 0
        speaking_prob = max([t.get("speaking_prob", 0) for t in tracks_d], default=0)
        p = sch.update(has_person, speaking_prob, False, False, ts)
        intent["preset"] = p["preset"]
        intent["breath_amp"] = p["breath_amp"]
        intent["blink_scale"] = p["blink_scale"]
        if p.get("anchor_deg") is not None:
            intent["anchor_deg"] = p["anchor_deg"]

        g = geom.solve(intent, anchors_map, ts)
        lid = blink.update(ts)

        rows.append({
            "frame": frame_idx,
            "ts_ms": ts,
            "n_faces": len(tracks_d),
            "active_track": intent.get("active_track_id", -1),
            "state": intent.get("state", ""),
            "preset": p["preset"],
            "neck_pan": round(g["body_pan_deg"], 1),
            "neck_tilt": round(g["body_tilt_deg"], 1),
            "eye_pan": round(g["eye_pan_deg"], 1),
            "eye_tilt": round(g["eye_tilt_deg"], 1),
            "offset": round(g["offset_deg"], 2),
            "lid": round((1.0 - lid), 2),
            "source": g.get("source", ""),
            "speaking": round(anchors_map.get(intent.get("active_track_id", -1), {}).get("speaking_prob", 0), 2),
        })

        if frame_idx % 100 == 0:
            print(f"  frame {frame_idx}/{min(n,max_frames)} faces={len(tracks_d)} state={intent.get('state','')}")

    cap.release()
    elapsed = time.time() - t0
    print(f"  完成: {len(rows)} frames, 耗时 {elapsed:.1f}s ({len(rows)/elapsed:.1f}x realtime)")

    # 写 CSV
    if not rows:
        print("  警告: 无数据行，跳过CSV")
        return
    csv_path = os.path.join(OUT_DIR, f"{name}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    print(f"  轨迹: {csv_path}")

    # 摘要
    summary = analyze(rows)
    print(f"\n  --- 摘要 ---")
    for k, v in summary.items():
        print(f"  {k}: {v}")

    return rows, summary


def analyze(rows):
    """分析轨迹，找出问题点"""
    states = {}
    for r in rows:
        s = r["state"]
        states[s] = states.get(s, 0) + 1

    n = len(rows)
    active_frames = sum(1 for r in rows if r["n_faces"] > 0)
    lost_frames = n - active_frames

    # 眼颈角度范围
    neck_pans = [r["neck_pan"] for r in rows]
    eye_pans = [r["eye_pan"] for r in rows]
    neck_tilts = [r["neck_tilt"] for r in rows]

    # 跳变检测：单帧变化 > 20° 算异常
    jumps = 0
    for i in range(1, len(rows)):
        if abs(rows[i]["neck_pan"] - rows[i-1]["neck_pan"]) > 20:
            jumps += 1
        if abs(rows[i]["eye_pan"] - rows[i-1]["eye_pan"]) > 20:
            jumps += 1

    # ID 切换次数
    id_switches = 0
    prev_id = None
    for r in rows:
        if prev_id is not None and r["n_faces"] > 0 and r["active_track"] != prev_id:
            id_switches += 1
        if r["n_faces"] > 0:
            prev_id = r["active_track"]

    return {
        "总帧数": n,
        "有人帧": f"{active_frames} ({100*active_frames//max(1,n)}%)",
        "无人帧": f"{lost_frames} ({100*lost_frames//max(1,n)}%)",
        "状态分布": ", ".join(f"{k}:{v}" for k, v in sorted(states.items(), key=lambda x: -x[1])),
        "颈pan范围": f"{min(neck_pans):.0f}~{max(neck_pans):.0f}°",
        "眼pan范围": f"{min(eye_pans):.0f}~{max(eye_pans):.0f}°",
        "颈tilt范围": f"{min(neck_tilts):.0f}~{max(neck_tilts):.0f}°",
        "角度跳变>20°": jumps,
        "ID切换次数": id_switches,
    }


if __name__ == "__main__":
    if len(sys.argv) > 1:
        videos = [sys.argv[1]]
    else:
        videos = [f for f in os.listdir(VIDEOS_DIR) if f.endswith(".mp4")]

    all_summaries = {}
    for v in sorted(videos):
        path = os.path.join(VIDEOS_DIR, v)
        if not os.path.exists(path):
            continue
        try:
            rows, summary = run_video(path)
            all_summaries[v] = summary
        except Exception as e:
            print(f"  失败: {e}")
            import traceback; traceback.print_exc()

    # 汇总表
    print(f"\n\n{'='*80}")
    print("全部视频汇总")
    print(f"{'='*80}")
    for v, s in all_summaries.items():
        print(f"\n{v[:30]}:")
        for k, val in s.items():
            print(f"  {k}: {val}")
