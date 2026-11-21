"""
动作编辑器核心功能测试
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from actions import ACTIONS, sample, smootherstep, export_python

def test_smootherstep():
    """测试插值函数"""
    assert smootherstep(0) == 0, "t=0应该返回0"
    assert smootherstep(1) == 1, "t=1应该返回1"
    assert abs(smootherstep(0.5) - 0.5) < 0.01, "t=0.5应该约等于0.5"
    print("✓ smootherstep 测试通过")

def test_sample():
    """测试采样函数"""
    kfs, dur, desc = ACTIONS["STRETCH"]
    
    # t=0应该返回第一个关键帧
    v0 = sample(0, kfs, dur)
    assert v0 == tuple(kfs[0][1:]), "t=0应该返回第一个关键帧"
    
    # t=dur应该返回最后一个关键帧
    v_end = sample(dur, kfs, dur)
    assert v_end == tuple(kfs[-1][1:]), "t=dur应该返回最后一个关键帧"
    
    # 中间值应该在两个关键帧之间
    v_mid = sample(dur/2, kfs, dur)
    assert len(v_mid) == 6, "应该返回6元组"
    print("✓ sample 测试通过")

def test_all_actions():
    """测试所有动作都能正常采样"""
    for name, (kfs, dur, desc) in ACTIONS.items():
        assert len(kfs) >= 2, f"{name} 至少需要2个关键帧"
        assert kfs[0][0] == 0.0, f"{name} 第一个关键帧时间应该是0"
        assert kfs[-1][0] == dur, f"{name} 最后一个关键帧时间应该等于dur"
        # 测试几个时间点采样
        for t in [0, dur*0.3, dur*0.7, dur]:
            v = sample(t, kfs, dur)
            assert len(v) == 6, f"{name} t={t} 应该返回6元组"
    print(f"✓ 所有 {len(ACTIONS)} 个动作测试通过")

def test_export():
    """测试导出功能"""
    code = export_python("STRETCH")
    assert "STRETCH_KEYFRAMES = [" in code
    assert "STRETCH_DUR = 5.2" in code
    print("✓ export_python 测试通过")

def test_lid_range():
    """测试lid值范围"""
    for name, (kfs, dur, desc) in ACTIONS.items():
        for kf in kfs:
            lid = kf[5]
            assert 0 <= lid <= 1, f"{name} lid={lid} 超出0-1范围"
    print("✓ lid 范围测试通过")

if __name__ == '__main__':
    print("开始测试动作编辑器核心功能...\n")
    test_smootherstep()
    test_sample()
    test_all_actions()
    test_export()
    test_lid_range()
    print("\n✅ 全部测试通过！")
