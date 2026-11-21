// P3 执行进程 · M3.5 执行后端 HAL（骨架）
// 铁律：RT 线程只写 DMC 信箱；后端 I/O 在普通线程，按后端刷新率消费。
#pragma once
#include <array>
#include <cstdint>

struct JointAngles {                     // 与 gaze_messages.proto JointAngles 对齐
    float neck_yaw=0, neck_pitch=0, neck_roll=0;
    float eye_yaw_L=0, eye_yaw_R=0, eye_pitch_L=0, eye_pitch_R=0;
    float lid_up_L=0, lid_up_R=0, lid_low_L=0, lid_low_R=0;
};

struct DeviceCommand {                   // DMC 信箱里的一帧
    uint64_t ts_ms;
    JointAngles joints;
};

struct DeviceState {
    JointAngles actual;
    bool overtemp=false, stall=false;
};

class IActuatorBackend {                 // 三后端共同接口
public:
    virtual ~IActuatorBackend() = default;
    virtual bool send(const DeviceCommand& cmd) = 0;        // 后端刷新率各自定
    virtual DeviceState read() = 0;                          // 产出 IF-6
    virtual void emergency_safe_position() = 0;              // 回安全位
};

// ---- 后端1：RS485 串口舵机（真实硬件，协议帧格式待舵机手册补） ----
class ServoBusBackend : public IActuatorBackend {
public:
    bool send(const DeviceCommand&) override;
    DeviceState read() override;
    void emergency_safe_position() override;
private:
    // TODO: open("/dev/ttySx", B115200); 组帧下发 11 通道；解析回读
    int fd_ = -1;
};

// ---- 后端2：数字人（UDP，渲染端选型待定，协议先定骨架） ----
class DigitalHumanBackend : public IActuatorBackend {
public:
    bool send(const DeviceCommand&) override;   // UDP 30-60Hz
    DeviceState read() override;                // 模拟编码器 = 指令值 + 小延迟
    void emergency_safe_position() override;
};

// ---- 后端3：sim（日志回放，PC 无硬件回归） ----
class SimBackend : public IActuatorBackend {
public:
    bool send(const DeviceCommand&) override;
    DeviceState read() override;
    void emergency_safe_position() override;
};
