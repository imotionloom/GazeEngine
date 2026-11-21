"""
动作编辑器后端服务器
启动: python server.py
然后浏览器打开 http://localhost:8899
"""
import json
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import os
import sys

# 导入动作库
sys.path.insert(0, os.path.dirname(__file__))
from actions import ACTIONS, sample, export_python

# ZMQ导入
try:
    import zmq
    HAS_ZMQ = True
except ImportError:
    HAS_ZMQ = False
    print("警告: pyzmq未安装，无法发送到VRM查看器")

# ========== 全局状态 ==========
PLAYING = False
PLAY_THREAD = None
PLAY_PARAMS = None  # {action, speed, amplify, blink, loop}
ACTIONS_LOCK = threading.Lock()  # 保护ACTIONS读写

# ZMQ全局只初始化一次
ZMQ_CONTEXT = None
ZMQ_PUB = None

HTTP_PORT = 8899
PUB_ADDR = "tcp://127.0.0.1:5557"  # 和ws_bridge.py保持一致


def init_zmq():
    """初始化ZMQ，全局只调一次"""
    global ZMQ_CONTEXT, ZMQ_PUB
    if ZMQ_PUB is not None:
        return True
    if not HAS_ZMQ:
        return False
    try:
        ZMQ_CONTEXT = zmq.Context()
        ZMQ_PUB = ZMQ_CONTEXT.socket(zmq.PUB)
        ZMQ_PUB.setsockopt(zmq.LINGER, 0)
        ZMQ_PUB.bind(PUB_ADDR)
        time.sleep(0.5)  # 等连接
        print(f"ZMQ初始化成功: {PUB_ADDR}")
        return True
    except zmq.ZMQError as e:
        print(f"ZMQ初始化失败: {e}")
        print(f"端口5557可能被占用，请先关闭其他ZMQ发布进程")
        ZMQ_PUB = None
        return False


def play_loop():
    """播放循环，在单独线程里跑"""
    global PLAYING
    
    if ZMQ_PUB is None:
        PLAYING = False
        return

    print(f"开始播放: {PLAY_PARAMS['action']}")

    try:
        while PLAYING:
            action_name = PLAY_PARAMS['action']
            speed = PLAY_PARAMS['speed']
            amplify = PLAY_PARAMS['amplify']
            blink = PLAY_PARAMS['blink']
            loop = PLAY_PARAMS['loop']

            with ACTIONS_LOCK:
                kfs, dur, desc = ACTIONS[action_name]
            start_time = time.time()

            while PLAYING:  # 这里也要检查PLAYING
                elapsed_real = time.time() - start_time
                elapsed = elapsed_real * speed

                if elapsed >= dur:
                    if loop:
                        start_time = time.time()
                        continue
                    else:
                        send_frame(ZMQ_PUB, action_name, 0, speed, amplify, blink)
                        break

                send_frame(ZMQ_PUB, action_name, elapsed, speed, amplify, blink)
                time.sleep(1/60)  # 60fps
    finally:
        # 发归零
        try:
            send_frame(ZMQ_PUB, PLAY_PARAMS['action'], 0, 1, 1, 1)
        except:
            pass
        print("播放停止")


def send_frame(pub, action_name, elapsed, speed, amplify, blink):
    """发送一帧数据"""
    with ACTIONS_LOCK:
        kfs, dur, desc = ACTIONS[action_name]
    off = sample(elapsed, kfs, dur)
    pan_off, tilt_off, epan_off, etilt_off, lid_off, roll_off = off

    # 应用倍率
    pan_off *= amplify
    tilt_off *= amplify
    roll_off *= amplify
    epan_off *= amplify
    etilt_off *= amplify
    lid_off *= blink

    payload = {
        "neck_yaw": pan_off,
        "neck_pitch": tilt_off,
        "neck_roll": roll_off,
        "eye_yaw": -epan_off,  # 注意符号和 test_interlude 一致
        "eye_tilt": etilt_off,
        "blink": 1.0,
        "blink_age_ms": None,
        "blink_ms": 180,
        "blink_close": 0.5,
        "blink_hold": 0.1,
        "lid_closure": lid_off * 95.0,  # 转成度数
        "look_scale": 1.0,
        "source": "ACTION_EDITOR",
        "state": action_name,
        "ts": int(elapsed * 1000),
    }
    pub.send_string(json.dumps(payload))


