"""
conftest.py — pytest 全局配置

确保 v3 目录始终在 sys.path 最前，防止旧 workspace 的同名模块（guards/、llm_tasks/ 等）
污染测试。
binance_api.py 会在运行时把 workspace 根目录注入 sys.path，导致
`from guards.patrol_sl_check import run` 可能加载到旧版本。
这里在每个测试前重新把 v3 目录置顶，保证优先级正确。
"""
import sys
from pathlib import Path

V3_ROOT = str(Path(__file__).parent.parent)   # /root/.openclaw/workspace/v3


def pytest_runtest_setup(item):
    """每个测试函数执行前，确保 v3 目录在 sys.path[0]"""
    if V3_ROOT not in sys.path:
        sys.path.insert(0, V3_ROOT)
    elif sys.path[0] != V3_ROOT:
        sys.path.remove(V3_ROOT)
        sys.path.insert(0, V3_ROOT)
    # 强制使 guards 缓存失效（防止跨测试污染）
    for key in list(sys.modules.keys()):
        if key.startswith("guards.") or key == "guards":
            del sys.modules[key]
