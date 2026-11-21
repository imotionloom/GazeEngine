// 渲染端冒烟测试：在 Node 里用桩环境执行 3d/index.html 的模块脚本，
// 捕获"未定义变量 / 顺序错误 / 远程分支抛异常"这类会让页面白屏的问题。
//
// 用法：node tests/js_smoke.mjs [index.html 路径]
// 退出码：0 通过，非 0 失败（会把异常栈打出来）
import fs from 'node:fs';
import path from 'node:path';

const file = process.argv[2] || '3d/index.html';
const html = fs.readFileSync(file, 'utf8');
const m = html.match(/<script type="module">([\s\S]*?)<\/script>/);
if (!m) { console.error('找不到 module 脚本'); process.exit(2); }
let code = m[1];
code = code.replace(/^\s*import[^\n]*\n/gm, '');   // 去掉 CDN import

// ---------------- 桩 ----------------
const noop = () => {};
class V3 {
  constructor(x = 0, y = 0, z = 0) { this.x = x; this.y = y; this.z = z; }
  set(x, y, z) { this.x = x; this.y = y; this.z = z; return this; }
  copy(v) { this.x = v.x; this.y = v.y; this.z = v.z; return this; }
  clone() { return new V3(this.x, this.y, this.z); }
  normalize() { return this; }
  length() { return Math.hypot(this.x, this.y, this.z); }
  sub(v) { return this.set(this.x - v.x, this.y - v.y, this.z - v.z); }
  add(v) { return this.set(this.x + v.x, this.y + v.y, this.z + v.z); }
  addScaledVector(v, s) { return this.set(this.x + v.x * s, this.y + v.y * s, this.z + v.z * s); }
  applyQuaternion() { return this; }
  getWorldPosition(t) { return t.set(0, 1.5, 0); }
  updateMatrixWorld() {}
}
class Quat {
  constructor(x=0,y=0,z=0,w=1) { this.x=x; this.y=y; this.z=z; this.w=w; }
  copy(q) { Object.assign(this, q); return this; }
}
class Obj {
  constructor() { this.position = new V3(); this.rotation = { x: 0, y: 0, z: 0 }; this.children = []; this.scale = new V3(1, 1, 1); }
  add(o) { this.children.push(o); return this; }
  getWorldPosition(t) { return t.set(0, 1.5, 0); }
  getWorldQuaternion(q) { return q.copy(new Quat()); }
  updateMatrixWorld() {}
  traverse(fn) { fn(this); }
  lookAt() {}
}
const THREE = {
  Vector3: V3, Quaternion: Quat, Object3D: Obj, Color: class { constructor() {} },
  Scene: class extends Obj { constructor() { super(); this.background = null; } },
  PerspectiveCamera: class extends Obj { constructor() { super(); this.aspect = 1; } updateProjectionMatrix() {} },
  WebGLRenderer: class { constructor() { this.domElement = { style: {} }; this.shadowMap = {}; } setSize() {} setPixelRatio() {} render() {} setClearColor() {} },
  GridHelper: class extends Obj {}, AmbientLight: class extends Obj {}, DirectionalLight: class extends Obj {},
  Clock: class { constructor() { this.t = 0; } getDelta() { const d = (this.dtMs ?? 16) / 1000; this.t += d; return d; } getElapsedTime() { return this.t; } },
  SRGBColorSpace: 'srgb',
  MathUtils: {
    degToRad: d => d * Math.PI / 180,
    radToDeg: r => r * 180 / Math.PI,
    clamp: (v, a, b) => Math.max(a, Math.min(b, v)),
    lerp: (a, b, t) => a + (b - a) * t,
  },
};
const els = {};
const document = {
  getElementById: id => (els[id] ||= { id, textContent: '', style: {}, appendChild: noop, innerHTML: '' }),
  createElement: () => ({ style: {}, appendChild: noop, getContext: () => null }),
  addEventListener: noop, body: { appendChild: noop }, querySelector: () => null,
};
const ctx = {
  THREE, document,
  performance: { now: () => Date.now() },
  requestAnimationFrame: noop,          // 关键：不真的循环
  addEventListener: noop,
  innerWidth: 800, innerHeight: 600, devicePixelRatio: 1,
  location: { search: '', hostname: '127.0.0.1', port: '9100' },
  WebSocket: class { constructor() { this.readyState = 0; } send() {} close() {} },
  console,
  setTimeout, clearTimeout, URLSearchParams, TextDecoder,
  GLTFLoader: class { register() {} load() {} },
  VRMLoaderPlugin: class {},
};

