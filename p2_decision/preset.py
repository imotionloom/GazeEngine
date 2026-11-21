"""
P2 决策 · 行为预设调度（真逻辑，可自测）
层级：强事件 > 手动预设 > auto调度 > 自主状态机
- preset="auto"（默认）：按场景自己切 idle/drowsy/meditate/attentive/greeting/curious
- preset="meditate" 等：手动锁定，auto 退出；preset_anchor 走配置 JSON
"""
from enum import Enum


class Preset(Enum):
    AUTO = "auto"
    MEDITATE = "meditate"
    ATTENTIVE = "attentive"
    GREETING = "greeting"
    DROWSY = "drowsy"
    CURIOUS = "curious"
    IDLE = "idle"


# 预设参数包
# - anchor_deg: 固定注视方向 (pan,tilt)；None=跟随人
# - neck_ratio: 锚点中由颈承担的比例（其余给眼）；避免"眼球上翻"
# - scan_tilt_bias: SCANNING 时叠加在 tilt 上的偏置（drowsy 半垂眼）
PRESET_PARAMS = {
    Preset.MEDITATE: {"anchor_deg": (12, 10), "neck_ratio": 0.75, "scan_tilt_bias": None,
                      "blink_scale": 0.6, "breath_amp": 1.5, "smooth": 0.05},
    Preset.ATTENTIVE: {"anchor_deg": None, "neck_ratio": 0.5, "scan_tilt_bias": None,
                       "blink_scale": 0.8, "breath_amp": 1.0, "smooth": 0.08},
    Preset.GREETING: {"anchor_deg": None, "neck_ratio": 0.5, "scan_tilt_bias": None,
                      "blink_scale": 1.0, "breath_amp": 1.0, "smooth": 0.1},
    Preset.DROWSY: {"anchor_deg": None, "neck_ratio": 0.7, "scan_tilt_bias": -5.0,
                    "blink_scale": 0.5, "breath_amp": 0.6, "smooth": 0.03},
    Preset.CURIOUS: {"anchor_deg": None, "neck_ratio": 0.5, "scan_tilt_bias": None,
                     "blink_scale": 1.0, "breath_amp": 1.2, "smooth": 0.12},
    Preset.IDLE: {"anchor_deg": None, "neck_ratio": 0.5, "scan_tilt_bias": None,
                  "blink_scale": 0.9, "breath_amp": 1.0, "smooth": 0.05},
}


class PresetScheduler:
    def __init__(self):
        self.manual = Preset.AUTO          # 当前生效模式
        self.current = Preset.IDLE         # auto 调度当前姿态
        self.no_person_since = None
        self.last_greeting_track = None
        self.curious_until = 0

    def set_manual(self, p: Preset):
        """/system/control 调用。null/auto = 交还 auto。"""
        self.manual = p

    def update(self, has_person: bool, speaking_prob: float, is_new_person: bool,
                sudden_event: bool, ts_ms: int):
        """返回当前生效预设 + 参数（合并手动锚点覆盖）。"""
        if sudden_event:
            self.curious_until = ts_ms + 3000

        if self.manual != Preset.AUTO:
            return self._pack(self.manual)   # 手动锁定，auto 退出

        # ---- auto 调度 ----
        if ts_ms < self.curious_until:
            self.current = Preset.CURIOUS
        elif not has_person:
            if self.no_person_since is None:
                self.no_person_since = ts_ms
            idle_t = ts_ms - self.no_person_since
            if idle_t > 120_000:
                self.current = Preset.DROWSY
            elif idle_t > 30_000:
                self.current = Preset.IDLE
        else:
            self.no_person_since = None
            if is_new_person:
                self.current = Preset.GREETING
                self._greeting_since = ts_ms
            elif self.current == Preset.GREETING and ts_ms - getattr(self, "_greeting_since", 0) > 3000:
                self.current = Preset.ATTENTIVE      # 问候 3s 后自动转倾听
            if speaking_prob > 0.5:
                self.current = Preset.ATTENTIVE
        return self._pack(self.current)

    def _pack(self, p: Preset):
        return {"preset": p.value, **PRESET_PARAMS[p]}


# ---------------- 自测 ----------------
if __name__ == "__main__":
    sch = PresetScheduler()
    print("== auto 调度：无人 30s→idle，120s→drowsy ==")
    print("t=0    :", sch.update(False, 0.0, False, False, 0)["preset"])
    print("t=31s  :", sch.update(False, 0.0, False, False, 31_000)["preset"])
    print("t=121s :", sch.update(False, 0.0, False, False, 121_000)["preset"])
    print("新人出现:", sch.update(True, 0.2, True, False, 122_000)["preset"])
    print("对方说话:", sch.update(True, 0.8, False, False, 123_000)["preset"])
    print("突发事件:", sch.update(True, 0.0, False, True, 124_000)["preset"])
    print("t=125s  :", sch.update(True, 0.0, False, False, 125_000)["preset"])

    print("\n== 手动锁定 meditate ==")
    sch.set_manual(Preset.MEDITATE)
    r = sch.update(False, 0.0, False, False, 126_000)
    print("锁定后 preset=%s anchor=%s blink_scale=%s" % (r["preset"], r["anchor_deg"], r["blink_scale"]))
    sch.set_manual(Preset.AUTO)
    print("交还 auto:", sch.update(False, 0.0, False, False, 127_000)["preset"])
