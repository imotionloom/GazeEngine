"""回放CSV轨迹到 WebSocket 9000，供 3d/index.html 实时显示 VRM 数字人。
用法: python replay_server.py [csv文件]
不带参数则列出可选CSV。
"""
import asyncio, csv, json, sys, os
from websockets.server import serve

TRAJ_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trajectories")
CLIENTS = set()
CURRENT_CSV = None
CURRENT_ROWS = None
PLAYBACK_SPEED = 1.0

async def ws_handler(ws):
    CLIENTS.add(ws)
    try:
        if CURRENT_ROWS:
            await broadcast_status(f"回放中: {CURRENT_CSV}")
        async for msg in ws:
            pass
    finally:
        CLIENTS.discard(ws)

async def broadcast_status(text):
    if not CLIENTS: return
    msg = json.dumps({"status": text})
    await asyncio.gather(*[c.send(msg) for c in CLIENTS], return_exceptions=True)

async def broadcast_frame(frame):
    if not CLIENTS: return
    await asyncio.gather(*[c.send(json.dumps(frame)) for c in CLIENTS], return_exceptions=True)

async def replay_loop():
    global CURRENT_ROWS, CURRENT_CSV
    while CURRENT_ROWS is None:
        await asyncio.sleep(0.1)

    print(f"回放: {CURRENT_CSV} ({len(CURRENT_ROWS)} 帧)")
    t0 = asyncio.get_event_loop().time()
    for row in CURRENT_ROWS:
        ts_ms = float(row["ts_ms"])
        # 等待到对应时间
        target_t = t0 + ts_ms / 1000.0 / PLAYBACK_SPEED
        now = asyncio.get_event_loop().time()
        sleep_t = target_t - now
        if sleep_t > 0:
            await asyncio.sleep(sleep_t)
        # 构造VRM数据帧
        frame = {
            "neck_yaw": float(row["neck_pan"]),
            "neck_pitch": float(row["neck_tilt"]),
            "neck_roll": 0,
            "eye_yaw": float(row["eye_pan"]),
            "eye_tilt": float(row["eye_tilt"]),
            "eye_offset": float(row.get("offset", 0)),
            "lid_closure": float(row.get("lid", 0)) * 60,
            "blink": 1.0 - float(row.get("lid", 0)),
            "state": row.get("state", ""),
            "look_scale": 30,
        }
        await broadcast_frame(frame)
    print("回放结束，3秒后重新开始...")
    await asyncio.sleep(3)

async def main():
    global CURRENT_CSV, CURRENT_ROWS
    if len(sys.argv) > 1:
        csv_name = sys.argv[1]
        if not csv_name.endswith(".csv"):
            csv_name += ".csv"
        path = os.path.join(TRAJ_DIR, csv_name)
    else:
        csvs = sorted([f for f in os.listdir(TRAJ_DIR) if f.endswith(".csv")])
        print("可选CSV:")
        for i, f in enumerate(csvs):
            print(f"  {i}: {f}")
        idx = int(input("选择: "))
        path = os.path.join(TRAJ_DIR, csvs[idx])

    CURRENT_CSV = os.path.basename(path)
    with open(path, encoding="utf-8") as f:
        CURRENT_ROWS = list(csv.DictReader(f))
    print(f"已加载 {len(CURRENT_ROWS)} 帧")

    async with serve(ws_handler, "127.0.0.1", 9000):
        print("WebSocket 9000 已启动，等待客户端连接...")
        # 等待第一个客户端连接后再开始回放
        while len(CLIENTS) == 0:
            await asyncio.sleep(0.2)
        print(f"客户端已连接，开始回放")
        while True:
            await replay_loop()

if __name__ == "__main__":
    asyncio.run(main())
