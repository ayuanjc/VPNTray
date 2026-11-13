# -*- coding: utf-8 -*-
"""内核托管层回归：端口独占、句柄不丢、地理数据按内容比对。"""
from __future__ import annotations

import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vpntray import kernel as K  # noqa: E402


class _Dead:
    """已经退出的进程桩。"""
    pid = 4242
    returncode = 0

    def poll(self):
        return 0


class _Stubborn:
    """terminate / kill 都不生效的进程桩（权限不足、进程卡在驱动里）。"""
    pid = 4343
    returncode = None

    def poll(self):
        return None

    def terminate(self):
        raise OSError("Access denied")

    def kill(self):
        raise OSError("Access denied")

    def wait(self, timeout=None):
        raise OSError("Access denied")


def _kernel():
    k = K.Kernel.__new__(K.Kernel)      # 不走 __init__（会 atexit 注册）
    import threading
    k.proc = None
    k._lock = threading.RLock()
    k._stop = threading.Event()
    k.last_error = ""
    return k


class PortProbe(unittest.TestCase):
    def test_free_port_is_free(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.assertTrue(K.port_free(port))

    def test_listening_port_is_not_free(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            port = srv.getsockname()[1]
            # 关键：只用 bind+SO_REUSEADDR 判断时这里会误判成空闲
            self.assertFalse(K.port_free(port))


class TerminateSafety(unittest.TestCase):
    def test_dead_process_is_released(self):
        k = _kernel()
        k.proc = _Dead()
        k._terminate(0.1)
        self.assertIsNone(k.proc)

    def test_unreapable_process_keeps_handle(self):
        k = _kernel()
        stuck = _Stubborn()
        k.proc = stuck
        k._terminate(0.05)
        # 杀不掉就必须留着句柄：丢了就再也不知道是谁占着端口
        self.assertIs(k.proc, stuck)


class GeoAssets(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.data = self.dir / "data"
        self.data.mkdir()
        (self.dir / "geoip.dat").write_bytes(b"A" * 100)
        (self.dir / "geosite.dat").write_bytes(b"B" * 50)
        (self.dir / "country.mmdb").write_bytes(b"C" * 10)
        self._saved = (K.GEOIP, K.GEOSITE, K.MMDB, K.DATA)
        K.GEOIP = self.dir / "geoip.dat"
        K.GEOSITE = self.dir / "geosite.dat"
        K.MMDB = self.dir / "country.mmdb"
        K.DATA = self.data

    def tearDown(self):
        K.GEOIP, K.GEOSITE, K.MMDB, K.DATA = self._saved
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_copies_and_writes_sidecar(self):
        K.ensure_geo_assets()
        self.assertEqual((self.data / "geoip.dat").read_bytes(), b"A" * 100)
        self.assertTrue((self.data / "geoip.dat.sha256").exists())

    def test_same_size_new_content_is_recopied(self):
        K.ensure_geo_assets()
        # 大小一样、内容不同（升级程序换了新版 geosite）：以前会被当成「已就位」
        K.GEOSITE.write_bytes(b"Z" * 50)
        K.ensure_geo_assets()
        self.assertEqual((self.data / "geosite.dat").read_bytes(), b"Z" * 50)

    def test_second_run_does_not_touch_file(self):
        K.ensure_geo_assets()
        dst = self.data / "geoip.dat"
        before = dst.stat().st_mtime_ns
        K.ensure_geo_assets()
        self.assertEqual(dst.stat().st_mtime_ns, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
