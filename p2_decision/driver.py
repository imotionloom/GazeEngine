"""L3 Driver：限速 + 限幅，所有分支统一过一道关口"""


class Driver:
    NECK_DPS = 50.0
    EYE_DPS  = 200.0
    LID_DPS  = 400.0
    ROLL_DPS = 200.0
    MAX_DPS  = 400.0  # = max(NECK, EYE, LID, ROLL)，P3全局插补上限

    def __init__(self):
        self._prev = {}
        self._ts = None

    def apply(self, g, ts_ms):
        if self._ts is None:
            self._ts = ts_ms
        dt = max(1e-3, (ts_ms - self._ts) / 1000.0)
        self._ts = ts_ms
        for key, dps in (("body_pan_deg", self.NECK_DPS),
                         ("body_tilt_deg", self.NECK_DPS),
                         ("eye_pan_deg", self.EYE_DPS),
                         ("eye_tilt_deg", self.EYE_DPS),
                         ("lid_closure_deg", self.LID_DPS),
                         ("neck_roll_deg", self.ROLL_DPS)):
            v = g.get(key, 0)
            prev = self._prev.get(key, v)
            step = dps * dt
            v = max(prev - step, min(prev + step, v))
            g[key] = round(v, 2)
            self._prev[key] = v
        # SoftLimiter 软限位
        try:
            from .arbitration import SoftLimiter
            clamped = SoftLimiter.clamp({
                "neck_yaw": g["body_pan_deg"], "neck_pitch": g["body_tilt_deg"],
                "neck_roll": g.get("neck_roll_deg", 0.0),
                "eye_yaw_L": g["eye_pan_deg"], "eye_yaw_R": g["eye_pan_deg"],
                "eye_pitch_L": g["eye_tilt_deg"], "eye_pitch_R": g["eye_tilt_deg"]})
            g["body_pan_deg"] = round(clamped["neck_yaw"], 2)
            g["body_tilt_deg"] = round(clamped["neck_pitch"], 2)
            g["neck_roll_deg"] = round(clamped["neck_roll"], 2)
            # 共享眼角取 L/R 中更紧的（离零更近的那个）
            def _tighter(a, b):
                return a if abs(a) < abs(b) else b
            g["eye_pan_deg"] = round(_tighter(clamped["eye_yaw_L"], clamped["eye_yaw_R"]), 2)
            g["eye_tilt_deg"] = round(_tighter(clamped["eye_pitch_L"], clamped["eye_pitch_R"]), 2)
            # _prev 必须存钳位后的值，否则松手时会多粘几帧限位
            self._prev["body_pan_deg"] = g["body_pan_deg"]
            self._prev["body_tilt_deg"] = g["body_tilt_deg"]
            self._prev["neck_roll_deg"] = g["neck_roll_deg"]
            self._prev["eye_pan_deg"] = g["eye_pan_deg"]
            self._prev["eye_tilt_deg"] = g["eye_tilt_deg"]
        except Exception:
            pass
        return g
