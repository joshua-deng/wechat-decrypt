"""
WeChat Decrypt 一键启动

python main.py               # 提取密钥 + 启动 Web UI
python main.py decrypt       # 提取密钥 + 解密全部数据库
python main.py export        # 提取密钥 + 解密 + 批量导出聊天记录
python main.py all           # 从零到完成：密钥 → 解密 → 导出
python main.py status        # 显示当前数据状态
"""

import functools
import glob
import json
import os
import platform
import subprocess
import sys

print = functools.partial(print, flush=True)

from key_utils import strip_key_metadata


def check_wechat_running():
    """检查微信是否在运行，返回 True/False"""
    if platform.system().lower() == "darwin":
        return subprocess.run(["pgrep", "-x", "WeChat"], capture_output=True).returncode == 0
    from find_all_keys import get_pids
    try:
        get_pids()
        return True
    except RuntimeError:
        return False


def ensure_keys(keys_file, db_dir):
    """确保密钥文件存在且匹配当前 db_dir，否则重新提取"""
    if os.path.exists(keys_file):
        try:
            with open(keys_file, encoding="utf-8") as f:
                keys = json.load(f)
        except (json.JSONDecodeError, ValueError):
            keys = {}
        saved_dir = keys.pop("_db_dir", None)
        if saved_dir and os.path.normcase(os.path.normpath(saved_dir)) != os.path.normcase(os.path.normpath(db_dir)):
            print(f"[!] 密钥文件对应的目录已变更，需要重新提取")
            print(f"    旧: {saved_dir}")
            print(f"    新: {db_dir}")
            keys = {}
        keys = strip_key_metadata(keys)
        if keys:
            print(f"[+] 已有 {len(keys)} 个数据库密钥")
            return

    print("[*] 密钥文件不存在，正在从微信进程提取...")
    print()
    from find_all_keys import main as extract_keys
    try:
        extract_keys()
    except RuntimeError as e:
        print(f"\n[!] 密钥提取失败: {e}")
        sys.exit(1)
    print()

    if not os.path.exists(keys_file):
        print("[!] 密钥提取失败")
        sys.exit(1)
    try:
        with open(keys_file, encoding="utf-8") as f:
            keys = json.load(f)
    except (json.JSONDecodeError, ValueError):
        keys = {}
    if not strip_key_metadata(keys):
        print("[!] 未能提取到任何密钥")
        print("    可能原因：选择了错误的微信数据目录，或微信需要重启")
        print("    请检查 config.json 中的 db_dir 是否与当前登录的微信账号匹配")
        sys.exit(1)


