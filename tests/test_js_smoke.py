"""渲染端冒烟：把 3d/index.html 的脚本放进 Node 桩环境跑一遍。

覆盖的是"页面白屏"这一类问题——未定义变量、声明顺序错、远程分支抛异常。
没有 Node 时自动跳过（CI 上装一下即可）。
"""
import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(HERE, "js_smoke.mjs")
PAGE = os.path.join(ROOT, "3d", "index.html")


@pytest.mark.skipif(shutil.which("node") is None, reason="未安装 Node")
def test_renderer_smoke():
    r = subprocess.run([shutil.which("node"), SCRIPT, PAGE],
                       capture_output=True, text=True, timeout=60,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    assert "全部通过" in r.stdout
