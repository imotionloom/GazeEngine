// P3 执行进程 · RT 主环骨架
// 绑 A72#3 隔离核，500-1000Hz。职责：插补 + 看门狗 + DMC 写后端。
#pragma once
#include "actuator_backend.hpp"
#include <atomic>
#include <mutex>

class Interpolator {
public:
    // 100Hz 目标 -> 1000Hz 线性插补；vel_limit_dps 限速
    // ⚠️ ts_ms 必须用本地 CLOCK_MONOTONIC 接收时刻，不要用 IF-5 header.ts_ms（墙钟），
    //    否则 now_ms()-from_ts_ 会长期≈0，插补器几乎不动。header.ts 只用于延迟统计。
    void set_target(const JointAngles& target, float vel_limit_dps, uint64_t ts_ms) {
        std::lock_guard<std::mutex> lk(mu_);
        from_ = cur_; to_ = target; from_ts_ = last_ts_; to_ts_ = ts_ms;
        vel_limit_ = vel_limit_dps;
    }
    JointAngles step(uint64_t now_ms) {
        std::lock_guard<std::mutex> lk(mu_);
        float span = (float)(to_ts_ - from_ts_);
        // span<=0：k=0 → out=from_，插补器停在旧位置
        // TODO(生产实现)：span<=0时应直接跳到to_，或由下方clamp_step按vel_limit滑行
        float k;
        if (span <= 0.f) {
            k = 0.0f;
        } else {
            k = (float)(now_ms - from_ts_) / span;
            k = k < 0.f ? 0.f : (k > 1.f ? 1.f : k);
        }
        JointAngles out;
        out.neck_yaw    = lerp(from_.neck_yaw,    to_.neck_yaw,    k);
        out.neck_pitch  = lerp(from_.neck_pitch,  to_.neck_pitch,  k);
        out.neck_roll   = lerp(from_.neck_roll,   to_.neck_roll,   k);
        out.eye_yaw_L   = lerp(from_.eye_yaw_L,   to_.eye_yaw_L,   k);
        out.eye_yaw_R   = lerp(from_.eye_yaw_R,   to_.eye_yaw_R,   k);
        out.eye_pitch_L = lerp(from_.eye_pitch_L, to_.eye_pitch_L, k);
        out.eye_pitch_R = lerp(from_.eye_pitch_R, to_.eye_pitch_R, k);
        out.lid_up_L    = lerp(from_.lid_up_L,    to_.lid_up_L,    k);
        out.lid_up_R    = lerp(from_.lid_up_R,    to_.lid_up_R,    k);
        out.lid_low_L   = lerp(from_.lid_low_L,   to_.lid_low_L,   k);
        out.lid_low_R   = lerp(from_.lid_low_R,   to_.lid_low_R,   k);

        // ⑪ vel_limit 生效：按本周期 dt(ms) 限速（11 通道全量）
        float dt = (last_ts_ == 0 || now_ms <= last_ts_) ? 1.f : (float)(now_ms - last_ts_);
        float max_step = vel_limit_ * dt / 1000.f;   // deg
        out.neck_yaw    = clamp_step(cur_.neck_yaw,    out.neck_yaw,    max_step);
        out.neck_pitch  = clamp_step(cur_.neck_pitch,  out.neck_pitch,  max_step);
        out.neck_roll   = clamp_step(cur_.neck_roll,   out.neck_roll,   max_step);
        out.eye_yaw_L   = clamp_step(cur_.eye_yaw_L,   out.eye_yaw_L,   max_step);
        out.eye_yaw_R   = clamp_step(cur_.eye_yaw_R,   out.eye_yaw_R,   max_step);
        out.eye_pitch_L = clamp_step(cur_.eye_pitch_L, out.eye_pitch_L, max_step);
        out.eye_pitch_R = clamp_step(cur_.eye_pitch_R, out.eye_pitch_R, max_step);
        out.lid_up_L    = clamp_step(cur_.lid_up_L,    out.lid_up_L,    max_step);
        out.lid_up_R    = clamp_step(cur_.lid_up_R,    out.lid_up_R,    max_step);
        out.lid_low_L   = clamp_step(cur_.lid_low_L,   out.lid_low_L,   max_step);
        out.lid_low_R   = clamp_step(cur_.lid_low_R,   out.lid_low_R,   max_step);

        cur_ = out; last_ts_ = now_ms;
        return out;
    }
private:
    static float lerp(float a, float b, float k) { return a + (b - a) * k; }
    static float clamp_step(float from, float to, float max_step) {
        float d = to - from;
        if (d >  max_step) d =  max_step;
        if (d < -max_step) d = -max_step;
        return from + d;
    }
    std::mutex mu_;
    JointAngles from_, to_, cur_;
    uint64_t from_ts_=0, last_ts_=0;
    float vel_limit_=900.f;  // 宽松上界默认值；实际以P2下发的Driver.MAX_DPS(=400)为准
};

