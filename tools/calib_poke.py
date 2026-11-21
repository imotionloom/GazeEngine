"""标定联动自检：一条命令判断"眼睛不动"卡在哪一环。

检查顺序：
  1. 扫 9100–9110，找到标定桥的 WS；
  2. 发一条 calib.command，看桥是否回 "未知命令"（= 旧版桥）；
  3. 若传 sweep，则做一圈扫视，直接观察数字人眼睛。

用法（桥要已经在跑）：
    python -m tools.calib_poke            # 只体检
    python -m tools.calib_poke --sweep    # 体检 + 扫视
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

PORTS = list(range(9101, 9111))


async def find_bridge():
    import websockets
    for port in PORTS:
        try:
            ws = await websockets.connect(f"ws://127.0.0.1:{port}", open_timeout=1.2)
            hello = json.loads(await asyncio.wait_for(ws.recv(), 2))
            return ws, port, hello
        except Exception:
            continue
    return None, None, None


async def probe(ws):
    """发一条最小命令，看桥是"接受"还是"未知命令"。"""
    await ws.send(json.dumps({"cmd": "calib.command", "joints": {
        "eye_yaw_L": 3.0, "eye_yaw_R": 3.0,
        "eye_pitch_L": 0.0, "eye_pitch_R": 0.0,
        "neck_yaw": 0.0, "neck_pitch": 0.0, "neck_roll": 0.0}}))
    errors, poses = [], 0
    try:
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), 2.0))
            if m.get("evt") == "error":
                errors.append(m.get("message", ""))
            if m.get("evt") == "calib.pose":
                poses += 1
    except asyncio.TimeoutError:
        pass
    return errors, poses


async def sweep(ws, span: float, step_ms: int):
    seq = [(-span, 0), (-span / 2, 0), (0, 0), (span / 2, 0), (span, 0),
           (0, 0), (0, -span / 2), (0, 0), (0, span / 2), (0, 0)]
    for pan, tilt in seq:
        await ws.send(json.dumps({
            "cmd": "calib.command",
            "joints": {"eye_yaw_L": pan, "eye_yaw_R": pan,
                       "eye_pitch_L": tilt, "eye_pitch_R": tilt,
                       "neck_yaw": 0.0, "neck_pitch": 0.0, "neck_roll": 0.0},
            "ttl_ms": 5000}))
        print(f"[poke] → pan={pan:+5.1f} tilt={tilt:+5.1f}")
        await asyncio.sleep(step_ms / 1000.0)


async def main_async(args) -> int:
    ws, port, hello = await find_bridge()
    if not ws:
        print("[poke] ✗ 9100–9110 都没找到标定桥的 WS。")
        print("[poke]   先在另一个终端跑：")
        print("        python -m tools.calib_bridge --backend sim")
        return 2

    print(f"[poke] ✓ 找到桥: 端口 {port}, 后端 {hello.get('backend')}, "
          f"{len(hello.get('steps') or [])} 步")
    errs, poses = await probe(ws)
    if errs:
        print(f"[poke] ✗ 桥报错: {errs[0]}")
        print("[poke]   → 这是旧版桥，关掉重启即可")
        await ws.close()
        return 3
    print(f"[poke] ✓ 桥接受 calib.command（收到 {poses} 条 calib.pose）")
    await ws.send(json.dumps({"cmd": "calib.status"}))
    clients = None
    try:
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), 2.0))
            if m.get("evt") == "calib.status":
                clients = m.get("clients")
                break
    except asyncio.TimeoutError:
        pass
    if clients is None:
        print("[poke]   ⚠ 桥没有返回客户端数（旧版桥，建议重启）")
    elif clients <= 1:
        print(f"[poke]   ⚠ 连到桥的只有 1 个客户端（就是你），数字人页面没连上")
        print(f"[poke]     打开 http://127.0.0.1:{port - 1}/index.html 并按 Ctrl+F5")
    else:
        print(f"[poke] ✓ 有 {clients} 个客户端连着（向导 + 数字人）")

    if args.sweep:
        print("[poke] 开始扫视，盯住数字人的眼睛…")
        await sweep(ws, args.span, args.step_ms)
        print("[poke] 扫视结束。")
    await ws.close()
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="标定联动自检")
    ap.add_argument("--sweep", action="store_true", help="体检后做一圈扫视")
    ap.add_argument("--span", type=float, default=15.0)
    ap.add_argument("--step-ms", type=int, default=700)
    sys.exit(asyncio.run(main_async(ap.parse_args())))


if __name__ == "__main__":
    main()
