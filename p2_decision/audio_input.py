"""
音频输入接口：VAD（语音活动检测）
PC 端用 sounddevice，RK3576 用 alsaaudio，统一输出 is_speaking
"""
import time
from abc import ABC, abstractmethod


class AudioInput(ABC):
    @abstractmethod
    def is_speaking(self) -> bool:
        """当前帧是否有人在说话"""
        pass

    @abstractmethod
    def close(self):
        pass


class SilentAudio(AudioInput):
    """无音频输入时的默认实现，永远返回 False"""
    def is_speaking(self) -> bool:
        return False
    def close(self):
        pass


class PCSVAD(AudioInput):
    """PC 端：sounddevice 录 16kHz 单声道，简单能量阈值 VAD，懒加载"""
    def __init__(self, threshold=0.02, window_ms=100):
        self.threshold = threshold
        self.window_samples = int(16000 * window_ms / 1000)
        self._buf = []
        self._speaking = False
        self._last_speech = 0
        self.stream = None
        self._sd = None
        self._np = None

    def _ensure(self):
        """第一次用的时候才开流，构造不碰麦克风"""
        if self.stream is not None:
            return True
        try:
            import sounddevice as sd
            import numpy as np
            self._sd = sd
            self._np = np
            def callback(indata, frames, time, status):
                # indata 是 int16 原始 buffer，归一化到 [-1, 1]
                samples = np.frombuffer(indata, dtype=np.int16).astype(np.float32) / 32768.0
                self._buf.append(float(np.abs(samples).mean()))
            self.stream = sd.RawInputStream(samplerate=16000, channels=1, dtype="int16",
                                            callback=callback, blocksize=512)
            self.stream.start()
            return True
        except Exception:
            self.stream = None
            return False

    def is_speaking(self) -> bool:
        if not self._ensure():
            return False
        sd, np = self._sd, self._np
        while len(self._buf) > self.window_samples / 512:
            self._buf.pop(0)
        energy = sum(self._buf) / max(len(self._buf), 1)
        if energy > self.threshold:
            self._last_speech = time.time()
            return True
        return time.time() - self._last_speech < 0.3

    def close(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None


def create_audio_input(enable=False) -> AudioInput:
    """工厂：PC 用 sounddevice，板端以后加 alsaaudio
    默认 enable=False（权重与环境都不需要，测试不会碰音频）；运行时改 True 开 VAD，
    LISTENING 族才可达。
    """
    if not enable:
        return SilentAudio()
    try:
        import sounddevice as sd
        sd.query_devices(kind="input")  # 先探测设备，没设备直接 fallback
        return PCSVAD()
    except Exception:
        return SilentAudio()
