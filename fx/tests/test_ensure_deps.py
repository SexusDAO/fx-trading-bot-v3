"""运行：cd fx && python3 -m unittest tests.test_ensure_deps -v
[v3.89 #24] _ensure_deps 只检查依赖，缺失时 critical + 退出，不运行 pip。main.py 导入有副作用，按源码抽取。"""
import ast, logging, subprocess, sys, unittest
from pathlib import Path
from unittest import mock

SRC = (Path(__file__).parent.parent / "main.py").read_text(encoding="utf-8")
_node = next(n for n in ast.parse(SRC).body if isinstance(n, ast.FunctionDef) and n.name == "_ensure_deps")


def _load():
    ns = {"logging": logging, "sys": sys, "subprocess": subprocess}
    exec(ast.get_source_segment(SRC, _node), ns)
    return ns["_ensure_deps"]


class TestEnsureDeps(unittest.TestCase):
    def test_missing_dependency_exits_without_pip(self):
        f = _load()
        with mock.patch.dict(sys.modules, {"socks": None}), \
             mock.patch("subprocess.run") as run, \
             self.assertLogs("fx.startup", level="CRITICAL") as cm, \
             self.assertRaises(SystemExit) as ex:
            f()
        self.assertEqual(ex.exception.code, 1)
        run.assert_not_called()
        self.assertTrue(any("PySocks" in m for m in cm.output))

    def test_present_dependency_passes(self):
        f = _load()
        import types
        with mock.patch.dict(sys.modules, {"socks": types.ModuleType("socks")}), \
             mock.patch("subprocess.run") as run:
            f()
        run.assert_not_called()

    def test_source_has_no_pip_install(self):
        self.assertNotIn('"pip"', ast.get_source_segment(SRC, _node))


if __name__ == "__main__":
    unittest.main()
