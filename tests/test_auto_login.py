"""通过脚本入口模拟 OpenWrt 命令与认证服务，不改变真实网卡。"""
import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import auto_login as app


class Router:
    def __init__(self, ip="10.111.97.173", reachable=True, leases=None):
        self.ip = ip
        self.reachable = reachable
        self.leases = leases or {}
        self.mac = "38:02:E3:08:70:A5"
        self.original = self.mac
        self.config = self.mac
        self.changes = []
        self.authenticated = False
        self.reconnects = 0

    def command(self, args, **kwargs):
        out, rc = "", 0
        if args[0] == "ifstatus":
            addresses = [{"address": self.ip, "mask": 19}] if self.ip else []
            out = json.dumps({"device": "wan", "l3_device": "wan", "ipv4-address": addresses})
        elif args[:3] == ["ip", "link", "show"]:
            out = "3: wan: <UP>\n    link/ether {} brd ff:ff:ff:ff:ff:ff\n".format(self.mac)
        elif args[:3] == ["uci", "-q", "show"]:
            out = "network.wan=interface\nnetwork.wan.device='wan'\nnetwork.@device[1]=device\nnetwork.@device[1].name='wan'\n"
            if self.config:
                out += "network.@device[1].macaddr='{}'\n".format(self.config)
        elif args[:2] == ["uci", "set"]:
            self.config = args[2].split("=", 1)[1]
            self.changes.append(self.config)
        elif args[:3] == ["uci", "-q", "delete"]:
            rc = 0 if self.config is not None else 1
            self.config = None
        elif args[:2] == ["uci", "commit"]:
            pass
        elif args[:2] == ["ubus", "call"]:
            if self.config:
                self.mac = self.config
        elif args[0] == "ifdown":
            pass
        elif args[0] == "ifup":
            self.reconnects += 1
            if self.mac in self.leases:
                self.ip, self.reachable = self.leases[self.mac]
        elif args[:3] == ["ip", "link", "set"]:
            self.mac = args[-1]
        elif args[0] == "ping":
            rc = 0 if self.reachable else 1
        else:
            raise AssertionError("未模拟的命令: {}".format(args))
        return SimpleNamespace(stdout=out, stderr="", returncode=rc)

    def get(self, url, **kwargs):
        if url == app.CHECK_URL:
            location = "http://10.10.16.101:8080/eportal/index.jsp?wlanuserip={}&mac={}&wlanacname=ac".format(self.ip, self.mac)
            return SimpleNamespace(status_code=204 if self.authenticated else 302, text="", headers={"Location": location})
        if url.startswith(app.API_OAUTH):
            return SimpleNamespace(status_code=302, text="", headers={"Location": "http://gateway/?code=testcode1234567890123456"})
        if "code=" in url:
            if not self.reachable:
                raise app.requests.exceptions.ConnectTimeout("模拟认证连接超时")
            self.authenticated = True
        elif not self.reachable:
            raise app.requests.exceptions.ConnectTimeout("模拟认证网关连接超时")
        return SimpleNamespace(status_code=200, text="", headers={})

    def session(self):
        return SimpleNamespace(headers={}, cookies=[], get=self.get,
                               post=lambda *a, **k: SimpleNamespace(json=lambda: {"data": {"token": "test-token"}}))


