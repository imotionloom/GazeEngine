"""
注视链路测试台（摄像头 → IF-1a proto → P2 → 打印）
按接口规范走：P1 出 dict → codec 序列化为 bytes（模拟 ZMQ）→ P2 反序列化 → 决策。
无摄像头时用 --image 模式喂单张图。
"""
import sys, os, time, argparse, json, csv
import cv2
import numpy as np
import zmq

sys.path.insert(0, os.path.dirname(__file__))

from p1_perception.pc_models import PcPerception
from p2_decision import codec, config as C
from p2_decision.state_machine import GazeStateMachine
from p2_decision.geometry import GeometrySolver
from p2_decision.preset import PresetScheduler, Preset
from p2_decision import calibration as calib
from p2_decision.joint_mapper import geometry_to_joints
from p2_decision.dynamics import (BlinkMachine, BLINK_DURATION_MS,
                                  BLINK_CLOSE_RATIO, BLINK_HOLD_RATIO)


def tracks_to_if1a(tracks, ts_ms, cfg=None):
    """tracks → proto bytes（IF-1a）。"""
    k = float(getattr(cfg, "dist_k", C.DIST_CALIB_K)) if cfg else C.DIST_CALIB_K
    out = []
    for t in tracks:
        anc = t["anchors"]
        try:
            eye_px = abs(float(anc["eye_R"][0]) - float(anc["eye_L"][0]))
            est_m = max(0.5, min(3.0, k / max(20.0, eye_px)))
        except (KeyError, TypeError, ValueError):
            est_m = 1.5
        out.append({
            "track_id": t["track_id"],
            "bbox": t["bbox"],
            "anchors": t["anchors"],
            "conf": t["conf"],
            "distance": {"est_m": round(est_m, 2), "dot_mps": 0.0},
            "speaking_prob": t.get("speaking_prob", 0.0),
            "identity": t.get("identity", "unknown"),
        })
    return codec.encode_perception_tracks(out, ts_ms)


def build_render_payload(g, lid, blink, ts_ms, cfg, state, presence=None):
    """给数字人的一帧数据。抽成函数是为了能被测试直接调用。"""
    p = {
        "neck_yaw": g["body_pan_deg"], "neck_pitch": g["body_tilt_deg"],
        "neck_roll": g.get("neck_roll_deg", 0),
        "eye_yaw": -g["eye_pan_deg"], "eye_tilt": g["eye_tilt_deg"],
        "blink": lid,
        # 眨眼"已进行多少 ms"——相对量，跨进程/跨机器都不受时钟基准影响
        "blink_age_ms": (ts_ms - blink.t0) if blink.t0 is not None else None,
        "blink_ms": BLINK_DURATION_MS,
        "blink_close": BLINK_CLOSE_RATIO,
        "blink_hold": BLINK_HOLD_RATIO,
        "lid_closure": g.get("lid_closure_deg", 0),
        "look_scale": getattr(cfg, "look_deg_per_weight", calib.C.LOOK_DEG_PER_WEIGHT),
        "source": g["source"], "state": state, "ts": ts_ms,
    }
    if presence is not None:
        p["presence"] = presence
    return p


