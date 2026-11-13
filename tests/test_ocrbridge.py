"""OCR 桥守护的单元测试。

三件事必须守住，别的都能商量：
1. 端口上的进程不是 node 时**不许**杀（用户自己起的服务不能被我干掉）；
2. restart = kill + start，顺序错了会在旧进程还活着时「重启」出第二个 node；
3. status() 在桥不是本进程拉起的时候也要能报出目录和 pid（面板要显示）。
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vpntray import ocrbridge  # noqa: E402


NETSTAT = """
活动连接

  协议  本地地址          外部地址        状态           PID
  TCP    127.0.0.1:7891         0.0.0.0:0              LISTENING       28472
  TCP    127.0.0.1:8791         0.0.0.0:0              LISTENING       28232
  TCP    127.0.0.1:8791         127.0.0.1:52000        ESTABLISHED     28232
"""


class _CP:
    def __init__(self, out="", err="", code=0):
        self.stdout = out.encode("gbk") if isinstance(out, str) else out
        self.stderr = err.encode("gbk") if isinstance(err, str) else err
        self.returncode = code


class PortLookup(unittest.TestCase):
    def test_finds_listening_pid(self):
        with mock.patch.object(ocrbridge.subprocess, "run", return_value=_CP(NETSTAT)):
            self.assertEqual(ocrbridge._pid_on_port(8791), 28232)

    def test_ignores_established_and_other_ports(self):
        with mock.patch.object(ocrbridge.subprocess, "run", return_value=_CP(NETSTAT)):
            self.assertEqual(ocrbridge._pid_on_port(9999), 0)

    def test_netstat_failure_returns_zero(self):
        with mock.patch.object(ocrbridge.subprocess, "run", side_effect=OSError("no netstat")):
            self.assertEqual(ocrbridge._pid_on_port(8791), 0)

    def test_image_of_parses_tasklist_csv(self):
        with mock.patch.object(ocrbridge.subprocess, "run", return_value=_CP('"node.exe","28232","Console","1","120,000 K"\n')):
            self.assertEqual(ocrbridge._image_of(28232), "node.exe")

    def test_image_of_handles_no_match(self):
        with mock.patch.object(ocrbridge.subprocess, "run", return_value=_CP("信息: 没有运行的任务匹配指定标准。\n")):
            self.assertEqual(ocrbridge._image_of(424242), "")


class KillSafety(unittest.TestCase):
    def test_no_listener(self):
        with mock.patch.object(ocrbridge, "_pid_on_port", return_value=0):
            ok, msg = ocrbridge.kill(8791)
        self.assertFalse(ok)
        self.assertIn("没有监听进程", msg)

    def test_refuses_foreign_process(self):
        with mock.patch.object(ocrbridge, "_pid_on_port", return_value=4321), \
             mock.patch.object(ocrbridge, "_image_of", return_value="python.exe"), \
             mock.patch.object(ocrbridge.subprocess, "run") as run:
            ok, msg = ocrbridge.kill(8791)
        self.assertFalse(ok)
        self.assertIn("python.exe", msg)
        run.assert_not_called()

    def test_kills_own_port_refused(self):
        with mock.patch.object(ocrbridge, "_pid_on_port", return_value=os.getpid()):
            ok, msg = ocrbridge.kill(8791)
        self.assertFalse(ok)
        self.assertIn("自己", msg)

    def test_kills_node(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return _CP("SUCCESS: 已终止进程")

        with mock.patch.object(ocrbridge, "_pid_on_port", return_value=28232), \
             mock.patch.object(ocrbridge, "_image_of", return_value="node.exe"), \
             mock.patch.object(ocrbridge.subprocess, "run", side_effect=fake_run):
            ok, msg = ocrbridge.kill(8791)
        self.assertTrue(ok, msg)
        self.assertEqual(calls[0][:3], ["taskkill", "/PID", "28232"])

    def test_taskkill_failure_reported(self):
        with mock.patch.object(ocrbridge, "_pid_on_port", return_value=28232), \
             mock.patch.object(ocrbridge, "_image_of", return_value="node.exe"), \
             mock.patch.object(ocrbridge.subprocess, "run", return_value=_CP("", "拒绝访问", 1)):
            ok, msg = ocrbridge.kill(8791)
        self.assertFalse(ok)
        self.assertIn("拒绝访问", msg)


class Restart(unittest.TestCase):
    def test_kill_then_start(self):
        order = []
        with mock.patch.object(ocrbridge, "kill", side_effect=lambda port=None: (order.append("kill"), (True, "已结束 pid 1"))[1]), \
             mock.patch.object(ocrbridge, "start", side_effect=lambda **kw: (order.append("start"), (True, "已就绪"))[1]), \
             mock.patch.object(ocrbridge.time, "sleep", return_value=None):
            ok, msg = ocrbridge.restart(port=8791)
        self.assertTrue(ok)
        self.assertEqual(order, ["kill", "start"])
        self.assertIn("已重启", msg)

    def test_start_only_when_nothing_to_kill(self):
        with mock.patch.object(ocrbridge, "kill", return_value=(False, "端口 8791 上没有监听进程")), \
             mock.patch.object(ocrbridge, "start", return_value=(True, "已就绪")):
            ok, msg = ocrbridge.restart(port=8791)
        self.assertTrue(ok)
        self.assertEqual(msg, "已拉起")


class StatusShape(unittest.TestCase):
    def test_reports_dir_and_pid_when_alive(self):
        ocrbridge._state.update({"port": 8791, "dir": "", "started": 0})
        with mock.patch.object(ocrbridge, "health", return_value=(True, "qwen3.5-ocr")), \
             mock.patch.object(ocrbridge, "_resolve_dir", return_value=Path(r"E:\x\ocr-router")), \
             mock.patch.object(ocrbridge, "_pid_on_port", return_value=28232):
            st = ocrbridge.status()
        self.assertTrue(st["ok"])
        self.assertEqual(st["reason"], "qwen3.5-ocr")
        self.assertEqual(st["pid"], 28232)
        self.assertIn("ocr-router", st["dir"])

    def test_dead_bridge_keeps_reason(self):
        ocrbridge._state.update({"port": 8791, "dir": "", "started": 0})
        with mock.patch.object(ocrbridge, "health", return_value=(False, "URLError: refused")):
            st = ocrbridge.status()
        self.assertFalse(st["ok"])
        self.assertIn("refused", st["reason"])


class WebuiRoutes(unittest.TestCase):
    def test_ocr_endpoints_registered(self):
        from vpntray import webui

        class _App:
            settings = {"ocr_bridge": True, "ocr_bridge_port": 8791, "ocr_bridge_dir": ""}

            def __getattr__(self, name):  # 路由表构建只用到 settings，别的一律给个空壳
                return lambda *a, **kw: {"ok": True}

        table = webui.WebUI(_App(), on_quit=None)._table()
        for path in ("/api/ocr/status", "/api/ocr/restart", "/api/ocr/config"):
            self.assertIn(path, table)


if __name__ == "__main__":
    unittest.main(verbosity=2)