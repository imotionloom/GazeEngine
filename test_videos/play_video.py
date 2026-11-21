"""实时播放视频 → P1→P2 → WebSocket 9000 → VRM数字人
用法: python play_video.py <video.mp4>
"""
import sys, os, time, json, asyncio, threading
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

# WebSocket server state
ws_clients = set()
latest_frame = None
frame_lock = threading.Lock()

async def ws_handler(ws):
    ws_clients.add(ws)
    try:
        while True:
            with frame_lock:
                data = latest_frame
            if data:
                await ws.send(json.dumps(data))
            await asyncio.sleep(0.033)  # 30Hz
    except (asyncio.CancelledError, ConnectionError):
        pass
    finally:
        ws_clients.discard(ws)

async def ws_main():
    from websockets.server import serve
    async with serve(ws_handler, "127.0.0.1", 9000):
        await asyncio.Future()  # run forever

def start_ws():
    t = threading.Thread(target=lambda: asyncio.run(ws_main()), daemon=True)
    t.start()

def play(video_path):
    global latest_frame
    start_ws()
    time.sleep(0.5)  # wait for WS server

    pc = PcPerception()
    fsm = GazeStateMachine()
    sch = PresetScheduler()
    blink = BlinkMachine()

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"错误: 无法打开视频 {video_path}")
        return
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    frame_ms = int(1000 / fps)
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    # 统一缩放到摄像头分辨率640x480，用真机标定内参（不加dy_m手眼偏移）
    geom = GeometrySolver(cam_w=640, cam_h=480, cfg=None)
    geom.fx = 451.77
    geom.cx = 320.06
    geom.cy = 240.0
    vw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    vh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"播放: {os.path.basename(video_path)} ({n} frames @ {fps:.0f}fps, {vw}x{vh} -> 640x480)")
    print("VRM 页面: http://localhost:8898/3d/index.html")

    ts = 0
    vw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    vh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    portrait = vh > vw
    print(f"视频: {vw}x{vh} {'(竖屏,不旋转)' if portrait else '(横屏)'}")

    fail_count = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            fail_count += 1
            if fail_count > 10:
                print("视频读取连续失败，退出")
                break
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ts = 0
            continue
        fail_count = 0
        # 方案B：竖屏中心裁剪正方形再缩放，避免脸变形
        if portrait:
            h, w = frame.shape[:2]
            side = min(w, h)  # 取短边
            x0 = (w - side) // 2
            y0 = (h - side) // 2
            frame = frame[y0:y0+side, x0:x0+side]
        frame = cv2.resize(frame, (640, 480))
        t0 = time.time()
        ts += frame_ms

        try:
            tracks = pc.process(frame)
        except Exception:
            tracks = []
        for t in tracks:
            if "distance" not in t:
                t["distance"] = {"est_m": 1.5, "dot_mps": 0.0}

        buf = encode_perception_tracks(tracks, ts)
        decoded = decode_perception_tracks(buf)
        tracks_d = decoded.get("tracks", [])

        anchors_map = {}
        for t in tracks_d:
            bx, by, bw, bh = t.get("bbox", [0,0,0,0])
            t["bbox_center"] = (bx + bw/2, by + bh/2)
            anc = dict(t.get("anchors", {}))
            anc["speaking_prob"] = t.get("speaking_prob", anc.get("speaking_prob", 0.0))
            anc["identity"] = t.get("identity", anc.get("identity", "unknown"))
            anchors_map[t["track_id"]] = anc

        intent = fsm.update(tracks_d, ts)
        has_person = len(tracks_d) > 0
        # 调试：打印原始人脸检测位置（每秒一次）
        if ts % 1000 < 33:
            raw_faces = [f"t{t.get('track_id','?')}@({t['bbox'][0]+t['bbox'][2]/2:.0f},{t['bbox'][1]+t['bbox'][3]/2:.0f})c={t.get('conf',0):.2f}" for t in tracks]
            print(f"  t={ts/1000:.1f}s state={intent.get('state')} active={intent.get('active_track_id')} 原始检测: {', '.join(raw_faces) if raw_faces else '无'} | 解码后: {len(tracks_d)}个")
        sp = max([t.get("speaking_prob", 0) for t in tracks_d], default=0)
        p = sch.update(has_person, sp, False, False, ts)
        intent["preset"] = p["preset"]
        intent["breath_amp"] = p["breath_amp"]
        intent["blink_scale"] = p["blink_scale"]
        if p.get("anchor_deg") is not None:
            intent["anchor_deg"] = p["anchor_deg"]

        g = geom.solve(intent, anchors_map, ts)
        lid = blink.update(ts)

        # 推给VRM
        data = {
            "neck_yaw": g["body_pan_deg"],
            "neck_pitch": g["body_tilt_deg"],
            "neck_roll": g.get("neck_roll_deg", 0),
            "eye_yaw": g["eye_pan_deg"],
            "eye_tilt": g["eye_tilt_deg"],
            "eye_offset": g["offset_deg"],
            "lid_closure": (1.0 - lid) * 60,
            "blink": lid,
            "state": intent.get("state", ""),
            "look_scale": 30,
        }
        with frame_lock:
            latest_frame = data

        # 画检测框和注视点
        active_id = intent.get("active_track_id", -1)
        for t in tracks_d:
            bx, by, bw, bh = [int(v) for v in t.get("bbox", [0,0,0,0])]
            tid = t.get("track_id", -1)
            is_active = (tid == active_id)
            color = (0, 255, 0) if is_active else (100, 100, 100)
            cv2.rectangle(frame, (bx, by), (bx+bw, by+bh), color, 2)
            cv2.putText(frame, f"id={tid} conf={t.get('conf',0):.2f}",
                        (bx, by-5), cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)
            # 画注视点（active track 的 anchors）
            if is_active and tid in anchors_map:
                anc = anchors_map[tid]
                for k, c in [("eye_L",(255,0,0)),("eye_R",(255,0,0)),("nose",(0,255,255)),("mouth",(0,0,255))]:
                    if k in anc:
                        px, py = int(anc[k][0]), int(anc[k][1])
                        cv2.circle(frame, (px, py), 3, c, -1)
                # 十字线标注视中心
                cx, cy = int(bx+bw/2), int(by+bh/2)
                cv2.drawMarker(frame, (cx, cy), (0,255,0), cv2.MARKER_CROSS, 15, 2)

        # 显示视频窗口
        cv2.putText(frame, f"state={intent.get('state','')} faces={len(tracks_d)}",
                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,255,0), 2)
        cv2.putText(frame, f"neck={g['body_pan_deg']:.0f} eye={g['eye_pan_deg']:.0f}",
                    (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0,200,255), 2)
        cv2.imshow("Video (press Q to quit)", frame)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

        # 实时速度
        elapsed = time.time() - t0
        sleep = max(0, frame_ms/1000 - elapsed)
        time.sleep(sleep)

    cap.release()
    cv2.destroyAllWindows()
    print("播放结束")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python play_video.py <video.mp4>")
        sys.exit(1)
    play(sys.argv[1])
