"""
IF-4 关节映射：几何输出 → 11 通道舵机指令 dict（过仲裁软限位）
通道对应（CH0-10，RS485 总线，单位：度）：
  CH0 neck_yaw / CH1 neck_pitch / CH2 neck_roll
  CH3 eye_yaw_L / CH4 eye_yaw_R
  CH5 eye_pitch_L / CH6 eye_pitch_R
  CH7 lid_up_L / CH8 lid_up_R
  CH9 lid_low_L / CH10 lid_low_R  下眼睑，v0 固定 0
"""
from .arbitration import SoftLimiter


def _quantize(v, step):
    """按标定的可分辨步长量化：低于器件分辨率的尾数没有意义，还会抖动。"""
    try:
        step = float(step)
    except (TypeError, ValueError):
        return v
    if step <= 0.01:
        return v
    return round(round(v / step) * step, 3)


def geometry_to_joints(g, blink_open=None, cfg=None):
    """g: geometry.solve 输出；blink_open: 1=全开 0=全闭。
    若 g 里有 lid_up（BlinkMachine 输出），优先用它。
    → {joint_name: deg}"""
    if blink_open is None:
        blink_open = g.get("lid_up", 1.0)
    n_pan = g["body_pan_deg"]
    n_tilt = g["body_tilt_deg"]
    e_pan = g["eye_pan_deg"]
    e_tilt = g["eye_tilt_deg"]
    off = g["offset_deg"]          # 会聚：双眼内八

    lid = (1.0 - blink_open) * 90.0 + g.get("lid_closure_deg", 0.0)
    lid = max(0.0, min(90.0, lid))
    q = getattr(cfg, "cmd_quantum_deg", {}) or {}
    qe, qn = q.get("eye", 0.0), q.get("neck", 0.0)
    joints = {
        "neck_yaw":     _quantize(round(n_pan, 2), qn),
        "neck_pitch":   _quantize(round(n_tilt, 2), qn),
        "neck_roll":    _quantize(round(g.get("neck_roll_deg", 0.0), 2), qn),
        "eye_yaw_L":    _quantize(round(e_pan + off, 2), qe),
        "eye_yaw_R":    _quantize(round(e_pan - off, 2), qe),
        "eye_pitch_L":  _quantize(round(e_tilt, 2), qe),
        "eye_pitch_R":  _quantize(round(e_tilt, 2), qe),
        "lid_up_L":     round(lid, 2),
        "lid_up_R":     round(lid, 2),
        "lid_low_L":    0.0,
        "lid_low_R":    0.0,
    }
    # C3b: sign/scale/offset 逆变换（标定的是 forward: actual = sign*scale*cmd + offset）
    # 我们要 target=actual，所以 cmd = (target - offset) / (sign * scale)
    channels = getattr(cfg, "channels", {}) or {}
    if channels:
        for j in list(joints.keys()):
            ch = channels.get(j)
            if ch:
                s = ch.get("sign", 1)
                sc = ch.get("scale", 1.0)
                off3 = ch.get("offset", 0.0)
                if abs(sc) > 0.01:
                    # scale本身带符号（反向轴为负），直接除；sign字段仅作记录
                    joints[j] = (joints[j] - off3) / sc
    return SoftLimiter.clamp(joints)


# 自测
if __name__ == "__main__":
    g = {"body_pan_deg": 15.0, "body_tilt_deg": 5.0,
         "eye_pan_deg": 25.0, "eye_tilt_deg": 3.0, "offset_deg": 1.5}
    print("全开：", geometry_to_joints(g, 1.0))
    print("闭眼：", geometry_to_joints(g, 0.0))
