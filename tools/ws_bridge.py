"""
ZMQ 5557 (test_harness PUB joints JSON) → WebSocket 9000 (browser 数字人) bridge.
用法：python tools/ws_bridge.py
"""
import asyncio, json, os, zmq
import websockets

async def main():
    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    sub.connect(os.environ.get("GAZE_DEBUG_PUB", "tcp://127.0.0.1:5557"))
    sub.setsockopt(zmq.SUBSCRIBE, b"")

    clients = set()
    async def ws_handler(ws):
        clients.add(ws)
        try:
            async for _ in ws: pass
        finally:
            clients.discard(ws)

    async with websockets.serve(ws_handler, "127.0.0.1", 9000):
        print("ws_bridge: ZMQ 5557 → WS 9000 已连接")
        while True:
            try:
                msg = sub.recv(zmq.NOBLOCK)
            except zmq.Again:
                await asyncio.sleep(0.005)
                continue
            if clients:
                await asyncio.gather(*[c.send(msg) for c in clients], return_exceptions=True)

if __name__ == "__main__":
    asyncio.run(main())
