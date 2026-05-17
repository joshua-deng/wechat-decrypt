import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import wechat_decrypt_launcher
import config


class LauncherDispatchTests(unittest.TestCase):
    def _dispatch(self, argv):
        calls = []
        stdout = io.StringIO()
        stderr = io.StringIO()

        def runner(script, script_args):
            calls.append((script, script_args))
            return 0

        code = wechat_decrypt_launcher.dispatch(
            argv,
            script_runner=runner,
            stdout=stdout,
            stderr=stderr,
        )
        return code, calls, stdout.getvalue(), stderr.getvalue()

    def test_no_args_starts_web_ui(self):
        code, calls, _, _ = self._dispatch([])

        self.assertEqual(code, 0)
        self.assertEqual(calls, [("monitor_web.py", [])])

    def test_web_command_starts_web_ui(self):
        code, calls, _, _ = self._dispatch(["web"])

        self.assertEqual(code, 0)
        self.assertEqual(calls, [("monitor_web.py", [])])

    def test_decrypt_command_routes_to_main(self):
        code, calls, _, _ = self._dispatch(["decrypt"])

        self.assertEqual(code, 0)
        self.assertEqual(calls, [("main.py", ["decrypt"])])

    def test_export_all_command_routes_to_export_all_chats(self):
        code, calls, _, _ = self._dispatch([
            "export-all",
            "--write-plan-csv",
            "export_plan.csv",
        ])

        self.assertEqual(code, 0)
        self.assertEqual(
            calls,
            [(
                "export_all_chats.py",
                ["--write-plan-csv", "export_plan.csv"],
            )],
        )

    def test_script_style_main_py_call_routes_to_main(self):
        code, calls, _, _ = self._dispatch(["main.py", "decrypt"])

        self.assertEqual(code, 0)
        self.assertEqual(calls, [("main.py", ["decrypt"])])

    def test_unknown_command_returns_error_without_running_script(self):
        code, calls, stdout, stderr = self._dispatch(["unknown"])

        self.assertEqual(code, 2)
        self.assertEqual(calls, [])
        self.assertIn("未知命令", stderr)
        self.assertIn("WeChatDecrypt.exe", stdout)

    def test_prepare_runtime_overrides_stale_meipass_app_dir(self):
        with tempfile.TemporaryDirectory() as exe_dir, \
             tempfile.TemporaryDirectory() as stale_dir, \
             patch.object(wechat_decrypt_launcher.sys, "frozen", True, create=True), \
             patch.object(
                 wechat_decrypt_launcher.sys,
                 "executable",
                 os.path.join(exe_dir, "WeChatDecrypt.exe"),
             ), \
             patch.dict(os.environ, {"WECHAT_DECRYPT_APP_DIR": stale_dir}):
            old_cwd = os.getcwd()
            try:
                wechat_decrypt_launcher._prepare_runtime()
                self.assertEqual(os.environ["WECHAT_DECRYPT_APP_DIR"], exe_dir)
                self.assertEqual(os.getcwd(), exe_dir)
            finally:
                os.chdir(old_cwd)

    def test_configure_stdio_uses_utf8(self):
        stream = io.TextIOWrapper(io.BytesIO(), encoding="cp936")

        with patch.object(wechat_decrypt_launcher.sys, "stdout", stream), \
             patch.object(wechat_decrypt_launcher.sys, "stderr", stream):
            wechat_decrypt_launcher._configure_stdio()

        self.assertEqual(stream.encoding.lower().replace("_", "-"), "utf-8")

    def test_pyinstaller_spec_includes_sqlite_hidden_imports(self):
        with open("WeChatDecrypt.spec", encoding="utf-8") as f:
            spec = f.read()

        self.assertIn("'sqlite3'", spec)
        self.assertIn("'_sqlite3'", spec)
        self.assertIn("'wave'", spec)
        self.assertIn("'mcp.server.fastmcp'", spec)
        self.assertIn("'Crypto.Util'", spec)
        self.assertIn("'Crypto.Util.Padding'", spec)

    def test_config_file_path_uses_app_dir_even_when_config_missing(self):
        with tempfile.TemporaryDirectory() as app_dir, \
             patch.dict(os.environ, {"WECHAT_DECRYPT_APP_DIR": app_dir}):
            expected = os.path.join(app_dir, "config.json")

            self.assertEqual(config._config_file_path(), expected)

    def test_mcp_server_import_allows_missing_keys_file(self):
        with tempfile.TemporaryDirectory() as app_dir:
            env = dict(os.environ)
            env["WECHAT_DECRYPT_APP_DIR"] = app_dir
            result = subprocess.run(
                [
                    sys.executable,
                    "-X",
                    "utf8",
                    "-c",
                    "import mcp_server; print(mcp_server.ALL_KEYS)",
                ],
                cwd=os.getcwd(),
                env=env,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("{}", result.stdout)


if __name__ == "__main__":
    unittest.main()
