"""
P2 决策 · 四状态注视状态机（真逻辑，可自测）
状态：LOOKING_AT_PERSON / GAZE_AWAY / RETURN / SCANNING
铁律：
  - 进入确认 ≈380-400ms：ENTRY_CONFIRM(230ms) + GREET_DELAY(150ms "注意到"延迟)
    注：旧规格写 200-250ms，是不含 greet 延迟的纯 P0；文档§2 应同步改成 380-400ms
  - 丢失三段式退出：0-167ms保持 → 167-667ms预测 → 667-1670ms回中 → >1670ms扫描
  - 身份锁：当前依赖 P1 tracker 的 track_id 稳定性（不切新 track 除非旧 track 丢失）；
    embedding 相似度门限（<0.85 不切人）待 IF-1b embedding 字段接入后实现（P1）
时钟全部外部注入（ts_ms），便于测试快进。
"""
import random
from enum import Enum


class GazeState(Enum):
    SCANNING = "SCANNING"
    LOOKING = "LOOKING_AT_PERSON"
    AWAY = "GAZE_AWAY"
    RETURN = "RETURN"


class FSMConfig:
    ENTRY_CONFIRM_MS = 230          # 进入确认窗口
    MIN_CONF = 0.5                  # 人脸置信度下限（侧脸/远景放宽）
    BBOX_JUMP_PX = 100              # 帧间 bbox 中心突变阈值（人群中放宽）
    RETURN_MS = 1800                # RETURN持续时间（眼睛先回人，脖子慢速转过去）
    GREET_DELAY_MS = 150            # 确认后延迟才锁定
    HOLD_MS = 167
    PREDICT_MS = 667
    RECENTER_MS = 1670


# 注意力模式层：不同模式下注视/瞟开时长完全不同
ATTENTION_MODES = {
    "FOCUSED": {
        "look_med": 18000, "away_med": 500,
        "min_dur": 30000, "max_dur": 60000,
        "nod_mult": 1.5, "blink_scale": 0.7,
    },
    "LISTENING": {
        "look_med": 25000, "away_med": 300,
        "min_dur": 20000, "max_dur": 40000,
        "nod_mult": 2.0, "blink_scale": 0.5,
    },
    "NEUTRAL": {
        "look_med": 10000, "away_med": 1200,
        "min_dur": 20000, "max_dur": 40000,
        "nod_mult": 1.0, "blink_scale": 1.0,
    },
    "DISTRACTED": {
        "look_med": 4000, "away_med": 2500,
        "min_dur": 15000, "max_dur": 30000,
        "nod_mult": 0.3, "blink_scale": 1.3,
    },
    "BORED": {
        "look_med": 2500, "away_med": 3000,
        "min_dur": 10000, "max_dur": 20000,
        "nod_mult": 0.0, "blink_scale": 1.5,
    },
}