def save_to_file(name, kfs, dur):
    """把修改后的动作写回actions.py文件（按行解析，不用正则）"""
    filepath = os.path.join(os.path.dirname(__file__), 'actions.py')
    
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    # 找 XXX_KEYFRAMES = [ 开始行
    start_idx = None
    end_idx = None
    dur_idx = None
    
    for i, line in enumerate(lines):
        if line.strip() == f'{name}_KEYFRAMES = [':
            start_idx = i
        if start_idx is not None and line.strip() == ']' and end_idx is None:
            end_idx = i
        if line.strip().startswith(f'{name}_DUR = '):
            dur_idx = i
            break
    
    if start_idx is None or end_idx is None or dur_idx is None:
        raise ValueError(f"找不到 {name} 的定义")
    
    # 生成新的keyframe行
    new_lines = [f'{name}_KEYFRAMES = [\n']
    for kf in kfs:
        new_lines.append(f'    {tuple(kf)},\n')
    new_lines.append(']\n')
    new_lines.append(f'{name}_DUR = {dur}\n')
    
    # 替换：从start_idx到dur_idx（包含）
    new_content_lines = lines[:start_idx] + new_lines + lines[dur_idx+1:]
    
    with open(filepath, 'w', encoding='utf-8') as f:
        f.writelines(new_content_lines)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == '/':
            self.send_html(200, 'index.html')
        elif path == '/api/actions':
            result = {}
            for name, (kfs, dur, desc) in ACTIONS.items():
                result[name] = {"dur": dur, "desc": desc, "num_keyframes": len(kfs)}
            self.send_json(200, result)
        elif path.startswith('/api/action/'):
            name = path.split('/')[-1]
            if name in ACTIONS:
                kfs, dur, desc = ACTIONS[name]
                self.send_json(200, {
                    "name": name, "dur": dur, "desc": desc,
                    "keyframes": [list(kf) for kf in kfs]
                })
            else:
                self.send_json(404, {"error": "not found"})
        elif path.startswith('/api/export/'):
            name = path.split('/')[-1]
            if name in ACTIONS:
                code = export_python(name)
                self.send_text(200, code)
            else:
                self.send_json(404, {"error": "not found"})
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        global PLAYING, PLAY_THREAD, PLAY_PARAMS
        parsed = urlparse(self.path)
        path = parsed.path

        content_length = int(self.headers.get('Content-Length', 0))
        if content_length > 0:
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
            except:
                self.send_json(400, {"error": "invalid json"})
                return
        else:
            data = {}

        if path == '/api/play':
            if not HAS_ZMQ:
                self.send_json(500, {"error": "pyzmq not installed"})
                return

            # 先初始化ZMQ（全局只初始化一次）
            if not init_zmq():
                self.send_json(500, {"error": "ZMQ初始化失败，端口被占用"})
                return

            # 停止之前的播放
            PLAYING = False
            if PLAY_THREAD:
                PLAY_THREAD.join(timeout=2)

            # 开始新播放
            PLAY_PARAMS = data
            PLAYING = True
            PLAY_THREAD = threading.Thread(target=play_loop, daemon=True)
            PLAY_THREAD.start()
            self.send_json(200, {"status": "playing"})

        elif path == '/api/stop':
            PLAYING = False
            self.send_json(200, {"status": "stopped"})

        elif path == '/api/params':
            # 实时更新播放参数，不用重新点播放
            if PLAY_PARAMS:
                PLAY_PARAMS.update(data)
            self.send_json(200, {"ok": True})

        elif path == '/api/new':
            name = data.get('name', '').strip().upper()
            if not name or not name.replace('_', '').isalnum():
                self.send_json(400, {"error": "动作名只能用字母数字下划线"})
                return
            if name in ACTIONS:
                self.send_json(400, {"error": "动作已存在"})
                return
            
            # 默认2个关键帧：开始和结束都在0
            default_kfs = [(0.0, 0, 0, 0, 0, 0, 0), (1.0, 0, 0, 0, 0, 0, 0)]
            dur = 1.0
            desc = data.get('desc', '自定义动作')
            
            with ACTIONS_LOCK:
                ACTIONS[name] = (default_kfs, dur, desc)
            
            # 写回文件
            save_to_file(name, default_kfs, dur)
            
            # 同时把动作加进注册表ACTIONS的文件里（save_to_file只写了keyframe，没写注册行）
            # 简单处理：重新import太麻烦，直接在文件末尾加注册行
            filepath = os.path.join(os.path.dirname(__file__), 'actions.py')
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            if f'"{name}"' not in content:
                with open(filepath, 'a', encoding='utf-8') as f:
                    f.write(f'\nACTIONS["{name}"] = ({name}_KEYFRAMES, {name}_DUR, "{desc}")\n')
            
            self.send_json(200, {"ok": True, "name": name})

        elif path.startswith('/api/save/'):
            name = path.split('/')[-1]
            if name in ACTIONS:
                # 校验关键帧
                kfs_raw = data['keyframes']
                if len(kfs_raw) < 2:
                    self.send_json(400, {"error": "至少需要2个关键帧"})
                    return
                
                # 钳位并校验
                kfs = []
                for kf in kfs_raw:
                    t = float(kf[0])
                    if t < 0:
                        self.send_json(400, {"error": f"时间不能为负: {t}"})
                        return
                    pan = max(-45, min(45, float(kf[1])))
                    tilt = max(-30, min(30, float(kf[2])))
                    epan = max(-25, min(25, float(kf[3])))
                    etilt = max(-25, min(25, float(kf[4])))
                    lid = max(0, min(1, float(kf[5])))
                    roll = max(-45, min(45, float(kf[6])))
                    kfs.append((t, pan, tilt, epan, etilt, lid, roll))
                
                # 按时间排序
                kfs.sort(key=lambda x: x[0])
                dur = float(data['dur'])
                if dur <= 0:
                    self.send_json(400, {"error": "时长必须大于0"})
                    return
                
                # 更新动作（加锁）
                with ACTIONS_LOCK:
                    ACTIONS[name] = (kfs, dur, ACTIONS[name][2])
                
                # 写回actions.py
                save_to_file(name, kfs, dur)
                self.send_json(200, {"ok": True, "message": "保存成功"})
            else:
                self.send_json(404, {"error": "action not found"})

        else:
            self.send_json(404, {"error": "not found"})

    def send_html(self, code, filename):
        filepath = os.path.join(os.path.dirname(__file__), filename)
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(content.encode('utf-8'))

    def send_json(self, code, obj):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode('utf-8'))

    def send_text(self, code, text):
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.end_headers()
        self.wfile.write(text.encode('utf-8'))

    def log_message(self, format, *args):
        pass  # 静默


if __name__ == '__main__':
    print(f"动作编辑器启动: http://localhost:{HTTP_PORT}")
    print(f"ZMQ PUB: {PUB_ADDR}")
    print()
    print("使用方法:")
    print("1. 先启动VRM查看器: python -m http.server 8898")
    print("2. 启动ws_bridge: python tools/ws_bridge.py")
    print("3. 打开编辑器: http://localhost:8899")
    
    # 只监听回环：本服务能直接改写 actions.py，不能暴露到局域网
    server = HTTPServer(('127.0.0.1', HTTP_PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止")
        PLAYING = False