class Watchdog {
public:
    explicit Watchdog(uint64_t max_age_ms=100) : max_age_ms_(max_age_ms) {}
    void feed(uint64_t ts_ms) {
        std::atomic_store(&last_cmd_ts_, ts_ms);
        std::atomic_store(&latched_, false);   // 收到指令解除 latching
    }
    bool expired(uint64_t now_ms) const {
        // ⑫ 首次 feed 前按 expired 处理（默认危险态）；触发后 latch 直到 feed
        uint64_t last = std::atomic_load(&last_cmd_ts_);
        if (last == 0) return true;
        bool over = (now_ms - last) > max_age_ms_;
        if (over) std::atomic_store(&latched_, true);
        return over || std::atomic_load(&latched_);
    }
private:
    uint64_t max_age_ms_;
    mutable std::atomic<uint64_t> last_cmd_ts_{0};
    mutable std::atomic<bool> latched_{false};
};

// ---------------------------------------------------------------------------
// 启动自检（C5 延迟 + C7-quick 精度），对应 docs/CALIBRATION_SPEC.md §7.7
// ---------------------------------------------------------------------------
// 设计要点：
//   * SELFTEST 是 P3 的一个独立状态，**在 RT 环之外**执行（低频、可失败、不阻塞）；
//   * 数字人与真机走同一状态机：数字人通过渲染端回读"实际应用的关节角"，接口相同；
//   * 任何一项失败都只降级、不阻塞启动；只有"三次不响应"才标 FAULT 并回安全位。
enum class ExecState {
    BOOT,       // 加载配置（calibration/active.json）+ 打开总线
    SELFTEST,   // C5 延迟 + C7-quick 精度体检（~7s）
    RUN,        // 正常 1000Hz 插补
    SAFE,       // 看门狗/故障 → 缓慢回安全位
    FAULT       // 硬件级故障，等待外部复位
};

struct ChannelPrecision {
    float effective_step_deg = 0.0f;   // 来自 calibration 的 C7 结果
    float requirement_deg    = 0.5f;   // 眼球 0.5 / 颈部 1.0 / 眼睑 2.0
    bool  pass               = true;
};

struct SelfTestReport {
    bool     c5_ok = false;
    bool     c7_ok = false;
    float    ipc_p99_ms = 0.0f;
    uint32_t degraded_mask = 0;        // 按 JointAngles 通道位标记
    uint32_t fault_mask    = 0;
};

class BootSequencer {
public:
    // 返回 SELFTEST 通过后的下一个状态。
    // quick 流程：每通道 0→+0.5°→0，读回增量；与 calibration 里的
    // effective_step_deg 比对，偏差 > max(2σ, 0.3°) 记 degraded；
    // 连续 3 次无响应记 fault。
    ExecState run_selftest(IActuatorBackend& backend, SelfTestReport* out) {
        // ① 从 calibration/active.json 读 runtime：watchdog_max_age_ms / neck_delay_ms /
        //    precision.effective_step_deg；缺文件 → 用保守默认（死区 1.0、亚度特性关）
        // ② C5：IF-5/IF-6 各 200 次 ping-pong → p99；超 5ms 只告警
        // ③ C7-quick：逐通道 ±0.5° 往返，安全包络 ±2°、限速、看门狗常开
        // ④ 汇总：全通过 → RUN；有 degraded → RUN（带降级掩码）；有 fault → SAFE
        (void)backend;
        if (out) *out = SelfTestReport{};
        return ExecState::RUN;
    }

    // 人工触发完整标定（C7-full / C3）时不进 RUN，先降到 SAFE 再测。
    static bool requires_safe_stop() { return true; }
};

// RT 主循环（伪代码骨架，实际需 SCHED_FIFO + 绑核）
// void rt_loop(IActuatorBackend& backend, Watchdog& wd, Interpolator& interp) {
//   while (true) {
//     uint64_t now = now_ms();
//     if (state == ExecState::SELFTEST) {          // 只在启动时进入一次
//       SelfTestReport rep;
//       state = sequencer.run_selftest(backend, &rep);
//       if (state == ExecState::SAFE) { backend.emergency_safe_position(); }
//       continue;
//     }
//     if (wd.expired(now)) {
//       backend.emergency_safe_position();   // ⑫ latch 后持续回中，直到 feed 解除
//       sleep_ms(1); continue;
//     }
//     JointAngles j = interp.step(now);
//     backend.send({now, j});
//     DeviceState st = backend.read();                         // → IF-6 反向共享环
//     sleep_ms(1);                                             // 1000Hz
//   }
// }
