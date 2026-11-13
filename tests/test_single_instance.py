"""单实例：互斥体判活 + 进程探测。

run.pyw 不是包内模块（它是 PyInstaller 的入口脚本），用 importlib 按路径加载。
"""
import importlib.util
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_run():
    spec = importlib.util.spec_from_file_location("run_entry", ROOT / "run.pyw")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Probe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.run_mod = _load_run()

    def test_other_instances_excludes_self(self):
        if sys.platform != "win32":
            self.skipTest("进程枚举只在 Windows")
        pids = self.run_mod._other_instances()
        self.assertNotIn(os.getpid(), pids)

    def test_mutex_blocks_second(self):
        if sys.platform != "win32":
            self.skipTest("互斥体只在 Windows")
        name = "Local\\VPNTray-test-runpyw"
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        k32.CreateMutexW.restype = wintypes.HANDLE
        h1 = k32.CreateMutexW(None, False, name)
        e1 = ctypes.get_last_error()
        h2 = k32.CreateMutexW(None, False, name)
        e2 = ctypes.get_last_error()
        self.assertEqual(e1, 0)
        self.assertEqual(e2, 183)   # ERROR_ALREADY_EXISTS
        k32.CloseHandle(h1)
        k32.CloseHandle(h2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