// 暴露内部：tick / 设置 remote / 读状态
const tail = `
return {
  tick,
  setRemote(v) { remote = v; lastMsgAt = performance.now(); },
  feed(m) { applyDataFrame(m); },
  setCalib(v, ttl) { calibPose = v; calibUntil = performance.now() + (ttl || 99000); },
  state() { return { curEyeYaw, curEyePitch, curNeckYaw }; },
  setVrm(v) { vrm = v; },
  gazeY() { return gazeTarget.y; },
  gazeX() { return gazeTarget.x; },
  now() { return renderT; },
  setDt(ms) { clock.dtMs = ms; },
};`;

let api;
try {
  const fn = new Function(...Object.keys(ctx), code + tail);
  api = fn(...Object.values(ctx));
} catch (e) {
  console.error('❌ 脚本顶层执行失败（页面会白屏）:\n', e.stack);
  process.exit(1);
}

// 假 VRM：够 tick 的远程分支跑起来
function fakeVRM(expressions = {}, bones = {}) {
  const mk = (n) => (bones[n] ||= { rotation: { x: 0, y: 0, z: 0 }, position: new V3(), getWorldPosition: t => t.set(0, 1.5, 0), getWorldQuaternion: q => q.copy(new Quat()) });
  return {
    scene: new Obj(),
    humanoid: { getNormalizedBoneNode: (n) => (n === 'neck' || n === 'head' ? mk(n) : mk('other')) },
    lookAt: { enabled: false, target: null, lookAt: () => {} },
    expressionManager: {
      setValue(n, v) { expressions[n] = v; },
      getExpression(n) { return { weight: expressions[n] ?? 0 }; },
      expressions: [{}, {}],
    },
    update: noop,
  };
}

let failed = 0;
const check = (name, fn) => {
  try { fn(); console.log(`  ✓ ${name}`); }
  catch (e) { failed++; console.error(`  ✗ ${name}: ${e.message}`); }
};

console.log('渲染端冒烟：');
check('空场景 tick 5 帧', () => { for (let i = 0; i < 5; i++) api.tick(); });

