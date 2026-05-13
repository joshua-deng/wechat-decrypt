"""
WeChat Decrypt 一键启动

python main.py          # 提取密钥 + 启动 Web UI
python main.py decrypt  # 提取密钥 + 解密全部数据库
"""
import json
import os
import platform
import sys
import subprocess

import functools
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
        # 检查密钥是否匹配当前 db_dir（防止切换账号后误复用旧密钥）
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

    # 提取后再次检查
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


def main():
    print("=" * 60)
    print("  WeChat Decrypt")
    print("=" * 60)
    print()

    # 1. 加载配置（自动检测 db_dir）
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

    # 3. 提取密钥
    ensure_keys(cfg["keys_file"], cfg["db_dir"])

    # 4. 根据子命令执行
    cmd = sys.argv[1] if len(sys.argv) > 1 else "web"

    if cmd == "decrypt":
        print("[*] 开始解密全部数据库...")
        print()
        from decrypt_db import main as decrypt_all
        decrypt_all()
    elif cmd == "web":
        print("[*] 启动 Web UI...")
        print()
        from monitor_web import main as start_web
        start_web()
    else:
        print(f"[!] 未知命令: {cmd}")
        print()
        print("用法:")
        print("  python main.py                启动实时消息监听 (Web UI)")
        print("  python main.py decrypt        解密全部数据库到 decrypted/")
        print("  python main.py decode-images  批量解密 .dat 图片到 decoded_image_dir/")
        print("  python main.py decode-images --help    查看 decode-images 全部选项")
        sys.exit(1)


if __name__ == "__main__":
    main()
