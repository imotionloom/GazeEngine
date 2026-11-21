"""
P2 编解码层：dict ↔ protobuf（gaze_messages_pb2）
P1/P2/P3 全部走同一套编码。时间戳 v=1。
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proto"))
import gaze_messages_pb2 as pb

SEQ = 0


def _header(ts_ms):
    global SEQ
    SEQ += 1
    return pb.MsgHeader(v=1, ts_ms=ts_ms, seq=SEQ)


# ---------- IF-1a：PerceptionTracks ----------
def encode_perception_tracks(tracks_dict, ts_ms, image_quality="good"):
    m = pb.PerceptionTracks()
    m.header.CopyFrom(_header(ts_ms))
    m.image_quality = pb.GOOD if image_quality == "good" else pb.POOR
    for t in tracks_dict:
        pt = m.tracks.add()
        pt.track_id = t["track_id"]
        pt.bbox_px.extend(t.get("bbox", [0, 0, 0, 0]))
        pt.anchors.eye_L.u, pt.anchors.eye_L.v = int(t["anchors"]["eye_L"][0]), int(t["anchors"]["eye_L"][1])
        pt.anchors.eye_R.u, pt.anchors.eye_R.v = int(t["anchors"]["eye_R"][0]), int(t["anchors"]["eye_R"][1])
        pt.anchors.nose.u,  pt.anchors.nose.v  = int(t["anchors"]["nose"][0]), int(t["anchors"]["nose"][1])
        pt.anchors.mouth.u, pt.anchors.mouth.v = int(t["anchors"]["mouth"][0]), int(t["anchors"]["mouth"][1])
        pt.face_confidence = t.get("conf", 0.9)
        pt.distance.est_m = t["distance"]["est_m"]
        pt.distance.dot_mps = t["distance"]["dot_mps"]
        pt.distance.confidence = t.get("dist_conf", 0.8)
        pt.speaking_prob = t.get("speaking_prob", 0.0)
        pt.identity = t.get("identity", "unknown")
    return m.SerializeToString()


def decode_perception_tracks(buf):
    m = pb.PerceptionTracks()
    m.ParseFromString(buf)
    return {
        "ts_ms": m.header.ts_ms, "seq": m.header.seq,
        "image_quality": "good" if m.image_quality == pb.GOOD else "poor",
        "tracks": [{
            "track_id": t.track_id,
            "bbox": list(t.bbox_px),
            "anchors": {
                "eye_L": (t.anchors.eye_L.u, t.anchors.eye_L.v),
                "eye_R": (t.anchors.eye_R.u, t.anchors.eye_R.v),
                "nose":  (t.anchors.nose.u, t.anchors.nose.v),
                "mouth": (t.anchors.mouth.u, t.anchors.mouth.v),
            },
            "conf": t.face_confidence,
            "distance": {"est_m": t.distance.est_m, "dot_mps": t.distance.dot_mps},
            "speaking_prob": t.speaking_prob,
            "identity": t.identity or "unknown",
        } for t in m.tracks],
    }


# ---------- IF-2：Intent ----------
_STATE_ENUM = {"LOOKING_AT_PERSON": pb.LOOKING_AT_PERSON, "GAZE_AWAY": pb.GAZE_AWAY,
               "RETURN": pb.RETURN, "SCANNING": pb.SCANNING}
_POINT_ENUM = {"eye_midpoint": pb.EYE_MIDPOINT, "nose": pb.NOSE, "mouth": pb.MOUTH,
               "frame_center": pb.FRAME_CENTER, "scan": pb.SCAN, "away": pb.AWAY,
               "preset": pb.PRESET}


def encode_intent(intent, ts_ms):
    m = pb.Intent()
    m.header.CopyFrom(_header(ts_ms))
    m.state = _STATE_ENUM.get(intent["state"], pb.SCANNING)
    m.active_track_id = intent.get("active_track_id") or 0
    # FSM 不直接产 source/looking_ratio，按 state 推导
    state = intent["state"]
    if state == "LOOKING_AT_PERSON":
        m.gaze_point = _POINT_ENUM.get(intent.get("source", "eye_midpoint"), pb.EYE_MIDPOINT)
        m.looking_ratio = intent.get("looking_ratio", 0.8)
    elif state in ("SCANNING", "SEEKING"):
        m.gaze_point = _POINT_ENUM.get(intent.get("source", "frame_center"), pb.FRAME_CENTER)
        m.looking_ratio = intent.get("looking_ratio", 0.2)
    else:
        m.gaze_point = _POINT_ENUM.get(intent.get("source", "frame_center"), pb.FRAME_CENTER)
        m.looking_ratio = intent.get("looking_ratio", 0.5)
    if intent.get("scan_dir"):
        m.scan_dir.pan_deg, m.scan_dir.tilt_deg = intent["scan_dir"]
    return m.SerializeToString()


def decode_intent(buf):
    m = pb.Intent()
    m.ParseFromString(buf)
    _STATE_R = {v: k for k, v in _STATE_ENUM.items()}
    _POINT_R = {v: k for k, v in _POINT_ENUM.items()}
    out = {
        "ts_ms": m.header.ts_ms, "seq": m.header.seq,
        "state": _STATE_R.get(m.state, "SCANNING"),
        "active_track_id": m.active_track_id,
        "source": _POINT_R.get(m.gaze_point, "frame_center"),
        "looking_ratio": m.looking_ratio,
    }
    if m.HasField("scan_dir"):
        out["scan_dir"] = (m.scan_dir.pan_deg, m.scan_dir.tilt_deg)
    return out


# ---------- IF-3：GazeTarget ----------
def encode_gaze_target(g, ts_ms):
    m = pb.GazeTarget()
    m.header.CopyFrom(_header(ts_ms))
    m.body_pan_deg = g["body_pan_deg"]
    m.body_tilt_deg = g["body_tilt_deg"]
    m.offset_deg = g["offset_deg"]
    m.distance_m = g["distance_m"]
    m.source = _POINT_ENUM.get(g.get("source", "frame_center"), pb.FRAME_CENTER)
    m.confidence = g.get("confidence", 0.9)
    return m.SerializeToString()


def decode_gaze_target(buf):
    m = pb.GazeTarget()
    m.ParseFromString(buf)
    _POINT_R = {v: k for k, v in _POINT_ENUM.items()}
    return {
        "ts_ms": m.header.ts_ms,
        "body_pan_deg": m.body_pan_deg, "body_tilt_deg": m.body_tilt_deg,
        "offset_deg": m.offset_deg, "distance_m": m.distance_m,
        "source": _POINT_R.get(m.source, "frame_center"),
        "confidence": m.confidence,
    }


# ---------- IF-1b：TrackMeta（事件流） ----------
_EVENT_ENUM = {"NEW_TRACK": pb.NEW_TRACK, "TRACK_LOST": pb.TRACK_LOST, "UPDATE": pb.UPDATE}

def encode_track_meta(meta, ts_ms):
    m = pb.TrackMeta()
    m.header.CopyFrom(_header(ts_ms))
    m.event = _EVENT_ENUM.get(meta.get("event", "UPDATE"), pb.UPDATE)
    m.track_id = meta["track_id"]
    emb = meta.get("embedding", b"")
    if hasattr(emb, "tobytes"):           # numpy ndarray → bytes
        emb = emb.astype("float32").tobytes()
    m.embedding = emb
    m.headpose_deg.yaw = meta.get("headpose", {}).get("yaw", 0.0)
    m.headpose_deg.pitch = meta.get("headpose", {}).get("pitch", 0.0)
    m.headpose_deg.roll = meta.get("headpose", {}).get("roll", 0.0)
    m.speaking_prob = meta.get("speaking_prob", 0.0)
    m.emotion = meta.get("emotion", "")
    return m.SerializeToString()


# ---------- IF-4：JointTargetRaw（动力学 → 仲裁） ----------
def encode_joint_target_raw(joints, ts_ms, source="rule", blink=False, confidence=0.9):
    m = pb.JointTargetRaw()
    m.header.CopyFrom(_header(ts_ms))
    m.source = pb.RULE if source == "rule" else pb.RGT
    for k, v in joints.items():
        setattr(m.joints, k, v)
    m.blink_trigger = blink
    m.confidence = confidence
    return m.SerializeToString()


# ---------- IF-6：JointState（执行 → 全链） ----------
def encode_joint_state(joints_actual, faults, ts_ms):
    m = pb.JointState()
    m.header.CopyFrom(_header(ts_ms))
    for k, v in joints_actual.items():
        setattr(m.joints_actual, k, v)
    m.fault.overtemp = faults.get("overtemp", False)
    m.fault.stall = faults.get("stall", False)
    return m.SerializeToString()


# ---------- /system/control + /heartbeat ----------
def encode_system_control(ctrl, ts_ms):
    m = pb.SystemControl()
    m.header.CopyFrom(_header(ts_ms))
    m.mode = {"auto": pb.MODE_AUTO, "demo": pb.DEMO, "freeze": pb.FREEZE}.get(ctrl.get("mode", "auto"), pb.MODE_AUTO)
    m.speed_scale = ctrl.get("speed_scale", 1.0)
    m.event_inject = ctrl.get("event_inject", "")
    m.actuator_backend = {"servo": pb.SERVO, "digital_human": pb.DIGITAL_HUMAN, "sim": pb.SIM}.get(ctrl.get("actuator_backend", "sim"), pb.SIM)
    m.preset = ctrl.get("preset", "auto")
    if "preset_anchor" in ctrl:
        m.preset_anchor.pan_deg = ctrl["preset_anchor"][0]
        m.preset_anchor.tilt_deg = ctrl["preset_anchor"][1]
    m.preset_hold_ms = ctrl.get("preset_hold_ms", 0)
    m.recalibrate = ctrl.get("recalibrate", False)
    return m.SerializeToString()


def encode_heartbeat(module, alive, ts_ms):
    m = pb.Heartbeat()
    m.header.CopyFrom(_header(ts_ms))
    m.module = module
    m.alive = alive
    return m.SerializeToString()


# ---------- IF-5：JointCmd ----------
def encode_joint_cmd(joints_dict, vel_limit_dps, ts_ms):
    m = pb.JointCmd()
    m.header.CopyFrom(_header(ts_ms))
    for k, v in joints_dict.items():
        setattr(m.joints, k, v)
    m.vel_limit_dps = vel_limit_dps
    return m.SerializeToString()


def decode_joint_cmd(buf):
    m = pb.JointCmd()
    m.ParseFromString(buf)
    return {
        "ts_ms": m.header.ts_ms, "seq": m.header.seq,
        "vel_limit_dps": m.vel_limit_dps,
        "joints": {f.name: getattr(m.joints, f.name) for f in m.joints.DESCRIPTOR.fields},
    }


# ---------- IF-1b：TrackMeta decode ----------
def decode_track_meta(buf):
    m = pb.TrackMeta()
    m.ParseFromString(buf)
    return {
        "ts_ms": m.header.ts_ms, "track_id": m.track_id,
        "embedding": m.embedding, "speaking_prob": m.speaking_prob,
    }


# ---------- IF-6：JointState decode ----------
def decode_joint_state(buf):
    m = pb.JointState()
    m.ParseFromString(buf)
    return {
        "ts_ms": m.header.ts_ms,
        "joints_actual": {f.name: getattr(m.joints_actual, f.name) for f in m.joints_actual.DESCRIPTOR.fields},
        "overtemp": m.fault.overtemp, "stall": m.fault.stall,
    }


# ---------- /system/control + /heartbeat decode ----------
_MODE_ENUM = {"auto": pb.MODE_AUTO, "demo": pb.DEMO, "freeze": pb.FREEZE}
_MODE_R = {v: k for k, v in _MODE_ENUM.items()}
_BACKEND_ENUM = {"servo": pb.SERVO, "digital_human": pb.DIGITAL_HUMAN, "sim": pb.SIM}
_BACKEND_R = {v: k for k, v in _BACKEND_ENUM.items()}


def decode_system_control(buf):
    m = pb.SystemControl()
    m.ParseFromString(buf)
    return {
        "ts_ms": m.header.ts_ms, "preset": m.preset,
        "mode": _MODE_R.get(m.mode, "auto"),
        "speed_scale": m.speed_scale, "event_inject": m.event_inject,
        "actuator_backend": _BACKEND_R.get(m.actuator_backend, "sim"),
        "preset_anchor": {"pan": m.preset_anchor.pan_deg, "tilt": m.preset_anchor.tilt_deg},
        "preset_hold_ms": m.preset_hold_ms,
        "recalibrate": m.recalibrate,
    }


def decode_heartbeat(buf):
    m = pb.Heartbeat()
    m.ParseFromString(buf)
    return {"ts_ms": m.header.ts_ms, "module": m.module, "alive": m.alive}


# ---------------- 自测 ----------------
if __name__ == "__main__":
    sample = [{
        "track_id": 7, "bbox": [260, 180, 120, 160],
        "anchors": {"eye_L": (280, 220), "eye_R": (340, 220),
                    "nose": (310, 245), "mouth": (310, 275)},
        "conf": 0.92, "distance": {"est_m": 1.2, "dot_mps": -0.3},
    }]
    buf = encode_perception_tracks(sample, 1000)
    print(f"IF-1a 编码 {len(buf)} bytes")
    back = decode_perception_tracks(buf)
    t = back["tracks"][0]
    print(f"解码回 track={t['track_id']} eye_L={t['anchors']['eye_L']} d={t['distance']['est_m']}")

    intent = {"state": "LOOKING_AT_PERSON", "active_track_id": 7,
              "source": "eye_midpoint", "looking_ratio": 0.8}
    buf2 = encode_intent(intent, 1001)
    print(f"IF-2 Intent 编码 {len(buf2)} bytes")

    g = {"body_pan_deg": 0.0, "body_tilt_deg": 0.0, "offset_deg": 1.8,
         "distance_m": 1.0, "source": "eye_midpoint"}
    buf3 = encode_gaze_target(g, 1002)
    print(f"IF-3 GazeTarget 编码 {len(buf3)} bytes")
    print("全部编解码往返成功")