const ex = {};
api.setVrm(fakeVRM(ex));
check('远程姿态 tick 3 帧', () => {
  api.setRemote({ neck_yaw: 5, neck_pitch: 2, neck_roll: 0, eye_yaw: 20, eye_tilt: -6, blink: 1, lid_closure: 0 });
  for (let i = 0; i < 3; i++) api.tick();
});
check('眼球角度收敛到 20°', () => {
  const s = api.state();
  if (Math.abs(s.curEyeYaw - 20) > 0.5) throw new Error(`curEyeYaw=${s.curEyeYaw.toFixed(2)}`);
});
check('表情权重写入 (lookRight≈0.67)', () => {
  const w = ex.lookRight ?? 0;
  if (Math.abs(w - 20 / 30) > 0.05) throw new Error(`lookRight=${w.toFixed(3)}`);
});
check('30° 指令 → 权重 1.0', () => {
  api.setRemote({ neck_yaw: 0, neck_pitch: 0, neck_roll: 0, eye_yaw: 30, eye_tilt: 0, blink: 1, lid_closure: 0 });
  for (let i = 0; i < 3; i++) api.tick();
  const w = ex.lookRight ?? 0;
  if (Math.abs(w - 1.0) > 0.05) throw new Error(`lookRight=${w.toFixed(3)}`);
});
check('左右方向：+pan 时头与眼同侧', () => {
  const bones = {};
  api.setVrm(fakeVRM({}, bones));
  api.setRemote({ neck_yaw: 20, neck_pitch: 0, neck_roll: 0, eye_yaw: 20, eye_tilt: 0, blink: 1, lid_closure: 0 });
  for (let i = 0; i < 5; i++) api.tick();
  const ry = bones.neck.rotation.y;
  const gx = api.gazeX();
  if (!(ry > 0)) throw new Error(`+pan 时 neck.rotation.y=${ry.toFixed(3)}，应为正（向右）`);
  if (!(gx > 0)) throw new Error(`+pan 时注视目标 x=${gx.toFixed(2)}，应在右侧`);
  if (Math.sign(ry) !== Math.sign(gx)) throw new Error('头和眼方向不一致');
});
check('俯仰方向：tilt>0 目标点在头下方', () => {
  // 从桩里取回 gazeTarget，用正 tilt 驱动后检查 y 是否需要低于头
  api.setRemote({ neck_yaw: 0, neck_pitch: 0, neck_roll: 0, eye_yaw: 0, eye_tilt: 10, blink: 1, lid_closure: 0 });
  for (let i = 0; i < 3; i++) api.tick();
  const y = api.gazeY();
  if (!(y < 1.5)) throw new Error(`tilt=+10 时目标 y=${y.toFixed(3)}，应在头(1.5)下方`);
  api.setRemote({ neck_yaw: 0, neck_pitch: 0, neck_roll: 0, eye_yaw: 0, eye_tilt: -10, blink: 1, lid_closure: 0 });
  for (let i = 0; i < 3; i++) api.tick();
  if (!(api.gazeY() > 1.5)) throw new Error('tilt=-10 时目标应在头上方');
});
check('眨眼按"已进行时长"重放（跨时钟基准安全）', () => {
  const run = (dtMs, ageAtRecv) => {
    const ex2 = {};
    api.setVrm(fakeVRM(ex2));
    api.setDt(dtMs);
    api.tick();
    api.feed({ eye_yaw: 0, eye_tilt: 0, neck_yaw: 0, neck_pitch: 0, neck_roll: 0,
      blink: 1, lid_closure: 0,
      blink_age_ms: ageAtRecv, blink_ms: 150, blink_close: 0.35, blink_hold: 0.55 });
    let first = null, last = null;
    for (let i = 0; i < 40; i++) {
      api.tick();
      if ((ex2.blink ?? 0) > 0.5) { if (first === null) first = api.now(); last = api.now(); }
    }
    return first === null ? 0 : last - first;
  };
  const fast = run(16, 0);
  if (fast < 60 || fast > 220) throw new Error(`30fps 下闭眼 ${fast}ms，应 ≈150ms`);
  const slow = run(200, 0);
  if (slow > 210) throw new Error(`5fps 下闭眼 ${slow}ms，不应超过一帧`);
  // 收到时事件已经过去 → 不该补放
  const stale = run(16, 400);
  if (stale !== 0) throw new Error(`过期事件仍触发眨眼 ${stale}ms`);
  // 没有 age 字段 → 退回采样值（blink 未提供时按睁眼处理，不应崩）
  const noEvent = run(16, null);
  if (noEvent < 0) throw new Error('异常');
});
check('标定覆盖优先', () => {
  api.setCalib({ neck_yaw: 0, neck_pitch: 0, neck_roll: 0, eye_yaw: -12, eye_tilt: 4, blink: 1, lid_closure: 0 });
  api.tick();
  const s = api.state();
  if (Math.abs(s.curEyeYaw + 12) > 1) throw new Error(`curEyeYaw=${s.curEyeYaw.toFixed(2)}`);
});
check('presence 面板数据不拖垮渲染（TDZ 回归）', () => {
  // 回归：renderPresence 曾用 tick 内 let preLast，调用早于声明 → 每帧 ReferenceError
  // → renderer.render 不执行 → 黑屏且 blink 不更新。
  api.setRemote({ neck_yaw: 0, neck_pitch: 0, neck_roll: 0, eye_yaw: 0, eye_tilt: 0,
    blink: 1, lid_closure: 0, blink_age_ms: 0, blink_ms: 150, blink_close: 0.35, blink_hold: 0.55,
    presence: { family: 'IDLE', priority: 6, family_t: 12, idle_t: 20,
                dims: { arousal: 0.4, load: 0.5 }, ranges: { arousal: [0.3, 0.6] }, center: { arousal: 0.45 },
                transition: { to: 'SCANNING', progress: 0.4 } } });
  for (let i = 0; i < 5; i++) api.tick();
  api.setRemote({ neck_yaw: 0, neck_pitch: 0, neck_roll: 0, eye_yaw: 0, eye_tilt: 0,
    blink: 1, lid_closure: 0, blink_age_ms: 0, blink_ms: 150,
    presence: { family: 'IDLE', priority: 6, family_t: 1, idle_t: 2, dims: {}, ranges: {}, center: {} } });
  for (let i = 0; i < 5; i++) api.tick();
});

check('无远程时回到 0（鼠标模式不崩）', () => {
  api.setVrm(fakeVRM(ex));
  for (let i = 0; i < 3; i++) api.tick();
});

if (failed) { console.error(`\n${failed} 项失败`); process.exit(1); }
console.log('\n全部通过');
process.exit(0);   // 页面里有重连定时器，不显式退出会挂住
