"""WeChat Decrypt GUI — 一键解密 / 导出消息 / 转换音频"""
import os
import sys
import subprocess
import threading
import sqlite3
import hashlib
import glob as globmod
import tkinter as tk
from tkinter import ttk, scrolledtext

# 确保工作目录为脚本所在目录（打包后也适用）
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)
os.environ["WECHAT_DECRYPT_APP_DIR"] = BASE_DIR


# ── 子任务入口（当以 --task 参数调用时直接执行对应脚本） ──────────────────────

# 显式导入：让 PyInstaller 收集子脚本需要的所有依赖
# （这些脚本通过 exec 动态加载，PyInstaller 无法自动检测）
import importlib.util  # noqa: F401 - used for dynamic loading
if False:  # noqa: never executed, only for PyInstaller dependency detection
    import sqlite3, hashlib, csv, json, re, glob, tempfile  # noqa: F401
    import xml.etree.ElementTree  # noqa: F401
    import functools, platform, ctypes, ctypes.wintypes  # noqa: F401
    import zstandard  # noqa: F401
    import pilk  # noqa: F401
    import Crypto, Crypto.Cipher, Crypto.Cipher.AES, Crypto.Util.Padding  # noqa: F401
    import wxwork_crypto  # noqa: F401
    import export_wxwork_messages  # noqa: F401


def _run_subtask(task: str):
    """在子进程中被调用，直接执行对应脚本逻辑"""
    # 强制 stdout/stderr 为 UTF-8
    if sys.platform == "win32":
        for s in (sys.stdout, sys.stderr):
            if hasattr(s, "reconfigure"):
                s.reconfigure(encoding="utf-8", errors="replace")

    # onefile: _MEIPASS 临时目录; onedir: _internal/; 开发: BASE_DIR
    if getattr(sys, "frozen", False):
        script_dir = getattr(sys, "_MEIPASS", os.path.join(os.path.dirname(sys.executable), "_internal"))
    else:
        script_dir = BASE_DIR

    # 让 import 能找到脚本同目录的模块
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    if BASE_DIR not in sys.path:
        sys.path.insert(0, BASE_DIR)

    mapping = {
        "decrypt": "main.py",
        "export": "export_messages.py",
        "voice": "voice_to_mp3.py",
        "find_image_key": "find_image_key.py",
        "decrypt_sns": "decrypt_sns.py",
        "export_sns": "export_sns.py",
        "find_wxwork_keys": "find_wxwork_keys.py",
        "decrypt_wxwork": "decrypt_wxwork_db.py",
        "export_wxwork": "export_wxwork_messages.py",
    }
    script = mapping.get(task)
    if not script:
        print(f"未知任务: {task}", flush=True)
        sys.exit(1)

    script_path = os.path.join(script_dir, script)
    if not os.path.exists(script_path):
        # 开发模式回退到 BASE_DIR
        script_path = os.path.join(BASE_DIR, script)
    if not os.path.exists(script_path):
        print(f"脚本不存在: {script_path}", flush=True)
        sys.exit(1)

    # 将 decrypt 命令传给 main.py
    if task == "decrypt":
        sys.argv = ["main.py", "decrypt"]
    elif task == "find_image_key":
        sys.argv = ["find_image_key.py"]
    elif task == "find_wxwork_keys":
        sys.argv = ["find_wxwork_keys.py"]
    elif task == "decrypt_wxwork":
        sys.argv = ["decrypt_wxwork_db.py"]
    elif task == "export_wxwork":
        sys.argv = ["export_wxwork_messages.py"]
    else:
        sys.argv = [script]

    # 设置环境变量，让 config.py 等脚本知道真正的应用目录
    os.environ["WECHAT_DECRYPT_APP_DIR"] = BASE_DIR
    os.chdir(BASE_DIR)

    # 加载并执行脚本
    spec = importlib.util.spec_from_file_location("__main__", script_path)
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = "__main__"
    spec.loader.exec_module(mod)


# ── 检查是否为子任务模式 ──────────────────────────────────────────────────────
if len(sys.argv) >= 3 and sys.argv[1] == "--task":
    _run_subtask(sys.argv[2])
    sys.exit(0)

# ── GUI 模式：隐藏控制台窗口 ────────────────────────────────────────────────
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)
    except Exception:
        pass


# ── 联系人发现 ────────────────────────────────────────────────────────────────