class GazeStateMachine:
    def __init__(self, cfg=None):
        if cfg is None:
            cfg = FSMConfig()
        self.cfg = cfg
        self.state = GazeState.SCANNING
        self.state_entered = 0
        self.active_track = None          # 当前锁定 track_id
        self._lost_since_lock = None      # track丢失锁定计时
        # 进入确认缓冲
        self.candidate = None
        self.lost_since = None
        import os
        _seed_env = os.environ.get("GAZE_SEED", "")
        try:
            _seed = int(_seed_env) if _seed_env else None
        except ValueError:
            _seed = None  # 非数字环境变量不崩
        self._rng = random.Random(_seed)
        self._look_target_ms = 0
        self._away_target_ms = 0
        self._greet_hold_until = 0
        # 注意力模式
        self.attention_mode = "NEUTRAL"
        self._mode_entered = 0
        m0 = ATTENTION_MODES[self.attention_mode]
        self._mode_until = self._rng.randint(m0["min_dur"], m0["max_dur"])
        self._look_count = 0          # 注视次数计数（超长outlier用）
        self._last_look_ms = 0        # 上次注视时长（负相关用）
        self._pending_double_glance = False  # 连续瞟开

    def _set_state(self, s, ts):
        # 修复2：最小驻留，防止状态频繁切换断片
        if self.state != s:
            if self.state == GazeState.LOOKING and ts - self.state_entered < 800:
                return  # LOOKING 至少 800ms 才允许退出
            if self.state == GazeState.SCANNING and ts - self.state_entered < 500:
                return  # SCANNING 至少 500ms 才允许退出
        old = self.state.value
        self.state = s
        self.state_entered = ts
        if s == GazeState.LOOKING:
            import math
            mode = ATTENTION_MODES[self.attention_mode]
            med = mode["look_med"]
            sigma = 0.4
            duration = math.exp(self._rng.gauss(math.log(med), sigma))
            # 负相关：上次注视<5s → 本次×2.5；上次>15s → 本次×0.6（首次不适用）
            if self._last_look_ms > 0:
                if self._last_look_ms < 5000:
                    duration *= 2.5
                elif self._last_look_ms > 15000:
                    duration *= 0.6
            # 超长outlier：每5-8次注视来一次30-60秒
            self._look_count += 1
            if self._look_count >= self._rng.randint(5, 8):
                duration *= 3.0
                self._look_count = 0
            self._look_target_ms = max(2000, min(60000, duration))
            self._last_look_ms = self._look_target_ms
        elif s == GazeState.AWAY:
            import math
            mode = ATTENTION_MODES[self.attention_mode]
            med = mode["away_med"]
            sigma = 0.4
            duration = math.exp(self._rng.gauss(math.log(med), sigma))
            # 连续瞟开：第二次更短
            if self._pending_double_glance:
                duration *= 0.6
                self._pending_double_glance = False
            self._away_target_ms = max(200, min(5000, duration))

    def _confirm_candidate(self, tid, conf, bbox_c, ts):
        """同 track_id 持续 ENTRY_CONFIRM_MS + 高 conf + bbox 不突变 才算真目标"""
        if self.candidate and self.candidate[0] == tid:
            _, since, last_c = self.candidate
            jump = abs(bbox_c[0] - last_c[0]) + abs(bbox_c[1] - last_c[1])
            if ts - since >= self.cfg.ENTRY_CONFIRM_MS and conf >= self.cfg.MIN_CONF and jump < self.cfg.BBOX_JUMP_PX:
                return True
            self.candidate = (tid, since, bbox_c)
        else:
            self.candidate = (tid, ts, bbox_c)
        return False

    def update(self, tracks, ts):
        """tracks: [{track_id, conf, bbox_center:(x,y)}]；返回 intent dict。"""
        # ---- 1. 丢失处理 ----
        if not tracks:
            if self.lost_since is None:
                self.lost_since = ts
            t = ts - self.lost_since
            if t < self.cfg.HOLD_MS and self.active_track is not None:
                return self._intent(ts, lost_stage="hold")           # 保持最后目标
            if t < self.cfg.PREDICT_MS:
                return self._intent(ts, lost_stage="predict")        # Kalman预测（几何层做）
            if t < self.cfg.RECENTER_MS:
                if self.state != GazeState.SCANNING:
                    self._set_state(GazeState.SCANNING, ts)
                self.candidate = None   # 丢失超时，清空候选，重入需重新确认
                return self._intent(ts, lost_stage="recenter")
            self._set_state(GazeState.SCANNING, ts)
            self.active_track = None
            self.candidate = None
            return self._intent(ts, lost_stage="scan")

        self.lost_since = None

        # ---- 2. 进入确认 → LOOKING（仅 SCANNING 入口；AWAY 走自己的计时器） ----
        target = tracks[0]   # 已由 P1 窗口平均排过序，最近者优先
        if self.state == GazeState.SCANNING:
            if self._confirm_candidate(target["track_id"], target["conf"],
                                       target["bbox_center"], ts):
                # 确认成功后等待 GREET_DELAY_MS 再锁定（模拟"注意到"延迟，替代概率丢帧）
                if self._greet_hold_until == 0:
                    self._greet_hold_until = ts + self.cfg.GREET_DELAY_MS
                if ts >= self._greet_hold_until:
                    self.active_track = target["track_id"]
                    self._last_bbox = target["bbox_center"]
                    self._set_state(GazeState.LOOKING, ts)
                    self._greet_hold_until = 0
            else:
                self._greet_hold_until = 0
            return self._intent(ts)

        # ---- 0. 注意力模式切换（持续时间到了随机换模式） ----
        if ts > self._mode_until:
            transitions = {
                "FOCUSED": ["NEUTRAL", "LISTENING"],
                "LISTENING": ["FOCUSED", "NEUTRAL"],
                "NEUTRAL": ["FOCUSED", "DISTRACTED"],
                "DISTRACTED": ["NEUTRAL", "BORED"],
                "BORED": ["NEUTRAL", "DISTRACTED"],
            }
            choices = transitions.get(self.attention_mode, ["NEUTRAL"])
            self.attention_mode = self._rng.choice(choices)
            m = ATTENTION_MODES[self.attention_mode]
            self._mode_entered = ts
            self._mode_until = ts + self._rng.randint(m["min_dur"], m["max_dur"])
            # 如果正在LOOKING，按新模式重算注视时长
            if self.state == GazeState.LOOKING:
                import math
                duration = math.exp(self._rng.gauss(math.log(m["look_med"]), 0.4))
                self._look_target_ms = max(2000, min(60000, duration))
                self._last_look_ms = self._look_target_ms  # 同步，避免负相关基于旧值
                self._look_count = 0  # 重置outlier计数

        # ---- 3. LOOKING 内部计时 ----
        if self.state == GazeState.LOOKING:
            # Bug fix: LOOKING 状态下如果 active_track 丢了（tracker 换 id 或暂时没检测到），
            # 尝试重新确认新 track；但无论确认成功与否，都必须继续走下面的过期计时，
            # 否则低置信度误检会导致永久 LOOKING + frame_center 僵直。
            track_ids = {t["track_id"] for t in tracks}
            if self.active_track not in track_ids:
                # track丢失：1.5秒内不切换，保持锁定
                if self._lost_since_lock is None:
                    self._lost_since_lock = ts
                if ts - self._lost_since_lock < 1500:
                    return self._intent(ts)
                # 1.5秒后仍无新track确认（低置信度），强制进AWAY
                if not tracks:
                    self._set_state(GazeState.AWAY, ts)
                    return self._intent(ts)
                # Bug fix: 选最接近旧位置的 track，而不是 tracks[0]，避免多人同框跳错
                if hasattr(self, "_last_bbox") and self._last_bbox:
                    target = min(tracks, key=lambda t:
                        (t["bbox_center"][0]-self._last_bbox[0])**2 +
                        (t["bbox_center"][1]-self._last_bbox[1])**2)
                if self._confirm_candidate(target["track_id"], target["conf"],
                                           target["bbox_center"], ts):
                    self.active_track = target["track_id"]
                    self._last_bbox = target["bbox_center"]
                    self._lost_since_lock = None
                    self.candidate = None
                    self._set_state(GazeState.LOOKING, ts)
                elif self.candidate and ts - self.candidate[1] > 500:
                    # 候选确认超过500ms仍失败（低置信度），强制进AWAY
                    self._set_state(GazeState.AWAY, ts)
            else:
                self._lost_since_lock = None
                # 正常跟踪时记录当前 bbox 位置，供换人时匹配
                for t in tracks:
                    if t["track_id"] == self.active_track:
                        self._last_bbox = t["bbox_center"]
                        break
            held = ts - self.state_entered
            if held > self._look_target_ms:
                self._set_state(GazeState.AWAY, ts)      # 随机移开
            return self._intent(ts)

        # ---- 4. AWAY → RETURN（20%连续瞟开；其余80%里90%回人，净回人72%） ----
        if self.state == GazeState.AWAY:
            if ts - self.state_entered > self._away_target_ms:
                if self._rng.random() < 0.20 and not self._pending_double_glance:
                    # 连续瞟开：立刻再瞟一次
                    self._pending_double_glance = True
                    self._set_state(GazeState.AWAY, ts)
                elif self._rng.random() < 0.9:
                    self._set_state(GazeState.RETURN, ts)
                else:
                    self._set_state(GazeState.AWAY, ts)
            return self._intent(ts)

        # ---- 5. RETURN：持续 RETURN_MS（眼睛先回人，脖子慢速转过去），到时再回 LOOKING ----
        if self.state == GazeState.RETURN:
            if ts - self.state_entered >= self.cfg.RETURN_MS:
                self._set_state(GazeState.LOOKING, ts)
            return self._intent(ts)

        return self._intent(ts)

    def _intent(self, ts, lost_stage=None):
        m = ATTENTION_MODES[self.attention_mode]
        return {
            "state": self.state.value,
            "active_track_id": self.active_track,
            "scan": self.state == GazeState.SCANNING,
            "lost_stage": lost_stage,
            "ts_ms": ts,
            "attention_mode": self.attention_mode,
            "nod_mult": m["nod_mult"],
            "blink_scale": m["blink_scale"],
        }


# ---------------- 自测 ----------------
if __name__ == "__main__":
    fsm = GazeStateMachine()
    t = 0
    print("== 启动扫描 ==")
    for _ in range(5):
        print(f"t={t}ms", fsm.update([], t)["state"]); t += 33

    print("\n== 人脸出现（需230ms确认） ==")
    face = {"track_id": 7, "conf": 0.9, "bbox_center": (320, 240)}
    for _ in range(10):
        r = fsm.update([face], t)
        print(f"t={t}ms state={r['state']:18s} track={r['active_track_id']}")
        t += 33

    print("\n== 人脸丢失三段式 ==")
    for _ in range(70):
        r = fsm.update([], t)
        if _ % 10 == 0:
            print(f"t={t}ms state={r['state']:18s} lost_stage={r['lost_stage']}")
        t += 33
