"""
配置加载器 - 从 config.json 读取路径配置
首次运行时自动检测微信数据目录，检测失败则提示手动配置
"""
import glob
import json
import os
import platform
import sys

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# 打包后 __file__ 指向临时目录，优先使用环境变量指定的 exe 所在目录。
def _app_base_dir():
    d = os.environ.get("WECHAT_DECRYPT_APP_DIR")
    if d and os.path.isdir(d):
        return d
    return os.path.dirname(os.path.abspath(__file__))


def _config_file_path():
    if os.environ.get("WECHAT_DECRYPT_APP_DIR"):
        return os.path.join(_app_base_dir(), "config.json")
    p = os.path.join(_app_base_dir(), "config.json")
    if os.path.exists(p):
        return p
    return CONFIG_FILE


_SYSTEM = platform.system().lower()

if _SYSTEM == "linux":
    _DEFAULT_TEMPLATE_DIR = os.path.expanduser("~/Documents/xwechat_files/your_wxid/db_storage")
    _DEFAULT_PROCESS = "wechat"
elif _SYSTEM == "darwin":
    # macOS 使用独立的 C 扫描器 (find_all_keys_macos.c)，此处仅提供 config 默认值
    _DEFAULT_TEMPLATE_DIR = os.path.expanduser(
        "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/your_wxid/db_storage"
    )
    _DEFAULT_PROCESS = "WeChat"
else:
    _DEFAULT_TEMPLATE_DIR = r"D:\xwechat_files\your_wxid\db_storage"
    _DEFAULT_PROCESS = "Weixin.exe"

_DEFAULT = {
    "db_dir": _DEFAULT_TEMPLATE_DIR,
    "keys_file": "all_keys.json",
    "decrypted_dir": "decrypted",
    "decoded_image_dir": "decoded_images",
    "wechat_process": _DEFAULT_PROCESS,
    "wxwork_db_dir": "",
    "wxwork_keys_file": "wxwork_keys.json",
    "wxwork_decrypted_dir": "wxwork_decrypted",
    "wxwork_export_dir": "wxwork_export",
    "wxwork_process": "WXWork.exe",
    # 语音转录后端: "local" (默认, 本地 Whisper) 或 "openai" (OpenAI API)
    # 切到 openai 时语音将上传至 OpenAI 服务器, 详见 README "语音转录隐私" 章节
    "transcription_backend": "local",
    "local_whisper_model": "base",
    "openai_api_key": "",
}


def _choose_candidate(candidates):
    """在多个候选目录中选择一个。"""
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        if (
            os.environ.get("WECHAT_DECRYPT_NONINTERACTIVE") == "1"
            or os.environ.get("WECHAT_DECRYPT_GUI") == "1"
            or not sys.stdin.isatty()
        ):
            return candidates[0]
        print("[!] 检测到多个微信数据目录（请选择当前正在运行的微信账号）:")
        for i, c in enumerate(candidates, 1):
            print(f"    {i}. {c}")
        print("    0. 跳过，稍后手动配置")
        try:
            while True:
                choice = input("请选择 [0-{}]: ".format(len(candidates))).strip()
                if choice == "0":
                    return None
                if choice.isdigit() and 1 <= int(choice) <= len(candidates):
                    return candidates[int(choice) - 1]
                print("    无效输入，请重新选择")
        except (EOFError, KeyboardInterrupt):
            print()
            return None
    return None


def _auto_detect_db_dir_windows():
    """从微信本地配置自动检测 Windows db_storage 路径。

    读取 %APPDATA%\\Tencent\\xwechat\\config\\*.ini，
    找到数据存储根目录，然后匹配 xwechat_files\\*\\db_storage。
    """
    appdata = os.environ.get("APPDATA", "")
    config_dir = os.path.join(appdata, "Tencent", "xwechat", "config")
    if not os.path.isdir(config_dir):
        return None

    # 从 ini 文件中找到有效的目录路径
    data_roots = []
    for ini_file in glob.glob(os.path.join(config_dir, "*.ini")):
        try:
            # 微信 ini 可能是 utf-8 或 gbk 编码（中文路径）
            content = None
            for enc in ("utf-8", "gbk"):
                try:
                    with open(ini_file, "r", encoding=enc) as f:
                        content = f.read(1024).strip()
                    break
                except UnicodeDecodeError:
                    continue
            if not content or any(c in content for c in "\n\r\x00"):
                continue
            if os.path.isdir(content):
                data_roots.append(content)
        except OSError:
            continue

    # 在每个根目录下搜索 xwechat_files\*\db_storage
    seen = set()
    candidates = []
    for root in data_roots:
        pattern = os.path.join(root, "xwechat_files", "*", "db_storage")
        for match in glob.glob(pattern):
            normalized = os.path.normcase(os.path.normpath(match))
            if os.path.isdir(match) and normalized not in seen:
                seen.add(normalized)
                candidates.append(match)

    return _choose_candidate(candidates)


