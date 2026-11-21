"""
调试 CLI：向 /system/control (5553) 发一条 preset 切换消息。
用法：
  python tools/send_control.py meditate
  python tools/send_control.py auto
  python tools/send_control.py attentive
"""
import os, sys, time, zmq
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from p2_decision import codec

if len(sys.argv) < 2:
    print(__doc__)
    sys.exit(1)

preset = sys.argv[1]
ctx = zmq.Context()
pub = ctx.socket(zmq.PUB)
pub.bind(os.environ.get("GAZE_CTRL", "tcp://127.0.0.1:5553"))
time.sleep(0.3)   # PUB 连上来需要一点时间
buf = codec.encode_system_control({"preset": preset, "mode": "auto"},
                                  int(time.time() * 1000))
pub.send(buf)
print(f"[ctrl] 已发 preset={preset} → 5553")
