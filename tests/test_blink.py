"""眨眼：曲线、事件、冷却。

眨眼策略全部在 P2（dynamics.BlinkMachine）；渲染端与 P3 只按 (now-t0)/ms 求值，
所以这里要保证：
  1) envelope 是唯一曲线定义；
  2) event() 给出 t0 与时长；
  3) 冷却有效，避免低帧率下连续触发。
"""
import pytest

from p2_decision.dynamics import (BlinkMachine, BLINK_DURATION_MS,
                                  BLINK_CLOSE_RATIO, BLINK_HOLD_RATIO,
                                  BLINK_COOLDOWN_MS)


def test_envelope_shape():
    assert BlinkMachine.envelope(0.0) == 0.0
    assert BlinkMachine.envelope(1.0) == 0.0
    assert BlinkMachine.envelope(-0.5) == 0.0
    # 闭合段线性上升
    assert BlinkMachine.envelope(BLINK_CLOSE_RATIO / 2) == pytest.approx(0.5)
    # 保持段全闭
    mid = (BLINK_CLOSE_RATIO + BLINK_HOLD_RATIO) / 2
    assert BlinkMachine.envelope(mid) == 1.0
    # 睁开段回到 0
    assert BlinkMachine.envelope(0.99) < 0.05


def test_event_reports_t0_and_duration():
    b = BlinkMachine()
    assert b.trigger(1234) is True
    ev = b.event()
    assert ev["t0"] == 1234
    assert ev["ms"] == BLINK_DURATION_MS
    assert ev["close"] == BLINK_CLOSE_RATIO and ev["hold"] == BLINK_HOLD_RATIO


def test_curve_is_lid_open_ratio():
    b = BlinkMachine()
    b.trigger(0)
    # 时长 150ms：闭合 0–52.5，全闭 52.5–82.5，睁开 82.5–150
    assert b.update(0) == pytest.approx(1.0)        # 刚触发，还睁着
    assert b.update(70) == pytest.approx(0.0)       # 保持段全闭
    assert 0.0 < b.update(90) < 0.5                 # 睁开途中
    assert b.update(165) == pytest.approx(1.0)      # 窗口结束，睁开


def test_cooldown_blocks_immediate_retrigger():
    b = BlinkMachine()
    assert b.trigger(0) is True
    for t in range(0, 200, 16):
        b.update(t)
    # 刚结束（约 150ms）不允许立刻再来
    assert b.trigger(160) is False
    # 冷却过后允许
    assert b.trigger(150 + BLINK_COOLDOWN_MS + 10) is True


def test_trigger_ignored_while_blinking():
    b = BlinkMachine()
    b.trigger(0)
    b.update(30)
    assert b.trigger(40) is False       # 正在眨，不能重入
