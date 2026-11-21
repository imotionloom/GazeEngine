"""
P2 决策进程 · 全链路（30Hz）
输入：IF-1a（ZMQ 收 P1）+ /system/control
输出：IF-5 JointCmd（11 通道，经 SoftLimiter + codec 序列化）
模块链：state_machine.GazeStateMachine → preset.PresetScheduler → geometry.GeometrySolver → joint_mapper → codec
"""
import os, zmq
def lerp(a, b, t): return a + (b - a) * t
from .state_machine import GazeStateMachine
from .geometry import GeometrySolver
from .joint_mapper import geometry_to_joints
from .preset import PresetScheduler, Preset
from .dynamics import BlinkMachine
from .driver import Driver
from .boot_choreography import BootChoreography
from . import calibration
from . import codec


class IntentDecision:
    def __init__(self, cfg=None):
        # 标定参数：唯一入口，构造时读一次并冻结（docs/CALIBRATION_SPEC.md §9）
        self.cfg = cfg if cfg is not None else calibration.load()
        self.calib_issues = calibration.apply(self.cfg)
        self.fsm = GazeStateMachine()
        self.geom = GeometrySolver(cfg=self.cfg)
        self.sch = PresetScheduler()
        self.blink = BlinkMachine()
        self._pending_blink = 0  # insight双连眨的第二下时间戳
        self.boot = BootChoreography()
        self.ctx = zmq.Context()
        self.prev_tracks = []
        self._seen_tracks = {}  # track_id -> last_seen_ts，5s 内不重复问候
        self._bound = False
        self._sudden_event = False  # /system/control 注入好奇事件
        for w in self.calib_issues:
            # GBK 控制台兼容：过滤非 GBK 字符（emoji），保留中文
            try:
                print(f"[P2][calib] {w}")
            except UnicodeEncodeError:
                safe = w.encode("gbk", errors="replace").decode("gbk")
                print(f"[P2][calib] {safe}")

    def start(self):
        """建 socket + bind/connect；可重复调用，多实例不抢端口。"""
        if self._bound:
            return
        self.sub = self.ctx.socket(zmq.SUB)
        self.sub.connect(os.environ.get("GAZE_IF1A", "tcp://127.0.0.1:5551"))
        self.sub.setsockopt(zmq.SUBSCRIBE, b"")
        self.ctrl = self.ctx.socket(zmq.SUB)
        self.ctrl.connect(os.environ.get("GAZE_CTRL", "tcp://127.0.0.1:5553"))
        self.ctrl.setsockopt(zmq.SUBSCRIBE, b"")
        self.pub_joint = self.ctx.socket(zmq.PUB)
        self.pub_joint.bind(os.environ.get("GAZE_IF5", "tcp://127.0.0.1:5555"))
        self.pub_hb = self.ctx.socket(zmq.PUB)
        self.pub_hb.bind(os.environ.get("GAZE_HB", "tcp://127.0.0.1:5560"))
        self._bound = True

    def on_control(self, ctrl_msg):
        """/system/control：preset / speed_scale / event_inject。脏消息保底不崩。"""
        try:
            if "preset" in ctrl_msg and ctrl_msg["preset"]:
                self.sch.set_manual(Preset(ctrl_msg["preset"]))
            ev = ctrl_msg.get("event_inject")
            if ev:
                # 接受字符串 ("curious"/"surprise") 或 dict ({"type":"surprise"})
                if isinstance(ev, str):
                    et = ev
                elif isinstance(ev, dict):
                    et = ev.get("type", "")
                else:
                    et = ""
                if et in ("curious", "surprise"):
                    self._sudden_event = True
        except Exception as e:
            print(f"[P2] 忽略非法控制消息: {e}")

    def on_tracks_buf(self, buf):
        """buf: IF-1a PerceptionTracks bytes。返回 (intent, g, joint_buf)。"""
        msg = codec.decode_perception_tracks(buf)
        ts = msg["ts_ms"]
        tracks = msg["tracks"]

        # 状态机（bbox 中心）
        fsm_tracks = [{"track_id": t["track_id"], "conf": t["conf"],
                       "bbox_center": (t["bbox"][0] + t["bbox"][2] / 2,
                                       t["bbox"][1] + t["bbox"][3] / 2)} for t in tracks]
        intent = self.fsm.update(fsm_tracks, ts)

        # 预设调度（greeting/attentive 等；v0 单人不挡路）
        has_person = len(tracks) > 0
        tid = tracks[0]["track_id"] if tracks else None
        # 5s 内见过的人不重复触发问候（防丢帧误判）
        new_person = False
        if tid is not None:
            last_seen = self._seen_tracks.get(tid, 0)
            new_person = (ts - last_seen) > 5000
            self._seen_tracks[tid] = ts
        # 清理超过 30s 未见的 track
        self._seen_tracks = {k: v for k, v in self._seen_tracks.items() if ts - v < 30000}
        # speaking_prob 从 tracks 读（IF-1b），PC 端无音频时为 0
        speaking_prob = 0.0
        for t in tracks:
            sp = t.get("anchors", {}).get("speaking_prob") or t.get("speaking_prob")
            if sp is not None:
                speaking_prob = max(speaking_prob, float(sp))
        p = self.sch.update(has_person, speaking_prob, new_person, self._sudden_event, ts)
        self._sudden_event = False
        self.prev_tracks = [t["track_id"] for t in tracks]

        # 预设锚点覆盖：meditate/drowsy 等 anchor_deg 不为 None 时，
        # 几何直接锁锚点，不跟人；同时把参数透传到 g 供 dynamics/joint_mapper 用
        intent["preset"] = p["preset"]
        intent["blink_scale"] = p["blink_scale"]
        intent["breath_amp"] = p["breath_amp"]
        intent["smooth"] = p["smooth"]
        if p["anchor_deg"] is not None:
            intent["anchor_deg"] = p["anchor_deg"]
            intent["neck_ratio"] = p["neck_ratio"]
        if p.get("scan_tilt_bias") is not None:
            intent["scan_tilt_bias"] = p["scan_tilt_bias"]

        # 几何：把 speaking_prob/identity 合并进 anchors 供 geometry 读取
        anchors_map = {}
        for t in tracks:
            anc = dict(t["anchors"])
            anc["speaking_prob"] = t.get("speaking_prob", anc.get("speaking_prob", 0.0))
            anc["identity"] = t.get("identity", anc.get("identity", "unknown"))
            anchors_map[t["track_id"]] = anc
        g = self.geom.solve(intent, anchors_map, ts)
        g.update({"preset": p["preset"], "blink_scale": p["blink_scale"],
                  "breath_amp": p["breath_amp"], "smooth": p["smooth"]})

        # dynamics：眨眼频率随 preset + presence fatigue + THINKING负荷
        fatigue = 0.5
        load = 0.5
        try:
            fatigue = self.geom.presence.fams[self.geom.presence.current]["dims"].fatigue
            load = self.geom.presence.fams[self.geom.presence.current]["dims"].load
        except Exception:
            pass
        blink_s = p["blink_scale"] * lerp(0.8, 1.6, fatigue)
        # THINKING时眨眼变稀（想得越深入，眨眼越少）
        if self.geom.presence.current == "THINKING":
            blink_s *= (0.95 - 0.55 * load)
        self.blink.set_scale(blink_s)
        # "想到一件事"的双连眨：第一下正常触发，190ms后第二下强制
        if self.geom.presence.take_insight():
            self.blink.trigger(ts)
            self._pending_blink = ts + 190
        if self._pending_blink and ts >= self._pending_blink:
            self.blink.trigger(ts, force=True)
            self._pending_blink = 0
        if g.get("_force_blink"):
            self.blink.trigger(ts, force=True)  # 双连眨第二次强制触发
        lid_up = self.blink.update(ts)
        # 呼吸已在 geometry._physiology_step 内叠加
        g["lid_up"] = lid_up

        # 关节映射 + 编码（IF-5）
        # vel_limit_dps是P3全局插补上限，取各通道最大值；per-channel限速在Driver里已做
        joints = geometry_to_joints(g, cfg=self.cfg)
        joint_buf = codec.encode_joint_cmd(joints, vel_limit_dps=Driver.MAX_DPS, ts_ms=ts)
        return intent, g, joint_buf


