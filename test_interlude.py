"""
独立动作测试工具：直接播放插曲动作，不用跑整个系统。
用法：
  python test_interlude.py --action STRETCH
  python test_interlude.py --action SLOW_NOD --loop
  python test_interlude.py --action LOOK_UP_RECALL --speed 0.5

前提：先启动 ws_bridge 和 VRM 查看器
  python tools/ws_bridge.py
  浏览器打开 http://localhost:8898/3d/index.html
"""
import argparse, json, os, sys, time, zmq

# 把项目根目录加到 path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from p2_decision.presence import (
    STRETCH_KEYFRAMES, STRETCH_DUR,
    SLOW_NOD_KEYFRAMES, SLOW_NOD_DUR,
    LOOK_UP_RECALL_KEYFRAMES, LOOK_UP_RECALL_DUR,
    HEAD_COCK_KEYFRAMES, HEAD_COCK_DUR,
    GLANCE_DOWN_KEYFRAMES, GLANCE_DOWN_DUR,
    _interlude_sample,
)

ACTIONS = {
    "STRETCH": (STRETCH_KEYFRAMES, STRETCH_DUR),
    "SLOW_NOD": (SLOW_NOD_KEYFRAMES, SLOW_NOD_DUR),
    "LOOK_UP_RECALL": (LOOK_UP_RECALL_KEYFRAMES, LOOK_UP_RECALL_DUR),
    "HEAD_COCK": (HEAD_COCK_KEYFRAMES, HEAD_COCK_DUR),
    "GLANCE_DOWN": (GLANCE_DOWN_KEYFRAMES, GLANCE_DOWN_DUR),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--action", required=True, choices=list(ACTIONS.keys()),
                    help="要测试的动作")
    ap.add_argument("--loop", action="store_true", help="循环播放")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="播放速度，1=正常，0.5=慢一半，2=快一倍")
    ap.add_argument("--amplify", type=float, default=1.0,
                    help="头/眼幅度倍率，1=正常，2=两倍幅度")
    ap.add_argument("--blink", type=float, default=1.0,
                    help="闭眼程度倍率，0=不闭眼，1=正常，1.5=闭更紧")
    args = ap.parse_args()

    kfs, dur = ACTIONS[args.action]
    dur *= args.speed

    # ZMQ PUB
    ctx = zmq.Context()
    pub = ctx.socket(zmq.PUB)
    pub_addr = os.environ.get("GAZE_DEBUG_PUB", "tcp://127.0.0.1:5557")
    try:
        pub.bind(pub_addr)
    except zmq.ZMQError:
        print(f"端口 {pub_addr} 被占用，请先关掉 test_harness.py")
        return

    print(f"正在播放: {args.action}")
    print(f"时长: {dur:.1f}s  速度: {args.speed}x  幅度: {args.amplify}x  闭眼: {args.blink}x")
    print(f"ZMQ 发布: {pub_addr}")
    print("按 Ctrl+C 停止")
    print()

    # 先等 1 秒，让连接建立
    time.sleep(1.0)

    try:
        while True:
            t0 = time.time()
            frame = 0
            while True:
                elapsed = time.time() - t0
                if elapsed >= dur:
                    break

                # 采样动作偏移
                off = _interlude_sample(elapsed, kfs, dur)
                # off = (pan_off, tilt_off, epan_off, etilt_off, lid_off, roll_off)
                pan_off, tilt_off, epan_off, etilt_off, lid_off, roll_off = off
                pan_off *= args.amplify
                tilt_off *= args.amplify
                roll_off *= args.amplify
                epan_off *= args.amplify
                etilt_off *= args.amplify
                lid_off *= args.blink

                # 生成 payload（基线是居中睁眼，动作叠加在上面）
                ts_ms = int(elapsed * 1000)
                payload = {
                    "neck_yaw": pan_off,
                    "neck_pitch": tilt_off,
                    "neck_roll": roll_off,
                    "eye_yaw": -epan_off,  # 注意符号和 build_render_payload 一致
                    "eye_tilt": etilt_off,
                    "blink": 1.0,  # 一直睁眼
                    "blink_age_ms": None,
                    "blink_ms": 180,
                    "blink_close": 0.5,
                    "blink_hold": 0.1,
                    "lid_closure": lid_off * 95.0,
                    "look_scale": 1.0,
                    "source": "TEST_INTERLUDE",
                    "state": args.action,
                    "ts": ts_ms,
                }
                pub.send_string(json.dumps(payload))

                # 打印进度
                if frame % 30 == 0:
                    print(f"  t={elapsed:5.2f}s  pan={pan_off:+5.1f}  "
                          f"tilt={tilt_off:+5.1f}  roll={roll_off:+5.1f}")
                frame += 1

                time.sleep(1 / 30)  # 30fps

            if not args.loop:
                break
            print()
            print("--- 循环播放下一遍 ---")
            print()

    except KeyboardInterrupt:
        print("\n停止播放")
    finally:
        pub.close()
        ctx.term()


if __name__ == "__main__":
    main()