def _load_contact_map(decrypted_dir):
    """从 contact.db 加载联系人映射 {username: {remark, nick_name, ...}}"""
    contact_map = {}
    db_path = os.path.join(decrypted_dir, "contact", "contact.db")
    if not os.path.exists(db_path):
        return contact_map
    try:
        conn = sqlite3.connect(db_path)
        for uname, alias, remark, nick_name in conn.execute(
            "SELECT username, alias, remark, nick_name FROM contact"
        ):
            contact_map[uname] = {
                "remark": remark or "",
                "nick_name": nick_name or "",
            }
        conn.close()
    except Exception:
        pass
    return contact_map


def _display_name(username, contact_map):
    info = contact_map.get(username, {})
    return info.get("remark") or info.get("nick_name") or username


def _discover_contacts():
    """扫描所有联系人/会话，返回 (contacts, has_voice)
    contacts: [(username, display_name), ...]
    has_voice: 是否存在语音数据
    """
    from config import load_config
    cfg = load_config()
    decrypted_dir = cfg["decrypted_dir"]

    if not os.path.isdir(decrypted_dir):
        raise FileNotFoundError(f"解密目录不存在: {decrypted_dir}\n请先运行「解密数据库」")

    contact_map = _load_contact_map(decrypted_dir)
    usernames = set()
    has_voice = False

    # 从消息数据库扫描
    msg_dir = os.path.join(decrypted_dir, "message")
    if os.path.isdir(msg_dir):
        db_files = [
            f for f in globmod.glob(os.path.join(msg_dir, "message_*.db"))
            if not f.endswith(("_fts.db", "_resource.db"))
        ]
        print(f"找到 {len(db_files)} 个消息数据库", flush=True)
        for db_path in db_files:
            try:
                conn = sqlite3.connect(db_path)
                hash_to_uname = {}
                for row in conn.execute("SELECT rowid, user_name FROM Name2Id"):
                    uname = row[1]
                    if uname:
                        h = hashlib.md5(uname.encode()).hexdigest()
                        hash_to_uname[h] = uname
                for (tbl,) in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
                ):
                    h = tbl[4:]
                    uname = hash_to_uname.get(h)
                    if uname:
                        usernames.add(uname)
                conn.close()
            except Exception as e:
                print(f"  读取 {os.path.basename(db_path)} 失败: {e}", flush=True)
                continue
    else:
        print(f"消息目录不存在: {msg_dir}", flush=True)

    # 从语音数据库扫描
    voice_db = os.path.join(msg_dir, "media_0.db")
    if os.path.exists(voice_db):
        try:
            conn = sqlite3.connect(voice_db)
            name_map = {}
            for rowid, uname in conn.execute("SELECT rowid, user_name FROM Name2Id"):
                name_map[rowid] = uname
            for (cid,) in conn.execute("SELECT DISTINCT chat_name_id FROM VoiceInfo"):
                uname = name_map.get(cid)
                if uname:
                    usernames.add(uname)
                    has_voice = True
            conn.close()
        except Exception as e:
            print(f"  读取语音数据库失败: {e}", flush=True)

    print(f"共发现 {len(usernames)} 个会话", flush=True)
    result = [(u, _display_name(u, contact_map)) for u in usernames]
    result.sort(key=lambda x: x[1].lower())
    return result, has_voice


# ── 导出选项对话框 ──────────────────────────────────────────────────────────