def _auto_detect_db_dir_linux():
    """自动检测 Linux 微信 db_storage 路径。

    优先搜索当前用户的 home 目录。以 sudo 运行时通过 SUDO_USER 回退到
    实际用户的 home，避免只搜索 /root 而遗漏真实数据目录。
    """
    seen = set()
    candidates = []
    search_roots = [
        os.path.expanduser("~/Documents/xwechat_files"),
    ]
    # sudo 运行时，~ 展开为 /root；回退到实际用户的 home
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        # 验证 SUDO_USER 是合法系统用户，防止路径注入
        import pwd
        try:
            sudo_home = pwd.getpwnam(sudo_user).pw_dir
        except KeyError:
            sudo_home = None
        if sudo_home:
            fallback = os.path.join(sudo_home, "Documents", "xwechat_files")
            if fallback not in search_roots:
                search_roots.append(fallback)

    for root in search_roots:
        if not os.path.isdir(root):
            continue
        pattern = os.path.join(root, "*", "db_storage")
        for match in glob.glob(pattern):
            normalized = os.path.normcase(os.path.normpath(match))
            if os.path.isdir(match) and normalized not in seen:
                seen.add(normalized)
                candidates.append(match)

    # 早期 Linux 微信版本（wine/容器方案）使用的数据路径
    old_path = os.path.expanduser("~/.local/share/weixin/data/db_storage")
    if os.path.isdir(old_path):
        normalized = os.path.normcase(os.path.normpath(old_path))
        if normalized not in seen:
            candidates.append(old_path)

    # 优先使用最近活跃账号：按 message 目录 mtime 降序（近似排序，best-effort）
    def _mtime(path):
        msg_dir = os.path.join(path, "message")
        target = msg_dir if os.path.isdir(msg_dir) else path
        try:
            return os.path.getmtime(target)
        except OSError:
            return 0

    candidates.sort(key=_mtime, reverse=True)
    return _choose_candidate(candidates)


def _auto_detect_db_dir_macos():
    """自动检测 macOS 微信 db_storage 路径。

    微信 4.x 数据目录位于 ~/Library/Containers/com.tencent.xinWeChat/.../xwechat_files/<wxid>/db_storage，
    路径中包含随机 hash，需要搜索定位。
    """
    base = os.path.expanduser(
        "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
    )
    if not os.path.isdir(base):
        return None

    seen = set()
    candidates = []
    pattern = os.path.join(base, "*", "db_storage")
    for match in glob.glob(pattern):
        normalized = os.path.normcase(os.path.normpath(match))
        if os.path.isdir(match) and normalized not in seen:
            seen.add(normalized)
            candidates.append(match)

    # 优先使用最近活跃账号：按 message 目录 mtime 降序
    def _mtime(path):
        msg_dir = os.path.join(path, "message")
        target = msg_dir if os.path.isdir(msg_dir) else path
        try:
            return os.path.getmtime(target)
        except OSError:
            return 0

    candidates.sort(key=_mtime, reverse=True)
    return _choose_candidate(candidates)


def auto_detect_db_dir():
    if _SYSTEM == "windows":
        return _auto_detect_db_dir_windows()
    if _SYSTEM == "linux":
        return _auto_detect_db_dir_linux()
    if _SYSTEM == "darwin":
        return _auto_detect_db_dir_macos()
    return None