class AutoLoginTests(unittest.TestCase):
    def invoke(self, router, state=None):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            state_path = str(Path(directory) / "recovery.json")
            if state is not None:
                Path(state_path).write_text(json.dumps(state), encoding="utf-8")
            # 原入口尚无状态常量时，也允许先运行正常流程基线。
            stack.enter_context(patch.object(app, "MAC_STATE_FILE", state_path, create=True))
            stack.enter_context(patch.object(app.subprocess, "run", router.command))
            stack.enter_context(patch.object(app.requests, "get", router.get))
            stack.enter_context(patch.object(app.requests, "Session", router.session))
            stack.enter_context(patch.object(app.time, "sleep"))
            output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            result = app.main()
            persisted = json.loads(Path(state_path).read_text()) if Path(state_path).exists() else None
            return result, output.getvalue(), persisted

    def test_normal_login_preserves_original_mac(self):
        router = Router()
        result, logs, _ = self.invoke(router)
        self.assertEqual(result, 0)
        self.assertTrue(router.authenticated)
        self.assertEqual(router.mac, router.original)
        self.assertEqual(router.changes, [])
        self.assertEqual(router.reconnects, 0)
        self.assertIn("SUCCESS! Network is up.", logs)

    def test_drift_rotates_then_completes_login(self):
        router = Router("10.111.105.88", False, {
            "38:02:E3:08:70:A6": ("10.111.105.90", False),
            "38:02:E3:08:70:A7": ("10.111.97.173", True),
        })
        result, logs, _ = self.invoke(router)
        self.assertEqual(result, 0)
        self.assertEqual(router.mac, "38:02:E3:08:70:A7")
        self.assertTrue(router.authenticated)
        self.assertIn("MAC 轮换成功，网关可达", logs)
        self.assertIn("清理旧设备", logs)

    def test_online_network_skips_rotation_despite_unreachable_auth_gateway(self):
        router = Router("10.111.105.88", False, {
            "38:02:E3:08:70:A6": ("10.111.97.173", True),
        })
        router.authenticated = True
        result, logs, state = self.invoke(router)
        self.assertEqual(result, 0)
        self.assertEqual(router.mac, "38:02:E3:08:70:A5")
        self.assertEqual(router.changes, [])
        self.assertEqual(router.reconnects, 0)
        self.assertIsNone(state)
        self.assertNotIn("启动 MAC 轮换", logs)

    def test_all_candidates_fail_restores_original_mac_and_configuration(self):
        router = Router("10.111.105.88", False)
        result, logs, state = self.invoke(router)
        self.assertEqual(result, 1)
        self.assertEqual(router.mac, "38:02:E3:08:70:A5")
        self.assertEqual(router.config, "38:02:E3:08:70:A5")
        self.assertFalse(router.authenticated)
        self.assertFalse(state["pending"])
        self.assertIn("所有候选 MAC 均失败", logs)
        self.assertIn("已恢复原始 MAC", logs)

    def test_cooldown_skips_mac_changes_and_redial(self):
        router = Router("10.111.105.88", False)
        result, logs, _ = self.invoke(router, {"last_attempt": time.time(), "pending": False})
        self.assertEqual(result, 1)
        self.assertEqual(router.changes, [])
        self.assertEqual(router.reconnects, 0)
        self.assertIn("冷却期", logs)

    def test_missing_ip_does_not_trigger_rotation(self):
        router = Router("", False)
        result, _, state = self.invoke(router)
        self.assertEqual(result, 1)
        self.assertEqual(router.changes, [])
        self.assertEqual(router.reconnects, 1)
        self.assertIsNone(state)

    def test_different_prefix_with_reachable_gateway_logs_in_normally(self):
        router = Router("10.111.105.88", True)
        result, _, _ = self.invoke(router)
        self.assertEqual(result, 0)
        self.assertEqual(router.changes, [])
        self.assertTrue(router.authenticated)
        self.assertEqual(router.reconnects, 0)

    def test_original_prefix_with_unreachable_gateway_does_not_rotate(self):
        router = Router("10.111.97.173", False)
        result, logs, state = self.invoke(router)
        self.assertEqual(result, 1)
        self.assertEqual(router.changes, [])
        self.assertEqual(router.reconnects, 0)
        self.assertIsNone(state)
        self.assertIn("直接登录", logs)
        self.assertIn("ConnectTimeout", logs)

    def test_api_failure_never_rotates_mac(self):
        router = Router()
        with patch.object(router, "session", side_effect=app.requests.exceptions.ConnectionError()):
            result, logs, state = self.invoke(router)
        self.assertEqual(result, 1)
        self.assertEqual(router.changes, [])
        self.assertEqual(router.reconnects, 0)
        self.assertIsNone(state)
        self.assertIn("ConnectionError", logs)

    def test_interrupted_rotation_restores_before_cooldown(self):
        router = Router("10.111.105.88", False)
        router.mac = router.config = "38:02:E3:08:70:A6"
        original = {"device": "wan", "mac": "38:02:E3:08:70:A5",
                    "key": "network.@device[1].macaddr", "value": "38:02:E3:08:70:A5"}
        result, logs, state = self.invoke(router, {"last_attempt": time.time(), "pending": True, "original": original})
        self.assertEqual(result, 1)
        self.assertEqual(router.mac, "38:02:E3:08:70:A5")
        self.assertFalse(state["pending"])
        self.assertIn("上次中断", logs)

    def test_failure_restores_mac_when_original_config_has_no_override(self):
        router = Router("10.111.105.88", False)
        router.config = None
        result, _, _ = self.invoke(router)
        self.assertEqual(result, 1)
        self.assertEqual(router.mac, "38:02:E3:08:70:A5")
        self.assertIsNone(router.config)

    def test_state_file_cannot_be_written_never_changes_wan(self):
        router = Router("10.111.105.88", False)
        with patch.object(app.os, "replace", side_effect=OSError("状态文件不能写入")):
            result, _, _ = self.invoke(router)
        self.assertEqual(result, 1)
        self.assertEqual(router.changes, [])
        self.assertEqual(router.reconnects, 0)

    def test_interrupted_restore_handles_already_deleted_mac_option(self):
        router = Router("10.111.105.88", False)
        router.config = None
        router.mac = "38:02:E3:08:70:A6"
        original = {"device": "wan", "mac": "38:02:E3:08:70:A5",
                    "key": "network.@device[1].macaddr", "value": None}
        result, logs, state = self.invoke(router, {"last_attempt": time.time(), "pending": True, "original": original})
        self.assertEqual(router.mac, "38:02:E3:08:70:A5")
        self.assertIsNone(router.config)
        self.assertFalse(state["pending"])


if __name__ == "__main__":
    unittest.main()