class ExportOptionsDialog(tk.Toplevel):
    def __init__(self, parent, contacts, has_voice=False):
        """contacts: [(username, display_name), ...]
        has_voice: 是否检测到语音数据
        """
        super().__init__(parent)
        self.title("导出选项")
        self.geometry("460x600")
        self.transient(parent)
        self.grab_set()
        self.result = None
        self.configure(bg="#f0f0f0")
        self._contacts = contacts
        self._vars = {}  # username -> BooleanVar

        # ── 导出格式 ──
        fmt_frame = ttk.LabelFrame(self, text="导出格式", padding=6)
        fmt_frame.pack(fill="x", padx=12, pady=(10, 4))

        self._fmt_csv = tk.BooleanVar(value=True)
        self._fmt_html = tk.BooleanVar(value=False)
        self._fmt_json = tk.BooleanVar(value=False)

        ttk.Checkbutton(fmt_frame, text="CSV（默认）", variable=self._fmt_csv).pack(side="left", padx=10)
        ttk.Checkbutton(fmt_frame, text="HTML", variable=self._fmt_html).pack(side="left", padx=10)
        ttk.Checkbutton(fmt_frame, text="JSON", variable=self._fmt_json).pack(side="left", padx=10)

        # ── 其他选项 ──
        opt_frame = ttk.LabelFrame(self, text="其他选项", padding=6)
        opt_frame.pack(fill="x", padx=12, pady=(0, 4))

        self._image_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt_frame, text="导出并解密图片",
                        variable=self._image_var).pack(anchor="w", padx=8)

        self._sns_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt_frame, text="导出朋友圈动态（文案/评论）",
                        variable=self._sns_var).pack(anchor="w", padx=8)

        self._sns_media_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt_frame, text="  ↳ 尝试下载朋友圈媒体（可能较慢）",
                        variable=self._sns_media_var).pack(anchor="w", padx=24)

        self._voice_var = tk.BooleanVar(value=False)
        if has_voice:
            ttk.Checkbutton(opt_frame, text="同时转换语音为 MP3",
                            variable=self._voice_var).pack(anchor="w", padx=8)

        # ── 联系人选择 ──
        top = ttk.Frame(self)
        top.pack(fill="x", padx=12, pady=(4, 4))

        ttk.Label(top, text=f"共 {len(contacts)} 个会话",
                  font=("Microsoft YaHei UI", 10)).pack(side="left")

        self._all_selected = True
        self._toggle_btn = ttk.Button(top, text="取消全选", command=self._toggle_all)
        self._toggle_btn.pack(side="right")

        # 搜索框
        search_frame = ttk.Frame(self)
        search_frame.pack(fill="x", padx=12, pady=(0, 4))
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._filter_list())
        ttk.Entry(search_frame, textvariable=self._search_var,
                  font=("Microsoft YaHei UI", 10)).pack(fill="x")

        # 滚动区域
        container = ttk.Frame(self)
        container.pack(fill="both", expand=True, padx=12, pady=4)

        self._canvas = tk.Canvas(container, bg="#ffffff", highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=self._canvas.yview)
        self._inner = ttk.Frame(self._canvas)

        self._inner.bind("<Configure>",
                         lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self._canvas.create_window((0, 0), window=self._inner, anchor="nw")
        self._canvas.configure(yscrollcommand=scrollbar.set)

        self._canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # 绑定鼠标滚轮
        self._canvas.bind("<Enter>", lambda e: self._bind_mousewheel())
        self._canvas.bind("<Leave>", lambda e: self._unbind_mousewheel())

        # 创建 Checkbutton 列表
        self._cb_widgets = []
        for username, dname in contacts:
            var = tk.BooleanVar(value=True)
            self._vars[username] = var
            label = f"{dname}  ({username})" if dname != username else username
            cb = ttk.Checkbutton(self._inner, text=label, variable=var)
            cb.pack(anchor="w", padx=6, pady=1)
            self._cb_widgets.append((username, dname, cb))

        # 底部按钮
        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=12, pady=(4, 10))

        ttk.Button(bottom, text="确定", command=self._on_ok).pack(side="right", padx=4)
        ttk.Button(bottom, text="取消", command=self._on_cancel).pack(side="right", padx=4)

    def _bind_mousewheel(self):
        self._canvas.bind_all("<MouseWheel>",
                              lambda e: self._canvas.yview_scroll(-1 * (e.delta // 120), "units"))

    def _unbind_mousewheel(self):
        self._canvas.unbind_all("<MouseWheel>")

    def _toggle_all(self):
        self._all_selected = not self._all_selected
        for var in self._vars.values():
            var.set(self._all_selected)
        self._toggle_btn.configure(text="取消全选" if self._all_selected else "全选")

    def _filter_list(self):
        keyword = self._search_var.get().strip().lower()
        for username, dname, cb in self._cb_widgets:
            if not keyword or keyword in dname.lower() or keyword in username.lower():
                cb.pack(anchor="w", padx=6, pady=1)
            else:
                cb.pack_forget()

    def _on_ok(self):
        formats = []
        if self._fmt_csv.get():
            formats.append("csv")
        if self._fmt_html.get():
            formats.append("html")
        if self._fmt_json.get():
            formats.append("json")

        if not formats and not self._voice_var.get() and not self._sns_var.get():
            from tkinter import messagebox
            messagebox.showwarning("提示", "请至少选择一种导出格式、朋友圈导出或语音转换", parent=self)
            return

        self.result = {
            "contacts": [u for u, var in self._vars.items() if var.get()],
            "formats": formats,
            "include_voice": self._voice_var.get(),
            "include_images": self._image_var.get(),
            "include_sns": self._sns_var.get(),
            "include_sns_media": self._sns_media_var.get(),
        }
        self.destroy()

    def _on_cancel(self):
        self.result = None
        self.destroy()


class WxworkExportOptionsDialog(tk.Toplevel):
    def __init__(self, parent, conversations):
        """conversations: [{conversation_id, display_name, kind, message_count, last_time}, ...]"""
        super().__init__(parent)
        self.title("企业微信导出选项")
        self.geometry("560x620")
        self.transient(parent)
        self.grab_set()
        self.result = None
        self.configure(bg="#f0f0f0")
        self._conversations = conversations
        self._vars = {}

        fmt_frame = ttk.LabelFrame(self, text="导出格式", padding=6)
        fmt_frame.pack(fill="x", padx=12, pady=(10, 4))

        self._fmt_csv = tk.BooleanVar(value=True)
        self._fmt_html = tk.BooleanVar(value=False)
        self._fmt_json = tk.BooleanVar(value=False)

        ttk.Checkbutton(fmt_frame, text="CSV（默认）", variable=self._fmt_csv).pack(side="left", padx=10)
        ttk.Checkbutton(fmt_frame, text="HTML", variable=self._fmt_html).pack(side="left", padx=10)
        ttk.Checkbutton(fmt_frame, text="JSON", variable=self._fmt_json).pack(side="left", padx=10)

        top = ttk.Frame(self)
        top.pack(fill="x", padx=12, pady=(4, 4))
        ttk.Label(top, text=f"共 {len(conversations)} 个企业微信会话",
                  font=("Microsoft YaHei UI", 10)).pack(side="left")

        self._all_selected = True
        self._toggle_btn = ttk.Button(top, text="取消全选", command=self._toggle_all)
        self._toggle_btn.pack(side="right")

        search_frame = ttk.Frame(self)
        search_frame.pack(fill="x", padx=12, pady=(0, 4))
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._filter_list())
        ttk.Entry(search_frame, textvariable=self._search_var,
                  font=("Microsoft YaHei UI", 10)).pack(fill="x")

        container = ttk.Frame(self)
        container.pack(fill="both", expand=True, padx=12, pady=4)

        self._canvas = tk.Canvas(container, bg="#ffffff", highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=self._canvas.yview)
        self._inner = ttk.Frame(self._canvas)
        self._inner.bind("<Configure>",
                         lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self._canvas.create_window((0, 0), window=self._inner, anchor="nw")
        self._canvas.configure(yscrollcommand=scrollbar.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self._canvas.bind("<Enter>", lambda e: self._bind_mousewheel())
        self._canvas.bind("<Leave>", lambda e: self._unbind_mousewheel())

        self._cb_widgets = []
        for conv in conversations:
            cid = conv["conversation_id"]
            var = tk.BooleanVar(value=True)
            self._vars[cid] = var
            last_time = self._format_time(conv.get("last_time"))
            suffix = f" · {last_time}" if last_time else ""
            label = (
                f"[{conv.get('kind', '会话')}] {conv.get('display_name') or cid}"
                f" · {conv.get('message_count', 0)} 条{suffix}"
            )
            cb = ttk.Checkbutton(self._inner, text=label, variable=var)
            cb.pack(anchor="w", padx=6, pady=1)
            self._cb_widgets.append((cid, label.lower(), cb))

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=12, pady=(4, 10))
        ttk.Button(bottom, text="确定", command=self._on_ok).pack(side="right", padx=4)
        ttk.Button(bottom, text="取消", command=self._on_cancel).pack(side="right", padx=4)

    def _format_time(self, value):
        if not value:
            return ""
        try:
            from datetime import datetime
            return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d")
        except Exception:
            return ""

    def _bind_mousewheel(self):
        self._canvas.bind_all("<MouseWheel>",
                              lambda e: self._canvas.yview_scroll(-1 * (e.delta // 120), "units"))

    def _unbind_mousewheel(self):
        self._canvas.unbind_all("<MouseWheel>")

    def _toggle_all(self):
        self._all_selected = not self._all_selected
        for var in self._vars.values():
            var.set(self._all_selected)
        self._toggle_btn.configure(text="取消全选" if self._all_selected else "全选")

    def _filter_list(self):
        keyword = self._search_var.get().strip().lower()
        for _cid, label, cb in self._cb_widgets:
            if not keyword or keyword in label:
                cb.pack(anchor="w", padx=6, pady=1)
            else:
                cb.pack_forget()

    def _on_ok(self):
        formats = []
        if self._fmt_csv.get():
            formats.append("csv")
        if self._fmt_html.get():
            formats.append("html")
        if self._fmt_json.get():
            formats.append("json")
        if not formats:
            from tkinter import messagebox
            messagebox.showwarning("提示", "请至少选择一种导出格式", parent=self)
            return
        self.result = {
            "conversations": [cid for cid, var in self._vars.items() if var.get()],
            "formats": formats,
        }
        self.destroy()

    def _on_cancel(self):
        self.result = None
        self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("WeChat Decrypt 工具箱")
        self.geometry("820x600")
        self.resizable(True, True)
        self.configure(bg="#f0f0f0")
        self._running = False
        self._auto_export = False
        self._selected_contacts = None
        self._export_formats = None
        self._include_voice = False
        self._include_images = True
        self._include_sns = False
        self._include_sns_media = False
        self._selected_wxwork_conversations = None
        self._wxwork_export_formats = None

        self._build_ui()

    # ── UI 构建 ────────────────────────────────────────────────────────────
    def _build_ui(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Big.TButton", font=("Microsoft YaHei UI", 11), padding=(16, 10))
        style.configure("TLabel", font=("Microsoft YaHei UI", 10), background="#f0f0f0")

        # 标题
        title = ttk.Label(self, text="WeChat Decrypt 工具箱", font=("Microsoft YaHei UI", 16, "bold"))
        title.pack(pady=(14, 6))

        # 按钮区域
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=20, pady=(4, 4))

        self.btn_decrypt = ttk.Button(
            btn_frame, text="① 微信解密", style="Big.TButton",
            command=lambda: self._run_task("decrypt")
        )
        self.btn_decrypt.pack(side="left", expand=True, fill="x", padx=4)

        self.btn_imgkey = ttk.Button(
            btn_frame, text="② 图片密钥", style="Big.TButton",
            command=lambda: self._run_task("find_image_key")
        )
        self.btn_imgkey.pack(side="left", expand=True, fill="x", padx=4)

        self.btn_export = ttk.Button(
            btn_frame, text="③ 导出数据", style="Big.TButton",
            command=lambda: self._run_task("export")
        )
        self.btn_export.pack(side="left", expand=True, fill="x", padx=4)

        self.btn_sns = ttk.Button(
            btn_frame, text="④ 朋友圈图片", style="Big.TButton",
            command=lambda: self._run_task("decrypt_sns")
        )
        self.btn_sns.pack(side="left", expand=True, fill="x", padx=4)

        wxwork_frame = ttk.Frame(self)
        wxwork_frame.pack(fill="x", padx=20, pady=(0, 6))

        self.btn_wxwork = ttk.Button(
            wxwork_frame, text="⑤ 企业微信解密", style="Big.TButton",
            command=lambda: self._run_task("wxwork_decrypt")
        )
        self.btn_wxwork.pack(side="left", expand=True, fill="x", padx=4)

        self.btn_wxwork_export = ttk.Button(
            wxwork_frame, text="⑥ 企业微信导出", style="Big.TButton",
            command=lambda: self._run_task("wxwork_export")
        )
        self.btn_wxwork_export.pack(side="left", expand=True, fill="x", padx=4)

        # 提示信息
        tips_frame = ttk.LabelFrame(self, text="使用提示", padding=6)
        tips_frame.pack(fill="x", padx=20, pady=(0, 4))
        tips_text = (
            "• 微信解密：需要微信正在运行中，会自动提取密钥并解密\n"
            "• 查找图片密钥：先在微信中打开 2-3 张图片查看，然后立即运行\n"
            "• 导出数据：选择联系人和格式，可同时导出消息/图片/语音\n"
            "• 朋友圈图片：解密朋友圈缓存图片（_t缩略图自动跳过）\n"
            "• 企业微信解密：需要企业微信正在运行中，输出到 wxwork_decrypted/\n"
            "• 企业微信导出：选择某个人或群，输出 CSV / HTML / JSON 到 wxwork_export/"
        )
        ttk.Label(tips_frame, text=tips_text, font=("Microsoft YaHei UI", 9),
                  wraplength=760, justify="left").pack(anchor="w")

        # 进度条
        self.progress = ttk.Progressbar(self, mode="indeterminate")
        self.progress.pack(fill="x", padx=20, pady=(0, 4))

        # 日志区域
        log_label = ttk.Label(self, text="运行日志：")
        log_label.pack(anchor="w", padx=20)

        self.log = scrolledtext.ScrolledText(
            self, wrap="word", height=18,
            font=("Consolas", 10), bg="#1e1e1e", fg="#d4d4d4",
            insertbackground="#fff", state="disabled"
        )
        self.log.pack(fill="both", expand=True, padx=20, pady=(2, 10))

        # 底部状态
        self.status_var = tk.StringVar(value="就绪")
        status = ttk.Label(self, textvariable=self.status_var, font=("Microsoft YaHei UI", 9))
        status.pack(anchor="w", padx=20, pady=(0, 8))

    # ── 日志写入 ───────────────────────────────────────────────────────────
    def _log(self, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    # ── 按钮状态 ───────────────────────────────────────────────────────────
    def _set_buttons(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        self.btn_decrypt.configure(state=state)
        self.btn_imgkey.configure(state=state)
        self.btn_export.configure(state=state)
        self.btn_sns.configure(state=state)
        self.btn_wxwork.configure(state=state)
        self.btn_wxwork_export.configure(state=state)

    # ── 任务调度 ───────────────────────────────────────────────────────────
    def _run_task(self, task: str):
        if self._running:
            return
        self._running = True
        self._selected_contacts = None
        self._export_formats = None
        self._include_voice = False
        self._include_sns = False
        self._include_sns_media = False
        self._selected_wxwork_conversations = None
        self._wxwork_export_formats = None
        self._clear_log()
        self._set_buttons(False)

        if task == "export":
            self.progress.start(15)
            self.status_var.set("正在扫描联系人...")
            threading.Thread(
                target=self._discover_and_select, daemon=True
            ).start()
        elif task == "find_image_key":
            self.progress.start(15)
            self.status_var.set("正在扫描微信进程内存...")
            threading.Thread(target=self._exec_task, args=(task,), daemon=True).start()
        elif task == "decrypt_sns":
            self.progress.start(15)
            self.status_var.set("正在解密朋友圈图片...")
            threading.Thread(target=self._exec_task, args=(task,), daemon=True).start()
        elif task == "wxwork_decrypt":
            self.progress.start(15)
            self.status_var.set("正在解密企业微信数据库...")
            threading.Thread(target=self._exec_wxwork_decrypt, daemon=True).start()
        elif task == "wxwork_export":
            self.progress.start(15)
            self.status_var.set("正在扫描企业微信会话...")
            threading.Thread(target=self._discover_wxwork_and_select, daemon=True).start()
        else:
            self.progress.start(15)
            labels = {"decrypt": "解密数据库"}
            self.status_var.set(f"正在{labels.get(task, task)}...")
            threading.Thread(target=self._exec_task, args=(task,), daemon=True).start()

    def _discover_and_select(self):
        """后台扫描联系人，然后在主线程弹出选择对话框"""
        try:
            contacts, has_voice = _discover_contacts()
        except Exception as e:
            self.after(0, self._log, f"扫描联系人失败: {e}\n")
            self.after(0, self._on_task_done)
            return

        if not contacts:
            self.after(0, self._log, "未找到任何联系人/会话\n")
            self.after(0, self._on_task_done)
            return

        self.after(0, self._show_contact_dialog, contacts, has_voice)

    def _show_contact_dialog(self, contacts, has_voice):
        self.progress.stop()
        self.status_var.set(f"请选择导出选项 ({len(contacts)} 个会话)")

        dlg = ExportOptionsDialog(self, contacts, has_voice=has_voice)
        self.wait_window(dlg)

        if dlg.result is None:
            self._on_task_done()
            return

        if not dlg.result["contacts"]:
            self._log("未选择任何联系人\n")
            self._on_task_done()
            return

        self._selected_contacts = dlg.result["contacts"]
        self._export_formats = dlg.result["formats"]
        self._include_voice = dlg.result["include_voice"]
        self._include_images = dlg.result["include_images"]
        self._include_sns = dlg.result["include_sns"]
        self._include_sns_media = dlg.result["include_sns_media"]

        self._clear_log()
        self.progress.start(15)
        n_sel = len(dlg.result["contacts"])
        parts = []
        if self._export_formats:
            parts.append(f"导出 {'/'.join(f.upper() for f in self._export_formats)}")
        if self._include_sns:
            parts.append("朋友圈")
        if self._include_voice:
            parts.append("转换语音")
        action = " + ".join(parts) or "处理"
        self.status_var.set(f"正在{action}...（{n_sel}/{len(contacts)} 个联系人）")
        threading.Thread(target=self._exec_combined, daemon=True).start()

    def _discover_wxwork_and_select(self):
        """后台扫描企业微信会话，然后在主线程弹出选择对话框"""
        try:
            from export_wxwork_messages import discover_conversations
            conversations = discover_conversations()
        except Exception as e:
            self.after(0, self._log, f"扫描企业微信会话失败: {e}\n")
            self.after(0, self._on_task_done)
            return

        if not conversations:
            self.after(0, self._log, "未找到任何企业微信会话，请先运行「企业微信解密」\n")
            self.after(0, self._on_task_done)
            return

        self.after(0, self._show_wxwork_dialog, conversations)

    def _show_wxwork_dialog(self, conversations):
        self.progress.stop()
        self.status_var.set(f"请选择企业微信导出选项 ({len(conversations)} 个会话)")

        dlg = WxworkExportOptionsDialog(self, conversations)
        self.wait_window(dlg)

        if dlg.result is None:
            self._on_task_done()
            return

        if not dlg.result["conversations"]:
            self._log("未选择任何企业微信会话\n")
            self._on_task_done()
            return

        self._selected_wxwork_conversations = dlg.result["conversations"]
        self._wxwork_export_formats = dlg.result["formats"]

        self._clear_log()
        self.progress.start(15)
        n_sel = len(dlg.result["conversations"])
        self.status_var.set(
            f"正在导出企业微信 {'/'.join(f.upper() for f in self._wxwork_export_formats)}..."
            f"（{n_sel}/{len(conversations)} 个会话）"
        )
        threading.Thread(target=self._exec_wxwork_export, daemon=True).start()

    # ── 子进程执行 ─────────────────────────────────────────────────────────
    def _run_subprocess(self, task: str) -> int:
        """运行子进程，返回退出码"""
        cmd = [sys.executable, "--task", task]
        self.after(0, self._log, f">>> {' '.join(cmd)}\n\n")

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["WECHAT_DECRYPT_APP_DIR"] = BASE_DIR

        if self._selected_contacts:
            env["WECHAT_EXPORT_CONTACTS"] = ",".join(self._selected_contacts)
        if self._export_formats:
            env["WECHAT_EXPORT_FORMATS"] = ",".join(self._export_formats)
        env["WECHAT_EXPORT_IMAGES"] = "1" if getattr(self, '_include_images', True) else "0"
        if getattr(self, '_include_sns_media', False):
            env["WECHAT_SNS_DOWNLOAD_MEDIA"] = "1"
        if self._selected_wxwork_conversations:
            env["WXWORK_EXPORT_CONVERSATIONS"] = ",".join(self._selected_wxwork_conversations)
        if self._wxwork_export_formats:
            env["WXWORK_EXPORT_FORMATS"] = ",".join(self._wxwork_export_formats)

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=BASE_DIR,
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )

        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace")
            self.after(0, self._log, line)

        proc.wait()
        return proc.returncode

    def _exec_combined(self):
        """执行导出（消息 + 可选语音）"""
        try:
            if self._export_formats:
                rc = self._run_subprocess("export")
                if rc != 0:
                    self.after(0, self._log, f"\n❌ 导出失败 (返回码 {rc})\n")
                    self.after(0, self.status_var.set, f"失败 (返回码 {rc})")
                    return

            if getattr(self, '_include_sns', False):
                if self._export_formats:
                    self.after(0, self._log, "\n\n━━━ 开始导出朋友圈 ━━━\n\n")
                rc = self._run_subprocess("export_sns")
                if rc != 0:
                    self.after(0, self._log, f"\n❌ 朋友圈导出失败 (返回码 {rc})\n")
                    self.after(0, self.status_var.set, f"朋友圈导出失败 (返回码 {rc})")
                    return

            if self._include_voice:
                if self._export_formats or getattr(self, '_include_sns', False):
                    self.after(0, self._log, "\n\n━━━ 开始转换语音 ━━━\n\n")
                rc = self._run_subprocess("voice")
                if rc != 0:
                    self.after(0, self._log, f"\n❌ 语音转换失败 (返回码 {rc})\n")
                    self.after(0, self.status_var.set, f"语音转换失败 (返回码 {rc})")
                    return

            self.after(0, self._log, "\n✅ 全部完成！\n")
            self.after(0, self.status_var.set, "完成")
        except Exception as e:
            self.after(0, self._log, f"\n❌ 异常: {e}\n")
            self.after(0, self.status_var.set, "异常")
        finally:
            self._selected_contacts = None
            self._export_formats = None
            self._include_voice = False
            self._include_images = True
            self._include_sns = False
            self._include_sns_media = False
            self.after(0, self._on_task_done)

    def _exec_wxwork_decrypt(self):
        """执行企业微信 key 提取 + 数据库解密。"""
        try:
            self.after(0, self._log, "━━━ 开始提取企业微信密钥 ━━━\n\n")
            rc = self._run_subprocess("find_wxwork_keys")
            if rc != 0:
                self.after(0, self._log, f"\n❌ 企业微信密钥提取失败 (返回码 {rc})\n")
                self.after(0, self.status_var.set, f"企业微信密钥提取失败 (返回码 {rc})")
                return

            self.after(0, self._log, "\n\n━━━ 开始解密企业微信数据库 ━━━\n\n")
            rc = self._run_subprocess("decrypt_wxwork")
            if rc != 0:
                self.after(0, self._log, f"\n❌ 企业微信数据库解密失败 (返回码 {rc})\n")
                self.after(0, self.status_var.set, f"企业微信解密失败 (返回码 {rc})")
                return

            self.after(0, self._log, "\n✅ 企业微信解密完成！输出目录: wxwork_decrypted\n")
            self.after(0, self.status_var.set, "企业微信解密完成")
        except Exception as e:
            self.after(0, self._log, f"\n❌ 异常: {e}\n")
            self.after(0, self.status_var.set, "异常")
        finally:
            self.after(0, self._on_task_done)

    def _exec_wxwork_export(self):
        """执行企业微信消息导出。"""
        try:
            rc = self._run_subprocess("export_wxwork")
            if rc != 0:
                self.after(0, self._log, f"\n❌ 企业微信导出失败 (返回码 {rc})\n")
                self.after(0, self.status_var.set, f"企业微信导出失败 (返回码 {rc})")
                return
            self.after(0, self._log, "\n✅ 企业微信导出完成！输出目录: wxwork_export\n")
            self.after(0, self.status_var.set, "企业微信导出完成")
        except Exception as e:
            self.after(0, self._log, f"\n❌ 异常: {e}\n")
            self.after(0, self.status_var.set, "异常")
        finally:
            self._selected_wxwork_conversations = None
            self._wxwork_export_formats = None
            self.after(0, self._on_task_done)

    def _exec_task(self, task: str):
        """执行单一任务（解密）"""
        try:
            rc = self._run_subprocess(task)
            if rc == 0:
                self.after(0, self._log, "\n✅ 完成！\n")
                self.after(0, self.status_var.set, "完成")
                if task == "decrypt":
                    self._auto_export = True
            else:
                self.after(0, self._log, f"\n❌ 进程退出，返回码: {rc}\n")
                self.after(0, self.status_var.set, f"失败 (返回码 {rc})")
        except Exception as e:
            self.after(0, self._log, f"\n❌ 异常: {e}\n")
            self.after(0, self.status_var.set, "异常")
        finally:
            self.after(0, self._on_task_done)

    def _on_task_done(self):
        self._running = False
        self.progress.stop()
        self._set_buttons(True)
        if self._auto_export:
            self._auto_export = False
            self._log("\n解密完成，自动进入导出流程...\n\n")
            self.after(500, lambda: self._run_task("export"))


if __name__ == "__main__":
    app = App()
    app.mainloop()