def load_config():
    cfg = {}
    config_file = _config_file_path()
    if os.path.exists(config_file):
        try:
            with open(config_file, encoding="utf-8") as f:
                cfg = json.load(f)
        except json.JSONDecodeError:
            print(f"[!] {config_file} 格式损坏，将使用默认配置")
            cfg = {}
    # db_dir 缺失或仍为模板值时，尝试自动检测
    db_dir = cfg.get("db_dir", "")
    if not db_dir or db_dir == _DEFAULT_TEMPLATE_DIR or "your_wxid" in db_dir:
        detected = auto_detect_db_dir()
        if detected:
            print(f"[+] 自动检测到微信数据目录: {detected}")
            cfg = {**_DEFAULT, **cfg, "db_dir": detected}
            with open(config_file, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=4, ensure_ascii=False)
            print(f"[+] 已保存到: {config_file}")
        else:
            if not os.path.exists(config_file):
                with open(config_file, "w", encoding="utf-8") as f:
                    json.dump(_DEFAULT, f, indent=4, ensure_ascii=False)
            print(f"[!] 未能自动检测微信数据目录")
            print(f"    请手动编辑 {config_file} 中的 db_dir 字段")
            if _SYSTEM == "linux":
                print("    Linux 默认路径类似: ~/Documents/xwechat_files/<wxid>/db_storage")
            elif _SYSTEM == "darwin":
                print("    macOS 默认路径类似: ~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<wxid>/db_storage")
            else:
                print(f"    路径可在 微信设置 → 文件管理 中找到")
            sys.exit(1)
    else:
        cfg = {**_DEFAULT, **cfg}

    # 将相对路径转为绝对路径
    base = _app_base_dir()
    for key in (
        "keys_file", "decrypted_dir", "decoded_image_dir",
        "wxwork_keys_file", "wxwork_decrypted_dir", "wxwork_export_dir",
    ):
        if key in cfg and cfg[key] and not os.path.isabs(cfg[key]):
            cfg[key] = os.path.join(base, cfg[key])
    # 路径展开:先 expanduser(~ 展开)+ expandvars($HOME / %USERPROFILE% 展开),
    # 再判 isabs;还相对就 join 项目根。这样 config 里既能写
    # "all_keys.json"(项目根相对),也能写 "~/Documents/wechat_decrypted" /
    # "$HOME/wechat" / "%USERPROFILE%\\wechat"(跨用户便携)。
    # 空字串 / null 不再触发 TypeError(用 cfg.get 而非 in)。
    base = _app_base_dir()
    if cfg.get("db_dir"):
        cfg["db_dir"] = os.path.expanduser(os.path.expandvars(cfg["db_dir"]))
    for key in ("keys_file", "decrypted_dir", "decoded_image_dir"):
        if cfg.get(key):
            cfg[key] = os.path.expanduser(os.path.expandvars(cfg[key]))
            if not os.path.isabs(cfg[key]):
                cfg[key] = os.path.join(base, cfg[key])

    # 自动推导微信数据根目录（db_dir 的上级目录）
    # db_dir 格式: D:\xwechat_files\<wxid>\db_storage
    # base_dir 格式: D:\xwechat_files\<wxid>
    db_dir = cfg.get("db_dir", "")
    if db_dir and os.path.basename(db_dir) == "db_storage":
        cfg["wechat_base_dir"] = os.path.dirname(db_dir)
    else:
        cfg["wechat_base_dir"] = db_dir

    # 输出目录：<app_dir>/wechat_files/<wxid>/
    wxid = os.path.basename(os.path.normpath(cfg["wechat_base_dir"]))
    cfg["output_base_dir"] = os.path.join(base, "wechat_files", wxid)

    # decoded_image_dir 默认值
    if "decoded_image_dir" not in cfg:
        cfg["decoded_image_dir"] = os.path.join(base, "decoded_images")

    # 自动检测 WeChat Files 目录（FileStorage/MsgAttach, FileStorage/Sns/Cache）
    if not cfg.get("wechat_files_dir"):
        wechat_files_base = os.path.join(os.path.expanduser("~"), "Documents", "WeChat Files")
        if os.path.isdir(wechat_files_base):
            # xwechat_files 的 wxid 可能带后缀如 _1d4c，需要模糊匹配
            wxid_prefix = wxid.rsplit("_", 1)[0] if "_" in wxid else wxid
            for d in os.listdir(wechat_files_base):
                if d == wxid or d == wxid_prefix or wxid.startswith(d):
                    candidate = os.path.join(wechat_files_base, d)
                    if os.path.isdir(os.path.join(candidate, "FileStorage")):
                        cfg["wechat_files_dir"] = candidate
                        break

    wf_dir = cfg.get("wechat_files_dir", "")
    cfg["msgattach_dir"] = os.path.join(wf_dir, "FileStorage", "MsgAttach") if wf_dir else ""
    cfg["sns_cache_dir"] = os.path.join(wf_dir, "FileStorage", "Sns", "Cache") if wf_dir else ""

    # xwechat_files 图片/缓存路径
    wb = cfg["wechat_base_dir"]
    cfg["xwechat_attach_dir"] = os.path.join(wb, "msg", "attach") if wb else ""
    cfg["xwechat_cache_dir"] = os.path.join(wb, "cache") if wb else ""

    return cfg