def show_status():
    """显示当前数据状态"""
    cfg = {}
    config_file = "config.json"
    if os.path.exists(config_file):
        with open(config_file, encoding="utf-8") as f:
            cfg = json.load(f)
        print(f"[config] db_dir = {cfg.get('db_dir', '?')}")
    else:
        print("[config] 未找到 config.json")

    keys_files = sorted(glob.glob("all_keys*.json"))
    print(f"[keys]   {len(keys_files)} 个密钥文件")
    for kf in keys_files:
        sz = os.path.getsize(kf) / 1024
        print(f"         {kf} ({sz:.0f} KB)")

    decrypted_dir = cfg.get("decrypted_dir", "decrypted")
    if os.path.exists(decrypted_dir):
        dbs = glob.glob(os.path.join(decrypted_dir, "**/*.db"), recursive=True)
        total_mb = sum(os.path.getsize(f) for f in dbs) / 1024 / 1024
        print(f"[decrypt] {len(dbs)} 个数据库 ({total_mb:.0f} MB)")
        # 检查是否有消息内容（约略估计是否已导出）
        for db in dbs:
            if "message" in os.path.basename(db):
                sz = os.path.getsize(db) / 1024 / 1024
                print(f"          消息库: {len([d for d in dbs if 'message' in d])} 个 ({sz:.0f} MB)")
                break
    else:
        print("[decrypt] 未解密 (运行: python main.py decrypt)")

    exported_dir = "exported_chats"
    if os.path.exists(exported_dir):
        jsons = [f for f in glob.glob(os.path.join(exported_dir, "*.json"))
                 if not f.endswith("_transcribed.json")]
        tx_jsons = glob.glob(os.path.join(exported_dir, "*_transcribed.json"))
        total_sz = sum(os.path.getsize(f) for f in jsons) / 1024 / 1024
        print(f"[export]  {len(jsons)} 个 JSON ({total_sz:.0f} MB)")
    else:
        print("[export]  未导出 (运行: python main.py export)")

    if os.path.exists(exported_dir):
        total_voice = 0
        total_tx = 0
        for jp in glob.glob(os.path.join(exported_dir, "*_transcribed.json")):
            try:
                with open(jp, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            if isinstance(data, dict) and "chats" in data:
                for chat in data["chats"]:
                    for m in chat.get("messages", []):
                        if m.get("type") == "voice":
                            total_voice += 1
                            if m.get("transcription"):
                                total_tx += 1
            elif isinstance(data, dict):
                for m in data.get("messages", []):
                    if m.get("type") == "voice":
                        total_voice += 1
                        if m.get("transcription"):
                            total_tx += 1
        if total_voice > 0:
            pct = total_tx * 100 // max(total_voice, 1)
            print(f"[transcribe] {total_tx}/{total_voice} ({pct}%) 条语音已转录")

    # 建议的下一步
    print()
    steps = []
    if not os.path.exists(decrypted_dir):
        steps.append("python main.py decrypt  — 解密数据库")
    elif not os.path.exists(exported_dir):
        steps.append("main.py export — 导出聊天记录")
    if steps:
        print("建议的下一步:")
        for s in steps:
            print(f"  {s}")
    else:
        print("所有步骤已完成。")


def print_usage():
    print("用法:")
    print("  python main.py              启动实时消息监听 (Web UI)")
    print("  python main.py decrypt      解密全部数据库到 decrypted/")
    print("  python main.py export       解密 + 批量导出聊天记录")
    print("  python main.py all          从零到完成：密钥 → 解密 → 导出")
    print("  python main.py status       显示当前状态和磁盘用量")


def main():
    print("=" * 60)
    print("  WeChat Decrypt")
    print("=" * 60)
    print()

    cmd = sys.argv[1] if len(sys.argv) > 1 else "web"

    # help / status 不需要密钥和微信进程
    if cmd in ("help", "-h", "--help"):
        print_usage()
        return
    if cmd in ("status", "-s"):
        show_status()
        return

    # 以下命令需要配置 + 微信进程
    from config import load_config
    cfg = load_config()

    if not check_wechat_running():
        print(f"[!] 未检测到微信进程 ({cfg.get('wechat_process', 'WeChat')})")
        print("    请先启动微信并登录，然后重新运行")
        sys.exit(1)
    print("[+] 微信进程运行中")

    ensure_keys(cfg["keys_file"], cfg["db_dir"])

    if cmd == "decrypt":
        print("[*] 开始解密全部数据库...")
        print()
        from decrypt_db import main as decrypt_all
        decrypt_all()

    elif cmd in ("export", "all"):
        print("[*] 开始解密全部数据库...")
        print()
        from decrypt_db import main as decrypt_all
        decrypt_all()
        print()
        print("[*] 开始批量导出聊天记录...")
        print()
        from export_all_chats import main as export_all
        try:
            export_all()
        except SystemExit:
            pass

        if cmd == "all" and os.path.exists("exported_chats"):
            print()
            print("[*] 检查语音转录配置...")
            from config import load_config
            cfg2 = load_config()
            from mcp_server import _resolve_active_backend
            backend = _resolve_active_backend()
            if backend and backend != "local":
                print(f"    检测到 backend = {backend}")
                print("    如需转录语音，运行: python export_all_chats.py --with-transcriptions")
            else:
                print("    未配置语音转录 backend (config.json 中设置)")
                print("    配置后运行: python export_all_chats.py --with-transcriptions")

    elif cmd == "web":
        print("[*] 启动 Web UI...")
        print()
        from monitor_web import main as start_web
        start_web()

    else:
        print(f"[!] 未知命令: {cmd}")
        print()
        print_usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