if __name__ == "__main__":
    import time, sys
    d = IntentDecision()
    d.start()
    # --no-boot: 跳过开机编排（调试用）
    use_boot = "--no-boot" not in sys.argv
    last_hb = 0
    last_joint_buf = None
    last_send = 0.0
    if use_boot:
        print("P2 决策进程启动 · 3秒保活期，请确认VRM已连接...")
        # 3秒保活：发中性姿态+心跳，防止P3看门狗超时
        neutral_g = {"body_pan_deg": 0.0, "body_tilt_deg": 0.0, "neck_roll_deg": 0.0,
                     "eye_pan_deg": 0.0, "eye_tilt_deg": 0.0, "offset_deg": 0.0,
                     "lid_up": 1.0, "source": "CONNECT"}
        wait_start = time.time()
        while time.time() - wait_start < 3.0:
            ts_now = int(time.time() * 1000)
            joints = geometry_to_joints(neutral_g, cfg=d.cfg)
            buf = codec.encode_joint_cmd(joints, vel_limit_dps=Driver.MAX_DPS, ts_ms=ts_now)
            d.pub_joint.send(buf)
            if ts_now - last_hb > 500:
                d.pub_hb.send(codec.encode_heartbeat("P2", True, ts_now))
                last_hb = ts_now
            time.sleep(0.033)
        d.boot.start()
        print("P2 开机编排播放中（17s，期间忽略追踪）")
    else:
        print("P2 决策进程启动 (IF-1a 收 / ctrl 收 / IF-5+hb 发, Ctrl+C 退出)")
    poller = zmq.Poller()
    poller.register(d.sub, zmq.POLLIN)
    poller.register(d.ctrl, zmq.POLLIN)
    boot_active = use_boot
    while True:
        socks = dict(poller.poll(timeout=20))
        if d.ctrl in socks:
            try:
                buf = d.ctrl.recv(zmq.NOBLOCK)
                if not boot_active:
                    d.on_control(codec.decode_system_control(buf))
            except Exception as e:
                print(f"[P2] 控制消息处理失败: {e}")
        # 开机编排：直接发关节目标，忽略 tracks
        if boot_active:
            g = d.boot.sample()
            if g is None:
                boot_active = False
                print("[P2] 开机编排结束，交回 FSM")
                # 排空空 tracks socket 缓冲
                while d.sub in dict(poller.poll(timeout=0)):
                    try: d.sub.recv(zmq.NOBLOCK)
                    except Exception: break
            else:
                joints = geometry_to_joints(g, cfg=d.cfg)
                joint_buf = codec.encode_joint_cmd(joints, vel_limit_dps=Driver.MAX_DPS,
                                                   ts_ms=int(time.time()*1000))
                d.pub_joint.send(joint_buf)
                last_joint_buf = joint_buf
                last_send = time.time()
                # 排空空 tracks socket 缓冲（不处理）
                while d.sub in dict(poller.poll(timeout=0)):
                    try: d.sub.recv(zmq.NOBLOCK)
                    except Exception: break
        elif d.sub in socks:
            try:
                buf = d.sub.recv(zmq.NOBLOCK)
                intent, g, joint_buf = d.on_tracks_buf(buf)
                last_joint_buf = joint_buf
                d.pub_joint.send(joint_buf)
                last_send = time.time()
            except Exception as e:
                print(f"[P2] tracks 处理失败: {e}")
        else:
            # 无新帧：30Hz 重发上一帧 joint_buf（P3 看门狗不触发）
            now = time.time()
            if last_joint_buf and (now - last_send) > 0.033:
                d.pub_joint.send(last_joint_buf)
                last_send = now
        now = time.time()
        if now - last_hb > 0.5:  # 2Hz
            last_hb = now
            d.pub_hb.send(codec.encode_heartbeat("P2", True, int(now * 1000)))
