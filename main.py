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


def _run_decode_images(cfg, argv):
    """`decode-images` 子命令:批量把 .dat 图片解密成明文图片树。

    与 decrypt 不同,decode-images **不需要** 微信进程在运行,也不需要 DB 密钥
    (只读已存在的 .dat 文件;V2 文件用 config.json 里的 image_aes_key)。
    """
    import argparse
    from decode_image import decode_all_dats

    parser = argparse.ArgumentParser(
        prog="main.py decode-images",
        description=(
            "批量解密微信本地 .dat 图片到明文图片树。"
            "区别于 decode_image.py 单文件 CLI,本子命令扫描 attach_dir 下"
            "全部 .dat,镜像目录结构产出明文(jpg / png / gif / webp / hevc)。"
        ),
    )
    default_base = cfg.get("wechat_base_dir") or os.path.dirname(cfg["db_dir"])
    default_attach = os.path.join(default_base, "msg", "attach")
    default_out = cfg.get("decoded_image_dir", "decoded_images")
    parser.add_argument(
        "--attach-dir", default=None,
        help=f"微信 msg/attach 根目录,覆盖默认推断(默认: {default_attach})",
    )
    parser.add_argument(
        "--decoded-dir", default=None,
        help=f"明文图片输出根目录,覆盖 config.json 的 decoded_image_dir(默认: {default_out})",
    )
    parser.add_argument(
        "--aes-key", default=None,
        help="V2 AES key(16 字节 ASCII 字符串),覆盖 config.json 的 image_aes_key",
    )
    parser.add_argument(
        "--xor-key", default=None,
        help="V2 XOR key(可十进制或 0x 十六进制),覆盖 config.json 的 image_xor_key(默认: 0x88)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="忽略已存在目标重新解密(默认按 basename 跳过)",
    )
    args = parser.parse_args(argv)

    attach_dir = args.attach_dir or default_attach
    out_dir = args.decoded_dir or default_out
    aes_key = args.aes_key if args.aes_key is not None else cfg.get("image_aes_key")
    xor_key_raw = args.xor_key if args.xor_key is not None else cfg.get("image_xor_key", 0x88)
    if isinstance(xor_key_raw, str):
        xor_key = int(xor_key_raw, 0)
    else:
        xor_key = xor_key_raw

    if not os.path.isdir(attach_dir):
        print(f"[ERROR] attach 目录不存在: {attach_dir}", file=sys.stderr)
        sys.exit(1)

    if aes_key is None:
        print(
            "[NOTE] 未配置 image_aes_key,V2 加密图片将被跳过(计入 skipped_no_key);"
            "V1 / 老 XOR 图片不受影响。提取 V2 key 见 README 的图片解密章节。",
            file=sys.stderr,
        )

    print(f"  attach_dir = {attach_dir}")
    print(f"  out_dir    = {out_dir}")
    print(f"  aes_key    = {'已配置' if aes_key else '未配置'}")
    print(f"  xor_key    = 0x{xor_key:02x}")
    print(f"  force      = {args.force}")
    print()

    stats = decode_all_dats(
        attach_dir=attach_dir,
        out_dir=out_dir,
        aes_key=aes_key,
        xor_key=xor_key,
        force=args.force,
    )

    print()
    print("=" * 60)
    print(f"扫描 {stats['total']} 个 .dat 文件")
    print(f"  解码: {stats['decoded']}  跳过(已存在): {stats['skipped']}  "
          f"无 key 跳过: {stats['skipped_no_key']}  失败: {stats['failed']}")
    if stats["formats"]:
        fmt_summary = ", ".join(f"{ext}={n}" for ext, n in sorted(stats["formats"].items()))
        print(f"  按格式: {fmt_summary}")
    print(f"输出在: {out_dir}")

    if stats["failed"] > 0:
        sys.exit(2)


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
    # 走 config._config_file_path() 而不是硬编码 "config.json"
    # 这样打包成 exe 后 (cwd 可能任意位置) 仍能找到正确的 config
    from config import _config_file_path
    config_file = _config_file_path()
    if os.path.exists(config_file):
        with open(config_file, encoding="utf-8") as f:
            cfg = json.load(f)
        print(f"[config] {config_file}")
        print(f"         db_dir = {cfg.get('db_dir', '?')}")
    else:
        print(f"[config] 未找到 {config_file}")

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
    print("  python main.py                启动实时消息监听 (Web UI)")
    print("  python main.py decrypt        解密全部数据库到 decrypted/")
    print("  python main.py decode-images  批量解密 .dat 图片到 decoded_image_dir/")
    print("  python main.py decode-images --help  查看 decode-images 全部选项")
    print("  python main.py export         解密 + 批量导出聊天记录")
    print("  python main.py all            从零到完成：密钥 → 解密 → 导出")
    print("  python main.py status         显示当前状态和磁盘用量")


def _call_with_argv(func, argv):
    """调用子命令 main() 时临时隔离 sys.argv，避免 argparse 读到外层命令。"""
    old_argv = sys.argv[:]
    try:
        sys.argv = argv
        return func()
    finally:
        sys.argv = old_argv


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

    # 早路由:decode-images 不需要微信进程在运行,也不需要 DB 密钥
    if len(sys.argv) > 1 and sys.argv[1] == "decode-images":
        print("[*] 批量解密图片...")
        print()
        _run_decode_images(cfg, sys.argv[2:])
        return

    # 2. 检查微信进程
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
        decrypt_all(sys.argv[2:])

    elif cmd in ("export", "all"):
        print("[*] 开始解密全部数据库...")
        print()
        from decrypt_db import main as decrypt_all
        decrypt_all([])
        print()
        print("[*] 开始批量导出聊天记录...")
        print()
        from export_all_chats import main as export_all
        try:
            export_args = sys.argv[2:] if cmd == "export" else []
            export_all(export_args)
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
