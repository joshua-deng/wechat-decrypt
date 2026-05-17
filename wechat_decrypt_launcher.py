#!/usr/bin/env python3
"""WeChatDecrypt.exe 双模式入口。

无参数时启动 Web UI；带参数时分发到现有 CLI 脚本。该入口同时兼容
PyInstaller onefile 里 Web UI 子进程使用的 ``sys.executable script.py``
调用形态。
"""

import os
import runpy
import sys


MAIN_COMMANDS = {"decrypt", "export", "all", "status", "-s", "decode-images"}
HELP_COMMANDS = {"help", "-h", "--help"}
SCRIPT_ENTRYPOINTS = {
    "main.py",
    "monitor_web.py",
    "export_all_chats.py",
    "find_image_key.py",
    "decrypt_sns.py",
    "export_sns.py",
    "find_wxwork_keys.py",
    "decrypt_wxwork_db.py",
    "export_wxwork_messages.py",
    "voice_to_mp3.py",
    "batch_decrypt_images.py",
    "decrypt_db.py",
}


def _app_base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _script_dir():
    return getattr(sys, "_MEIPASS", _app_base_dir())


def _prepare_runtime():
    base_dir = _app_base_dir()
    os.environ["WECHAT_DECRYPT_APP_DIR"] = base_dir
    os.chdir(base_dir)


def _configure_stdio():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def print_usage(stream=None):
    stream = stream or sys.stdout
    stream.write(
        "WeChatDecrypt.exe 用法:\n"
        "\n"
        "  WeChatDecrypt.exe                         启动 Web UI\n"
        "  WeChatDecrypt.exe web                     启动 Web UI\n"
        "  WeChatDecrypt.exe status                  查看状态\n"
        "  WeChatDecrypt.exe decrypt                 提取密钥并解密数据库\n"
        "  WeChatDecrypt.exe export [args...]        解密并批量导出聊天\n"
        "  WeChatDecrypt.exe all                     密钥 -> 解密 -> 导出\n"
        "  WeChatDecrypt.exe decode-images [args...] 批量解密 .dat 图片\n"
        "  WeChatDecrypt.exe export-all [args...]    直接调用 export_all_chats.py\n"
        "\n"
        "兼容脚本式调用:\n"
        "  WeChatDecrypt.exe main.py decrypt\n"
        "  WeChatDecrypt.exe export_all_chats.py --write-plan-csv export_plan.csv\n"
    )


def _resolve_script(argv):
    if not argv:
        return "monitor_web.py", []

    cmd = argv[0]
    if cmd == "web":
        return "monitor_web.py", argv[1:]
    if cmd in MAIN_COMMANDS:
        return "main.py", argv
    if cmd == "export-all":
        return "export_all_chats.py", argv[1:]
    if cmd in SCRIPT_ENTRYPOINTS:
        return cmd, argv[1:]
    return None, None


def run_script(script, script_args):
    _prepare_runtime()
    script_path = os.path.join(_script_dir(), script)
    if not os.path.exists(script_path):
        script_path = os.path.join(_app_base_dir(), script)
    if not os.path.exists(script_path):
        raise FileNotFoundError(f"脚本不存在: {script_path}")

    old_argv = sys.argv[:]
    try:
        sys.argv = [script] + list(script_args)
        try:
            runpy.run_path(script_path, run_name="__main__")
        except SystemExit as exc:
            if exc.code is None:
                return 0
            if isinstance(exc.code, int):
                return exc.code
            print(exc.code, file=sys.stderr)
            return 1
        return 0
    finally:
        sys.argv = old_argv


def dispatch(argv=None, script_runner=None, stdout=None, stderr=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    runner = script_runner or run_script

    if argv and argv[0] in HELP_COMMANDS:
        print_usage(stdout)
        return 0

    script, script_args = _resolve_script(argv)
    if script is None:
        print_usage(stdout)
        print(f"未知命令: {argv[0]}", file=stderr)
        return 2
    return runner(script, script_args)


def main():
    _configure_stdio()
    sys.exit(dispatch())


if __name__ == "__main__":
    main()