def run_p2_if1a(buf, fsm, geom, ts_ms, raw_tracks=None, extra_pan_deg=0.0):
    """P2：bytes → dict → 状态机 → 几何 → 一行打印。"""
    msg = codec.decode_perception_tracks(buf)
    tracks = msg["tracks"]
    # EAR 不走 proto，从 raw_tracks 补回来
    # EAR 已停用（镜像眨眼取消），proto 里也没有该字段；保留结构便于恢复
    # if raw_tracks:
    #     for rt in raw_tracks:
    #         for t in tracks:
    #             if t["track_id"] == rt["track_id"]:
    #                 t["anchors"]["ear"] = rt["anchors"].get("ear", 1.0)

    # 状态机（bbox 中心）
    fsm_tracks = [{"track_id": t["track_id"], "conf": t["conf"],
                   "bbox_center": (t["bbox"][0] + t["bbox"][2] / 2,
                                   t["bbox"][1] + t["bbox"][3] / 2)} for t in tracks]
    intent = fsm.update(fsm_tracks, ts_ms)

    # 几何
    anchors_map = {t["track_id"]: t["anchors"] for t in tracks}
    # ------------------------------------------------------------------
    # 镜像眨眼：已停用（2026-10-28）
    # 原因：_force_blink 的触发窗口缺少下界，用户眨眼后会连续触发 2 次，
    #       观感上闭眼时长偏长。需要恢复时把下面整段和下面两处一并解注释：
    #         · 本文件的 blink.trigger 处
    #         · geometry.py 的 _force_blink 字段
    #     for t in tracks:
    #         ear = t["anchors"].get("ear", 1.0)
    #         if ear < 0.18:
    #             geom._ear_low_frames = getattr(geom, "_ear_low_frames", 0) + 1
    #         else:
    #             geom._ear_low_frames = 0
    #         now_cool = ts_ms - getattr(geom, "_last_mirror_blink", 0)
    #         if (geom._ear_low_frames >= 2 and not getattr(geom, "_user_blink_just_now", False)
    #                 and now_cool > 3000):
    #             geom._user_blink_just_now = True
    #             geom._last_mirror_blink = ts_ms
    #             geom._mirror_blink_at = ts_ms + 150 + (ts_ms % 150)
    #         elif ear > 0.3:
    #             geom._user_blink_just_now = False
    # ------------------------------------------------------------------
    g = geom.solve(intent, anchors_map, ts_ms, extra_pan_deg)

    return intent, g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", help="单张测试图路径（无摄像头用）")
    ap.add_argument("--cam", type=int, default=0)
    ap.add_argument("--no-cam", action="store_true",
                    help="无相机：只跑 P2 生成拟人空闲运动（扫描/扫视/眨眼/呼吸）")
    args = ap.parse_args()

    pc = None if args.no_cam else PcPerception()
    fsm = GazeStateMachine()
    # 与 P2 主进程走同一条标定通路：读 calibration/*/active.json，缺省回退
    cfg = calib.load()
    for w in calib.apply(cfg):
        try:
            print(f"[harness][calib] {w}")
        except UnicodeEncodeError:
            safe = w.encode("gbk", errors="replace").decode("gbk")
            print(f"[harness][calib] {safe}")
    geom = GeometrySolver(cfg=cfg)
    sch = PresetScheduler()
    blink = BlinkMachine()

    # ZMQ PUB：把 g 当 JSON 发到 5557，供 ws_bridge 转发给浏览器数字人
    zctx = zmq.Context()
    pub = zctx.socket(zmq.PUB)
    pub_addr = os.environ.get("GAZE_DEBUG_PUB", "tcp://127.0.0.1:5557")
    try:
        pub.bind(pub_addr)
    except zmq.error.ZMQError:
        print(f"[harness] {pub_addr} 已被占用（可能已有实例在跑）；改用 GAZE_DEBUG_PUB=tcp://127.0.0.1:5657 再试")
        sys.exit(1)
    time.sleep(0.5)   # 等 SUB 连上（ZMQ slow joiner）

    if args.image:
        if pc is None:
            print("[harness] --image 需要摄像头模型，不要同时加 --no-cam")
            sys.exit(1)
        bgr = cv2.imread(args.image)
        raw = pc.process(bgr)
        buf = tracks_to_if1a(raw, 1000, cfg)
        intent, g = run_p2_if1a(buf, fsm, geom, 1000, raw)
        print(f"[IF-1a bytes={len(buf)}] 人脸数={len(codec.decode_perception_tracks(buf)['tracks'])}")
        print(f"状态={intent['state']:18s} 活动track={intent['active_track_id']}")
        print(f"颈pan={g['body_pan_deg']:+6.1f}° 颈tilt={g['body_tilt_deg']:+5.1f}° "
              f"眼pan={g['eye_pan_deg']:+5.1f}° 会聚={g['offset_deg']:.2f}° 目标点={g['source']}")
        print("IF-4 关节指令:", geometry_to_joints(g, cfg=cfg))
        return

    if args.no_cam:
        # ---- 无相机：纯 P2 空闲运动，30Hz 推给数字人 ----
        print("无相机模式：只跑 P2（扫描/扫视/眨眼/呼吸），Ctrl+C 退出")
        t0 = time.time()
        last_print = 0.0
        frame = 0
        _pending_blink = 0
        try:
            while True:
                ts = int((time.time() - t0) * 1000)
                intent = fsm.update([], ts)
                # 走 PresetScheduler（和 main.py 一致）
                p = sch.update(intent, ts)
                intent["preset"] = p["preset"]
                intent["breath_amp"] = p["breath_amp"]
                intent["blink_scale"] = p["blink_scale"]
                if p.get("anchor_deg") is not None:
                    intent["anchor_deg"] = p["anchor_deg"]
                    intent["neck_ratio"] = p["neck_ratio"]
                g = geom.solve(intent, {}, ts)
                # ② 认知负荷：思考时眨眼变稀；"想到"时一次双连眨
                if geom.presence.current == "THINKING":
                    _ld = float(geom.presence.debug_info()["dims"]["load"])
                    blink.set_scale(0.95 - 0.55 * _ld)      # load 0.4→0.73  0.9→0.45
                else:
                    blink.set_scale(1.0)
                if geom.presence.take_insight():
                    blink.trigger(ts)
                    _pending_blink = ts + 190
                if _pending_blink and ts >= _pending_blink:
                    blink.trigger(ts, force=True)  # 双连眨第二下绕过 cooldown
                    _pending_blink = 0
                lid = blink.update(ts)
                pub.send_string(json.dumps(
                    build_render_payload(g, lid, blink, ts, cfg, intent["state"],
                                         presence=geom.presence.debug_info())))
                frame += 1
                if time.time() - last_print > 1.0:
                    last_print = time.time()
                    print(f"t={ts:6d}ms {intent['state']:10s} "
                          f"颈pan={g['body_pan_deg']:+6.1f}° 眼pan={g['eye_pan_deg']:+6.1f}° "
                          f"眼tilt={g['eye_tilt_deg']:+5.1f}° lid={lid:.2f} "
                          f"fps={frame / max(0.1, time.time() - t0):.1f}")
                time.sleep(1 / 30)
        except KeyboardInterrupt:
            print("\n退出")
        finally:
            try:
                geom.audio.close()
            except Exception:
                pass
        return

    # 相机后端：Windows 默认 MSMF 时 cap.read() 常要 15-40ms，DSHOW 通常只要 2-3ms。
    # 顺序尝试，第一个能打开的用；GAZE_CAM_BACKEND 可强制指定 auto|dshow|msmf|any。
    cam_backend = os.environ.get("GAZE_CAM_BACKEND", "auto").lower()
    cands = {"dshow": [("dshow", cv2.CAP_DSHOW)],
             "msmf":  [("msmf",  cv2.CAP_MSMF)],
             "any":   [("any",   cv2.CAP_ANY)]}.get(cam_backend)
    if not cands:
        # 实测：这台机器 DSHOW 读帧 97ms（~10fps），MSMF 只要 2ms（缓冲返回）。
        # 所以 MSMF 优先，DSHOW 兜底；GAZE_CAM_BACKEND 可强制。
        cands = ([("msmf", cv2.CAP_MSMF), ("dshow", cv2.CAP_DSHOW), ("any", cv2.CAP_ANY)]
                 if os.name == "nt" else [("any", cv2.CAP_ANY)])
    cap = None
    cam_name = "?"
    for name, be in cands:
        try:
            c = cv2.VideoCapture(args.cam, be)
        except Exception:
            continue
        if c is not None and c.isOpened():
            cap, cam_name = c, name
            break
        try:
            c.release()
        except Exception:
            pass
    if cap is None:
        print(f"摄像头{args.cam}打不开（试过 {[n for n, _ in cands]}）"); return
    # 分辨率/帧率/缓冲：分辨率必须与 GeometrySolver 的 cam_w/cam_h 一致，否则像素→角度会错
    cam_w, cam_h = 640, 480
    # 像素格式：很多 USB 摄像头在 DSHOW 默认 YUY2 下 640x480 只能跑 ~10fps，切 MJPG 才能 30fps。
    # 用 GAZE_CAM_FOURCC=YUY2 / NONE 可回退。必须在设分辨率之前设置。
    fourcc_name = os.environ.get("GAZE_CAM_FOURCC", "MJPG").upper()
    if fourcc_name and fourcc_name != "NONE":
        ok4 = cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc_name[:4]))
        if not ok4:
            print(f"[cam][warn] 设置像素格式 {fourcc_name} 失败，沿用驱动默认")
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, cam_w)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cam_h)
    cap.set(cv2.CAP_PROP_FPS, 30)
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)     # 不让驱动攒旧帧，降低 cap.read() 阻塞
    except Exception:
        pass
    _ok0, _f0 = cap.read()      # 先读一帧，逼驱动完成初始化（顺便让首帧时间不再算进 cap）
    real_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or cam_w
    real_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or cam_h
    got4 = int(cap.get(cv2.CAP_PROP_FOURCC))
    got4s = "".join(chr((got4 >> (8 * i)) & 0xFF) for i in range(4))
    print(f"[cam] backend={cam_name} {real_w}x{real_h} @ {cap.get(cv2.CAP_PROP_FPS):.0f}fps "
          f"fourcc={got4s} buffersize={cap.get(cv2.CAP_PROP_BUFFERSIZE):.0f}")
    if got4s.strip("\x00") and got4s.upper()[:4] != fourcc_name[:4] and fourcc_name != "NONE":
        print(f"[cam][warn] 像素格式请求 {fourcc_name}，实际 {got4s}（可能不支持）")
    if (real_w, real_h) != (cam_w, cam_h):
        print(f"[cam][warn] 实际分辨率 {real_w}x{real_h} != {cam_w}x{cam_h}，"
              f"像素→角度会偏，请调 GeometrySolver(cam_w=, cam_h=)")
    # 构建标记：方便确认跑的是不是最新代码（对比 presence.py 的修改时间）
    try:
        _pp = os.path.join(os.path.dirname(__file__), "p2_decision", "presence.py")
        print(f"[build] presence.py mtime = {time.strftime('%m-%d %H:%M:%S', time.localtime(os.path.getmtime(_pp)))}")
    except Exception:
        pass
    print("链路测试中（Q退出）...  C=标定模式, S=保存标定")
    t0 = time.time()
    prev_ts = 0
    calib_mode = False
    calib_pan = 0.0   # 手动偏移
    calib_tilt = 0.0
    # CSV 日志（每次启动清空）
    log_path = os.path.join(os.path.dirname(__file__), "runtime_log.csv")
    lf = open(log_path, "w", newline="", encoding="utf-8")
    lw = csv.writer(lf)
    lw.writerow(["ts_ms", "dt_ms", "n_tracks", "state", "family", "source",
                 "arousal", "load", "valence", "intent", "stability", "fatigue",
                 "raw_pan", "raw_tilt", "neck_pan", "neck_tilt",
                 "eye_pan", "eye_tilt", "offset", "lid",
                 "tgt_u", "tgt_v", "eye_dist_px",
                 "pan_sp", "split_engaged", "sac_inflight",
                 "neck_roll", "interlude", "neck_hold"])
    print(f"日志: {log_path}")
    _times = {"cap": [], "alg": [], "p2": [], "pub": [], "ui": []}
    _pending_blink = 0      # 双连眨的第二次
    # 帧率封顶：MSMF 的 read() 是缓冲返回（实测 ~2ms），若计算比相机快就会空转处理重复帧。
    # GAZE_FPS_CAP 可覆盖；默认 msmf=30fps，dshow 不封顶（它的 read 本身会阻塞）
    _fps_cap = float(os.environ.get("GAZE_FPS_CAP", "0") or 0)
    if _fps_cap <= 0:
        _fps_cap = 30.0 if cam_name == "msmf" else 0.0
    _frame_ms = 1000.0 / _fps_cap if _fps_cap > 0 else 0.0
    if _frame_ms > 0 and os.name == "nt":
        # Windows 默认 sleep 粒度 ~15.6ms，封顶 33ms 会睡过头（实测掉到 21fps）。
        # timeBeginPeriod(1) 把粒度降到 1ms。
        try:
            import ctypes
            ctypes.windll.winmm.timeBeginPeriod(1)
            print("[cam] timer resolution -> 1ms (timeBeginPeriod)")
        except Exception as e:
            print(f"[cam][warn] timeBeginPeriod 失败: {e}")
    print(f"[cam] frame cap = {_fps_cap:.0f} fps")
    # 预览刷新间隔：默认 2（每 2 帧刷一次，约 15fps）；GAZE_UI_EVERY=1 恢复每帧刷
    _ui_every = max(1, int(os.environ.get("GAZE_UI_EVERY", "2") or 2))
    _frame_i = 0
    print(f"[cam] preview every {_ui_every} frame(s)")
    # 开机编排：先送3秒中性姿态等 ZMQ/浏览器连上，再播17s编排
    from p2_decision.boot_choreography import BootChoreography
    boot = BootChoreography()
    boot_active = False
    boot_played = False   # 开机只播一次，结束后不重启
    boot_delay_until = time.time() + 3.0   # 3秒连接缓冲
    print("[BOOT] 3秒后开始开机编排，请确认浏览器已打开...")
    while True:
        _loop_t0 = time.perf_counter()
        _t0 = time.perf_counter()
        ok, bgr = cap.read()
        if not ok: break
        ts = int((time.time() - t0) * 1000)
        _times["cap"].append((time.perf_counter()-_t0)*1000)
        # --- 开机编排分支 ---
        if not boot_active and not boot_played and time.time() >= boot_delay_until:
            boot.start()
            boot_active = True
            boot_played = True
            print("[BOOT] 开始播放开机编排（17s）...")
        if boot_active:
            g = boot.sample()
            if g is None:
                boot_active = False
                print("[BOOT] 结束，进入实时识别")
            else:
                g["source"] = "BOOT"
                lid = g["lid_up"]
                pub.send_string(json.dumps(
                    build_render_payload(g, lid, blink, ts, cfg, "BOOT", presence=None)))
                if ts % 500 < 33:
                    print(f"[BOOT] t={ts/1000:.1f}s  pan={g['body_pan_deg']:+5.1f}  "
                          f"tilt={g['body_tilt_deg']:+5.1f}  epan={g['eye_pan_deg']:+5.1f}  lid={lid:.2f}")
                _frame_i += 1
                if _frame_i % _ui_every == 0:
                    cv2.imshow("gaze", cv2.flip(bgr, 1))
                if (cv2.waitKey(1) & 0xFF) == ord('q'): break
                continue
        # --- 连接缓冲期：送中性姿态（居中睁眼），等 ZMQ/浏览器连上 ---
        if not boot_active and time.time() < boot_delay_until:
            g = {"body_pan_deg": 0.0, "body_tilt_deg": 0.0, "neck_roll_deg": 0.0,
                 "eye_pan_deg": 0.0, "eye_tilt_deg": 0.0, "offset_deg": 0.0,
                 "lid_up": 1.0, "lid_closure_deg": 0.0, "source": "CONNECT"}
            lid = 1.0
            pub.send_string(json.dumps(
                build_render_payload(g, lid, blink, ts, cfg, "CONNECT", presence=None)))
            _frame_i += 1
            if _frame_i % _ui_every == 0:
                cv2.imshow("gaze", cv2.flip(bgr, 1))
            if (cv2.waitKey(1) & 0xFF) == ord('q'): break
            continue
        _t0 = time.perf_counter()
        raw_tracks = pc.process(bgr)
        _times["alg"].append((time.perf_counter()-_t0)*1000)
        _t0 = time.perf_counter()
        buf = tracks_to_if1a(raw_tracks, ts, cfg)
        # 边缘运动扫视：低通 + 死区，只在无人脸时生成偏移（先算好再传入 solve）
        distraction_pan = getattr(main, '_distraction_pan', 0)
        if getattr(pc, 'motion', None) and len(raw_tracks) == 0:
            mx, my = pc.motion
            target_pan = (mx - 320) / 320 * 8
            if abs(target_pan - distraction_pan) < 3:
                target_pan = distraction_pan
            distraction_pan = 0.2 * target_pan + 0.8 * distraction_pan
        else:
            distraction_pan = 0
        main._distraction_pan = distraction_pan
        intent, g = run_p2_if1a(buf, fsm, geom, ts, raw_tracks, distraction_pan)
        _times["p2"].append((time.perf_counter()-_t0)*1000)
        _t0 = time.perf_counter()
        # ② 认知负荷：思考时眨眼变稀；"想到"时一次双连眨
        if geom.presence.current == "THINKING":
            _ld = float(geom.presence.debug_info()["dims"]["load"])
            blink.set_scale(0.95 - 0.55 * _ld)      # load 0.4→0.73  0.9→0.45
        else:
            blink.set_scale(1.0)
        if geom.presence.take_insight():
            blink.trigger(ts)
            _pending_blink = ts + 190
        if _pending_blink and ts >= _pending_blink:
            blink.trigger(ts, force=True)  # 双连眨第二下绕过 cooldown
            _pending_blink = 0
        lid = blink.update(ts)   # 1=睁, 0=闭
        # 镜像眨眼已停用：见 run_p2_if1a 里的说明
        # if g.get("_force_blink") and blink.state == "IDLE":
        #     blink.trigger(ts)
        g["lid_up"] = lid
        # 标定模式：手动偏移
        if calib_mode:
            g["body_pan_deg"] = round(g["body_pan_deg"] + calib_pan, 2)
            g["body_tilt_deg"] = round(g["body_tilt_deg"] + calib_tilt, 2)
        _pub_t0 = time.perf_counter()
        pub.send_string(json.dumps(
            build_render_payload(g, lid, blink, ts, cfg, intent["state"],
                                 presence=geom.presence.debug_info())))
        _times["pub"].append((time.perf_counter()-_pub_t0)*1000)
        msg = codec.decode_perception_tracks(buf)
        dt_ms = ts - prev_ts if prev_ts else 0
        prev_ts = ts
        lw.writerow([ts, dt_ms, len(msg['tracks']),
                     intent['state'], g.get('state',''), g['source'],
                     g.get('dims',{}).get('arousal',''), g.get('dims',{}).get('load',''),
                     g.get('dims',{}).get('valence',''), g.get('dims',{}).get('intent',''),
                     g.get('dims',{}).get('stability',''), g.get('dims',{}).get('fatigue',''),
                     g.get('_raw_pan', ''), g.get('_raw_tilt', ''),
                     round(g['body_pan_deg'],2), round(g['body_tilt_deg'],2),
                     round(g['eye_pan_deg'],2), round(g['eye_tilt_deg'],2),
                     round(g['offset_deg'],2), round(lid,3),
                     g.get('_tgt_u',''), g.get('_tgt_v',''), g.get('_eye_dist_px',''),
                     g.get('_pan_sp',''), g.get('_split_engaged',''),
                     g.get('_sac_inflight',''),
                     round(g.get('neck_roll_deg',0),2),
                     geom.presence._interlude_name or '',
                     1 if ts < getattr(geom, '_neck_hold_until', 0) else 0])
        if ts % 500 < 33:   # 2Hz 打印，别刷屏
          tid = intent.get('active_track_id')
          lost = intent.get('lost_stage') or ''
          # 打印当前脸中心和置信度
          face_info = ''
          if msg['tracks']:
            ft0 = msg['tracks'][0]
            u0 = (ft0['bbox'][0] + ft0['bbox'][2]) / 2
            v0 = (ft0['bbox'][1] + ft0['bbox'][3]) / 2
            face_info = f" u={u0:.0f},v={v0:.0f},conf={ft0['conf']:.2f}"
            # 诊断：scan 时同时打印 anchor 与 bbox 中心的偏差，用于判断关键点是否错位
            if g.get('_tgt_u') not in (None, ''):
                try:
                    au, av = float(g['_tgt_u']), float(g['_tgt_v'])
                    face_info += f" anchor=({au:.0f},{av:.0f}) d=({au-u0:+.0f},{av-v0:+.0f})"
                except Exception:
                    pass
          print(f"t={ts:5d}ms 脸数={len(msg['tracks'])}{face_info} 状态={intent['state']:18s} "
                f"track={tid} {lost:8s} "
                f"颈pan={g['body_pan_deg']:+5.1f}° 眼pan={g['eye_pan_deg']:+5.1f}° "
                f"会聚={g['offset_deg']:.2f}° 目标={g['source']} "
                f"det/lm/emb={pc.stats['det']}/{pc.stats['lm']}/{pc.stats['emb']}")
        # 预览每 UI_EVERY 帧刷一次：imshow 实测 ~13ms/次，占帧预算一半；
        # 人眼对 15fps 预览完全够用。waitKey 仍然每帧调用（收键 + 让窗口响应）。
        _frame_i += 1
        if _frame_i % _ui_every == 0:
            _ui_t0 = time.perf_counter()
            cv2.imshow("gaze", cv2.flip(bgr, 1))
            _times["ui"].append((time.perf_counter()-_ui_t0)*1000)
        raw = cv2.waitKey(1)
        if len(_times["cap"]) >= 100:
            import statistics
            med = {k: statistics.median(v[-100:]) if v else 0.0 for k,v in _times.items()}
            print(f"  [timing p50ms] cap={med['cap']:.1f} alg={med['alg']:.1f} p2={med['p2']:.1f} pub={med['pub']:.1f} ui={med['ui']:.1f}")
        key = raw & 0xFF
        if key == ord('q'): break
        if key == ord('t'):
            geom.presence.apply_preset("THINKING_HARD")
            print("[harness] THINKING_HARD preset on")
        if key == ord('r'):
            geom.presence.clear_preset()
            print("[harness] preset cleared")
        if key == ord('c'):
            calib_mode = not calib_mode
            print(f"\n=== 标定模式 {'ON' if calib_mode else 'OFF'} ===")
        # S 键保存已移除（calibration.load 只读 active.json，写 txt 无意义）
        if calib_mode:
            if raw in (81, 2424832): calib_pan -= 0.5
            if raw in (83, 2555904): calib_pan += 0.5
            if raw in (82, 2490368): calib_tilt -= 0.5
            if raw in (84, 2621440): calib_tilt += 0.5
        # 帧率封顶：MSMF 的 read() 是缓冲返回（~2ms），不封顶会空转处理重复帧。
        # 混合等待：剩余 >3ms 用 sleep，最后 <3ms 自旋，避免 sleep 粒度造成掉帧。
        if _fps_cap > 0:
            _target = _loop_t0 + _frame_ms / 1000.0
            while True:
                _left = (_target - time.perf_counter()) * 1000.0
                if _left <= 0:
                    break
                if _left > 3.0:
                    time.sleep((_left - 3.0) / 1000.0)
                else:
                    time.sleep(0)      # 让出时间片但不睡满，误差 <1ms
    cap.release(); cv2.destroyAllWindows()
    lf.close()
    try:
        geom.audio.close()
    except Exception:
        pass
    print("日志已保存")


if __name__ == "__main__":
    main()
