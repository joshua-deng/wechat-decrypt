r"""
WeChat MCP Server - query WeChat messages, contacts via Claude

Based on FastMCP (stdio transport), reuses existing decryption.
Runs on Windows Python (needs access to D:\ WeChat databases).
"""

import io
import os, sys, json, time, sqlite3, tempfile, struct, hashlib, atexit, re, threading, subprocess
import glob
import wave
import hmac as hmac_mod
from contextlib import closing
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET
from Crypto.Cipher import AES
from mcp.server.fastmcp import FastMCP
import zstandard as zstd
from decode_image import ImageResolver
from key_utils import get_key_info, key_path_variants, strip_key_metadata

# ============ 加密常量 ============
PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16
RESERVE_SZ = 80
SQLITE_HDR = b'SQLite format 3\x00'
WAL_HEADER_SZ = 32
WAL_FRAME_HEADER_SZ = 24

# ============ 配置加载 ============
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")

with open(CONFIG_FILE, encoding="utf-8") as f:
    _cfg = json.load(f)
for _key in ("keys_file", "decrypted_dir"):
    if _key in _cfg and not os.path.isabs(_cfg[_key]):
        _cfg[_key] = os.path.join(SCRIPT_DIR, _cfg[_key])

DB_DIR = _cfg["db_dir"]
KEYS_FILE = _cfg["keys_file"]
DECRYPTED_DIR = _cfg["decrypted_dir"]

# 图片相关路径
_db_dir = _cfg["db_dir"]
if os.path.basename(_db_dir) == "db_storage":
    WECHAT_BASE_DIR = os.path.dirname(_db_dir)
else:
    WECHAT_BASE_DIR = _db_dir

DECODED_IMAGE_DIR = _cfg.get("decoded_image_dir")
if not DECODED_IMAGE_DIR:
    DECODED_IMAGE_DIR = os.path.join(SCRIPT_DIR, "decoded_images")
elif not os.path.isabs(DECODED_IMAGE_DIR):
    DECODED_IMAGE_DIR = os.path.join(SCRIPT_DIR, DECODED_IMAGE_DIR)

with open(KEYS_FILE, encoding="utf-8") as f:
    ALL_KEYS = strip_key_metadata(json.load(f))

# ============ 解密函数 ============

def decrypt_page(enc_key, page_data, pgno):
    iv = page_data[PAGE_SZ - RESERVE_SZ : PAGE_SZ - RESERVE_SZ + 16]
    if pgno == 1:
        encrypted = page_data[SALT_SZ : PAGE_SZ - RESERVE_SZ]
        cipher = AES.new(enc_key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted)
        return bytes(bytearray(SQLITE_HDR + decrypted + b'\x00' * RESERVE_SZ))
    else:
        encrypted = page_data[: PAGE_SZ - RESERVE_SZ]
        cipher = AES.new(enc_key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted)
        return decrypted + b'\x00' * RESERVE_SZ


def full_decrypt(db_path, out_path, enc_key):
    file_size = os.path.getsize(db_path)
    total_pages = file_size // PAGE_SZ
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(db_path, 'rb') as fin, open(out_path, 'wb') as fout:
        for pgno in range(1, total_pages + 1):
            page = fin.read(PAGE_SZ)
            if len(page) < PAGE_SZ:
                if len(page) > 0:
                    page = page + b'\x00' * (PAGE_SZ - len(page))
                else:
                    break
            fout.write(decrypt_page(enc_key, page, pgno))
    return total_pages


def decrypt_wal(wal_path, out_path, enc_key):
    if not os.path.exists(wal_path):
        return 0
    wal_size = os.path.getsize(wal_path)
    if wal_size <= WAL_HEADER_SZ:
        return 0
    frame_size = WAL_FRAME_HEADER_SZ + PAGE_SZ
    patched = 0
    with open(wal_path, 'rb') as wf, open(out_path, 'r+b') as df:
        wal_hdr = wf.read(WAL_HEADER_SZ)
        wal_salt1 = struct.unpack('>I', wal_hdr[16:20])[0]
        wal_salt2 = struct.unpack('>I', wal_hdr[20:24])[0]
        while wf.tell() + frame_size <= wal_size:
            fh = wf.read(WAL_FRAME_HEADER_SZ)
            if len(fh) < WAL_FRAME_HEADER_SZ:
                break
            pgno = struct.unpack('>I', fh[0:4])[0]
            frame_salt1 = struct.unpack('>I', fh[8:12])[0]
            frame_salt2 = struct.unpack('>I', fh[12:16])[0]
            ep = wf.read(PAGE_SZ)
            if len(ep) < PAGE_SZ:
                break
            if pgno == 0 or pgno > 1000000:
                continue
            if frame_salt1 != wal_salt1 or frame_salt2 != wal_salt2:
                continue
            dec = decrypt_page(enc_key, ep, pgno)
            df.seek((pgno - 1) * PAGE_SZ)
            df.write(dec)
            patched += 1
    return patched


# ============ DB 缓存 ============

class DBCache:
    """缓存解密后的 DB，通过 mtime 检测变化。使用固定文件名，重启后可复用。"""

    CACHE_DIR = os.path.join(tempfile.gettempdir(), "wechat_mcp_cache")
    MTIME_FILE = os.path.join(tempfile.gettempdir(), "wechat_mcp_cache", "_mtimes.json")

    def __init__(self):
        self._cache = {}  # rel_key -> (db_mtime, wal_mtime, tmp_path)
        os.makedirs(self.CACHE_DIR, exist_ok=True)
        self._load_persistent_cache()

    def _cache_path(self, rel_key):
        """rel_key -> 固定的缓存文件路径"""
        h = hashlib.md5(rel_key.encode()).hexdigest()[:12]
        return os.path.join(self.CACHE_DIR, f"{h}.db")

    def _load_persistent_cache(self):
        """启动时从磁盘恢复缓存映射，验证 mtime 后复用"""
        if not os.path.exists(self.MTIME_FILE):
            return
        try:
            with open(self.MTIME_FILE, encoding="utf-8") as f:
                saved = json.load(f)
        except (json.JSONDecodeError, OSError):
            return
        reused = 0
        for rel_key, info in saved.items():
            tmp_path = info["path"]
            if not os.path.exists(tmp_path):
                continue
            rel_path = rel_key.replace('\\', os.sep)
            db_path = os.path.join(DB_DIR, rel_path)
            wal_path = db_path + "-wal"
            try:
                db_mtime = os.path.getmtime(db_path)
                wal_mtime = os.path.getmtime(wal_path) if os.path.exists(wal_path) else 0
            except OSError:
                continue
            if db_mtime == info["db_mt"] and wal_mtime == info["wal_mt"]:
                self._cache[rel_key] = (db_mtime, wal_mtime, tmp_path)
                reused += 1
        if reused:
            print(f"[DBCache] reused {reused} cached decrypted DBs from previous run", flush=True)

    def _save_persistent_cache(self):
        """持久化缓存映射到磁盘"""
        data = {}
        for rel_key, (db_mt, wal_mt, path) in self._cache.items():
            data[rel_key] = {"db_mt": db_mt, "wal_mt": wal_mt, "path": path}
        try:
            with open(self.MTIME_FILE, 'w', encoding="utf-8") as f:
                json.dump(data, f)
        except OSError:
            pass

    def get(self, rel_key):
        key_info = get_key_info(ALL_KEYS, rel_key)
        if not key_info:
            return None
        rel_path = rel_key.replace('\\', '/').replace('/', os.sep)
        db_path = os.path.join(DB_DIR, rel_path)
        wal_path = db_path + "-wal"
        if not os.path.exists(db_path):
            return None

        try:
            db_mtime = os.path.getmtime(db_path)
            wal_mtime = os.path.getmtime(wal_path) if os.path.exists(wal_path) else 0
        except OSError:
            return None

        if rel_key in self._cache:
            c_db_mt, c_wal_mt, c_path = self._cache[rel_key]
            if c_db_mt == db_mtime and c_wal_mt == wal_mtime and os.path.exists(c_path):
                return c_path

        tmp_path = self._cache_path(rel_key)
        enc_key = bytes.fromhex(key_info["enc_key"])
        full_decrypt(db_path, tmp_path, enc_key)
        if os.path.exists(wal_path):
            decrypt_wal(wal_path, tmp_path, enc_key)
        self._cache[rel_key] = (db_mtime, wal_mtime, tmp_path)
        self._save_persistent_cache()
        return tmp_path

    def cleanup(self):
        """正常退出时保存缓存映射（不删文件，下次启动可复用）"""
        self._save_persistent_cache()


_cache = DBCache()
atexit.register(_cache.cleanup)


# ============ 联系人缓存 ============

_contact_names = None  # {username: display_name}
_contact_full = None   # [{username, nick_name, remark}]
_contact_tags = None   # {label_id: {name, sort_order, members: [{username, display_name}]}}
_self_username = None
_XML_UNSAFE_RE = re.compile(r'<!DOCTYPE|<!ENTITY', re.IGNORECASE)
_XML_PARSE_MAX_LEN = 20000
_QUERY_LIMIT_MAX = 500
_HISTORY_QUERY_BATCH_SIZE = 500


def _load_contacts_from(db_path):
    names = {}
    full = []
    conn = sqlite3.connect(db_path)
    try:
        for r in conn.execute("SELECT username, nick_name, remark FROM contact").fetchall():
            uname, nick, remark = r
            display = remark if remark else nick if nick else uname
            names[uname] = display
            full.append({'username': uname, 'nick_name': nick or '', 'remark': remark or ''})
    finally:
        conn.close()
    return names, full


def get_contact_names():
    global _contact_names, _contact_full
    if _contact_names is not None:
        return _contact_names

    # 优先用已解密的 contact.db
    pre_decrypted = os.path.join(DECRYPTED_DIR, "contact", "contact.db")
    if os.path.exists(pre_decrypted):
        try:
            _contact_names, _contact_full = _load_contacts_from(pre_decrypted)
            return _contact_names
        except Exception:
            pass

    # 实时解密
    path = _cache.get(os.path.join("contact", "contact.db"))
    if path:
        try:
            _contact_names, _contact_full = _load_contacts_from(path)
            return _contact_names
        except Exception:
            pass

    return {}


def get_contact_full():
    global _contact_full
    if _contact_full is None:
        get_contact_names()
    return _contact_full or []


def _get_contact_db_path():
    """获取 contact.db 路径（优先已解密，其次实时解密）"""
    pre = os.path.join(DECRYPTED_DIR, "contact", "contact.db")
    if os.path.exists(pre):
        return pre
    return _cache.get(os.path.join("contact", "contact.db"))


def _extract_pb_field_30(data):
    """从 extra_buffer (protobuf) 中提取 Field #30 的字符串值（联系人标签ID）"""
    if not data:
        return None
    pos = 0
    n = len(data)
    while pos < n:
        # 读 varint tag
        tag = 0
        shift = 0
        while pos < n:
            b = data[pos]; pos += 1
            tag |= (b & 0x7f) << shift
            if not (b & 0x80):
                break
            shift += 7
        field_num = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:  # varint
            while pos < n and data[pos] & 0x80:
                pos += 1
            pos += 1
        elif wire_type == 2:  # length-delimited
            length = 0; shift = 0
            while pos < n:
                b = data[pos]; pos += 1
                length |= (b & 0x7f) << shift
                if not (b & 0x80):
                    break
                shift += 7
            if field_num == 30:
                try:
                    return data[pos:pos + length].decode('utf-8')
                except Exception:
                    return None
            pos += length
        elif wire_type == 1:  # 64-bit
            pos += 8
        elif wire_type == 5:  # 32-bit
            pos += 4
        else:
            break
    return None


def _load_contact_tags():
    """加载并缓存联系人标签数据"""
    global _contact_tags
    if _contact_tags is not None:
        return _contact_tags

    db_path = _get_contact_db_path()
    if not db_path:
        return {}

    try:
        conn = sqlite3.connect(db_path)
    except Exception:
        return {}

    try:
        # 1. 加载标签定义
        try:
            label_rows = conn.execute(
                "SELECT label_id_, label_name_, sort_order_ FROM contact_label ORDER BY sort_order_"
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
        if not label_rows:
            return {}

        labels = {}
        for lid, lname, sort_order in label_rows:
            labels[lid] = {'name': lname, 'sort_order': sort_order, 'members': []}

        # 2. 扫描联系人的标签关联
        names = get_contact_names()
        rows = conn.execute(
            "SELECT username, extra_buffer FROM contact WHERE extra_buffer IS NOT NULL"
        ).fetchall()

        for username, buf in rows:
            label_str = _extract_pb_field_30(buf)
            if not label_str:
                continue
            display = names.get(username, username)
            for lid_s in label_str.split(','):
                try:
                    lid = int(lid_s.strip())
                except (ValueError, AttributeError):
                    continue
                if lid in labels:
                    labels[lid]['members'].append({'username': username, 'display_name': display})

        _contact_tags = labels
        return _contact_tags
    except Exception:
        return {}
    finally:
        conn.close()


# ============ 辅助函数 ============

def format_msg_type(t):
    base_type, _ = _split_msg_type(t)
    return {
        1: '文本', 3: '图片', 34: '语音', 42: '名片',
        43: '视频', 47: '表情', 48: '位置', 49: '链接/文件',
        50: '通话', 10000: '系统', 10002: '撤回',
    }.get(base_type, f'type={t}')


def _split_msg_type(t):
    try:
        t = int(t)
    except (TypeError, ValueError):
        return 0, 0
    # WeChat packs the base type into the low 32 bits and app subtype into the high 32 bits.
    if t > 0xFFFFFFFF:
        return t & 0xFFFFFFFF, t >> 32
    return t, 0


def resolve_username(chat_name):
    """将聊天名/备注名/wxid 解析为 username"""
    names = get_contact_names()

    # 直接是 username
    if chat_name in names or chat_name.startswith('wxid_') or '@chatroom' in chat_name:
        return chat_name

    # 模糊匹配(优先精确包含)
    chat_lower = chat_name.lower()
    for uname, display in names.items():
        if chat_lower == display.lower():
            return uname
    for uname, display in names.items():
        if chat_lower in display.lower():
            return uname

    return None


_zstd_dctx = zstd.ZstdDecompressor()


def _decompress_content(content, ct):
    """解压 zstd 压缩的消息内容"""
    if ct and ct == 4 and isinstance(content, bytes):
        try:
            return _zstd_dctx.decompress(content).decode('utf-8', errors='replace')
        except Exception:
            return None
    if isinstance(content, bytes):
        try:
            return content.decode('utf-8', errors='replace')
        except Exception:
            return None
    return content


def _parse_message_content(content, local_type, is_group):
    """解析消息内容，返回 (sender_id, text)。

    群消息 content 形如 'wxid_xxx:\n<xml...>'；某些 type=19 合并转发也会
    写成 'wxid_xxx:<?xml...' 或 'wxid_xxx:<msg...' 不带换行——剥离逻辑两种都要处理。
    """
    if content is None:
        return '', ''
    if isinstance(content, bytes):
        return '', '(二进制内容)'

    sender = ''
    text = content
    if is_group:
        if ':\n' in content:
            sender, text = content.split(':\n', 1)
        else:
            # 'sender:<?xml...' / 'sender:<msg...' 等无换行 case
            m = re.match(r'^([A-Za-z0-9_\-@.]+):(<\?xml|<msg|<msglist|<voipmsg|<sysmsg)', content)
            if m:
                sender = m.group(1)
                text = content[len(sender) + 1:]

    return sender, text


def _collapse_text(text):
    if not text:
        return ''
    return re.sub(r'\s+', ' ', text).strip()


def _get_self_username():
    global _self_username
    if _self_username:
        return _self_username

    if not DB_DIR:
        return ''

    names = get_contact_names()
    account_dir = os.path.basename(os.path.dirname(DB_DIR))
    candidates = [account_dir]

    m = re.fullmatch(r'(.+)_([0-9a-fA-F]{4,})', account_dir)
    if m:
        candidates.insert(0, m.group(1))

    for candidate in candidates:
        if candidate and candidate in names:
            _self_username = candidate
            return _self_username

    return ''


def _load_name2id_maps(conn):
    id_to_username = {}
    try:
        rows = conn.execute("SELECT rowid, user_name FROM Name2Id").fetchall()
    except sqlite3.Error:
        return id_to_username

    for rowid, user_name in rows:
        if not user_name:
            continue
        id_to_username[rowid] = user_name
    return id_to_username


def _display_name_for_username(username, names):
    if not username:
        return ''
    if username == _get_self_username():
        return 'me'
    return names.get(username, username)


def _resolve_sender_label(real_sender_id, sender_from_content, is_group, chat_username, chat_display_name, names, id_to_username):
    sender_username = id_to_username.get(real_sender_id, '')

    if is_group:
        if sender_username and sender_username != chat_username:
            return _display_name_for_username(sender_username, names)
        if sender_from_content:
            return _display_name_for_username(sender_from_content, names)
        return ''

    if sender_username == chat_username:
        return chat_display_name
    if sender_username:
        return _display_name_for_username(sender_username, names)
    return ''


def _resolve_quote_sender_label(ref_user, ref_display_name, is_group, chat_username, chat_display_name, names):
    if is_group:
        if ref_user:
            return _display_name_for_username(ref_user, names)
        return ref_display_name or ''

    self_username = _get_self_username()
    if ref_user:
        if ref_user == chat_username:
            return chat_display_name
        if self_username and ref_user == self_username:
            return 'me'
        return names.get(ref_user, ref_display_name or ref_user)
    if ref_display_name:
        if ref_display_name == chat_display_name:
            return chat_display_name
        self_display_name = names.get(self_username, self_username) if self_username else ''
        if self_display_name and ref_display_name == self_display_name:
            return 'me'
        return ref_display_name
    return ''


# 合并转发消息（含 recorditem 内嵌 XML）在 dataitem 数量多时显著超过默认 20K 上限，
# 实测真实 outer XML 可达 ~500KB。caller 可通过 max_len 参数为 type=19 类大消息放宽限制。
_RECORD_XML_PARSE_MAX_LEN = 500_000


def _safe_basename(name):
    """对 user-derived filename（从消息 XML 来，不可信）做严格 sanitize。

    Reject 而不是 normalize：哪怕 os.path.basename 把 '../foo' 剥成 'foo' 是
    safe 的，意图依然可疑，应该显式失败让用户看到。
    """
    if not name:
        return ''
    if '\x00' in name:
        return ''
    if os.path.isabs(name):
        return ''
    # 任何 path separator 或 .. component 直接拒（不做 normalize）
    parts = name.replace('\\', '/').split('/')
    if any(p in ('', '.', '..') for p in parts) and len(parts) > 1:
        return ''
    if len(parts) > 1:
        return ''
    if name in ('.', '..'):
        return ''
    return name


def _path_under_root(path, root):
    """resolve realpath 后确认仍在 root 下（防 symlink 跳出）。"""
    try:
        real_path = os.path.realpath(path)
        real_root = os.path.realpath(root)
    except OSError:
        return False
    return real_path == real_root or real_path.startswith(real_root + os.sep)


# 大附件 md5 校验时的安全上限：超过此 size 直接拒绝校验（避免 MCP 进程
# 在 100MB+ 视频/附件上一次性 read() 整文件爆内存或长时间阻塞）。
_MD5_VERIFY_MAX_SIZE = 500 * 1024 * 1024  # 500 MB
_MD5_CHUNK_SIZE = 64 * 1024  # 64 KB


def _md5_file_chunked(path, max_size=_MD5_VERIFY_MAX_SIZE):
    """流式分块计算文件 md5，避免大文件一次读完爆内存。

    超过 max_size 直接拒绝（DoS 防御 + 大附件 md5 校验现实意义不大）。
    返回 (md5_hex, error)；成功时 error 为 None。
    """
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return None, f"无法读取文件 size: {e}"
    if size > max_size:
        return None, f"文件 size {size:,} 超过 md5 校验上限 {max_size:,}（防 DoS）"
    h = hashlib.md5()
    try:
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(_MD5_CHUNK_SIZE)
                if not chunk:
                    break
                h.update(chunk)
    except OSError as e:
        return None, f"读取文件失败: {e}"
    return h.hexdigest().lower(), None


def _parse_xml_root(content, max_len=_XML_PARSE_MAX_LEN):
    if not content or len(content) > max_len or _XML_UNSAFE_RE.search(content):
        return None

    try:
        return ET.fromstring(content)
    except ET.ParseError:
        return None


def _parse_int(value, fallback=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _parse_app_message_outer(content):
    """Parse outer appmsg XML，对 type=19 合并卡片自动放宽到 _RECORD_XML_PARSE_MAX_LEN。

    所有解析 outer appmsg 的 caller（get_chat_history 渲染 / decode_file_message /
    decode_record_item）共用此 helper，避免同一条大消息在不同 caller 上行为不一致。
    Substring 短路保证非 type=19 的大 appmsg 不付出 500K parse 代价。"""
    root = _parse_xml_root(content)
    if root is None and content and len(content) <= _RECORD_XML_PARSE_MAX_LEN:
        if '<type>19</type>' in content:
            root = _parse_xml_root(content, max_len=_RECORD_XML_PARSE_MAX_LEN)
    return root


def _format_app_message_text(content, local_type, is_group, chat_username, chat_display_name, names):
    if not content or '<appmsg' not in content:
        return None

    _, sub_type = _split_msg_type(local_type)
    root = _parse_app_message_outer(content)
    if root is None:
        return None

    appmsg = root.find('.//appmsg')
    if appmsg is None:
        return None

    title = _collapse_text(appmsg.findtext('title') or '')
    app_type_text = (appmsg.findtext('type') or '').strip()
    app_type = _parse_int(app_type_text, _parse_int(sub_type, 0))

    if app_type == 57:
        ref = appmsg.find('.//refermsg')
        ref_user = ''
        ref_display_name = ''
        ref_content = ''
        if ref is not None:
            ref_user = (ref.findtext('fromusr') or '').strip()
            ref_display_name = (ref.findtext('displayname') or '').strip()
            ref_content = _collapse_text(ref.findtext('content') or '')
        if len(ref_content) > 160:
            ref_content = ref_content[:160] + "..."

        quote_text = title or "[引用消息]"
        if ref_content:
            ref_label = _resolve_quote_sender_label(
                ref_user, ref_display_name, is_group, chat_username, chat_display_name, names
            )
            prefix = f"回复 {ref_label}: " if ref_label else "回复: "
            quote_text += f"\n  ↳ {prefix}{ref_content}"
        return quote_text

    if app_type == 19:
        return _format_record_message_text(appmsg, title)

    if app_type == 6:
        return f"[文件] {title}" if title else "[文件]"
    if app_type == 5:
        return f"[链接] {title}" if title else "[链接]"
    if app_type in (33, 36, 44):
        return f"[小程序] {title}" if title else "[小程序]"
    if title:
        return f"[链接/文件] {title}"
    return "[链接/文件]"


_RECORD_MAX_ITEMS = 50
_RECORD_MAX_LINE_LEN = 200

# 合并转发 dataitem 的 datatype → wechat 缓存子目录映射。仅这 4 类有真本地
# binary 文件；其他 datatype（链接/名片/小程序/视频号 等）只有 metadata。
_RECORD_BINARY_SUBDIR = {'8': 'F', '2': 'Img', '5': 'V', '4': 'A'}

# datatype → 中文标签，散在多处使用：渲染合并卡片 / decode_record_item 的
# 错误提示 / 单元测试。统一在模块顶部维护避免漂移。
_RECORD_DATATYPE_LABEL = {
    '1': '文本', '2': '图片', '3': '名片', '4': '语音',
    '5': '视频', '6': '链接', '7': '位置', '8': '文件',
    '17': '聊天记录', '19': '小程序', '22': '视频号',
    '23': '视频号直播', '29': '音乐', '36': '小程序/H5',
    '37': '表情包',
}


def _format_record_dataitem(item):
    """格式化合并记录中的单个 dataitem，返回展示文本。"""
    datatype = (item.get('datatype') or '').strip()

    if datatype == '1':
        return _collapse_text(item.findtext('datadesc') or '') or '[文本]'
    if datatype in ('2', '3', '4', '5', '7', '23', '37'):
        return f"[{_RECORD_DATATYPE_LABEL[datatype]}]"
    if datatype in ('6', '36'):
        link_title = _collapse_text(item.findtext('datatitle') or '')
        label = _RECORD_DATATYPE_LABEL[datatype]
        return f"[{label}] {link_title}" if link_title else f"[{label}]"
    if datatype == '8':
        file_title = _collapse_text(item.findtext('datatitle') or '')
        return f"[文件] {file_title}" if file_title else '[文件]'
    if datatype == '17':
        nested_title = _collapse_text(item.findtext('datatitle') or '')
        return f"[聊天记录] {nested_title}" if nested_title else '[聊天记录]'
    if datatype == '19':
        # 小程序：appbranditem/sourcedisplayname 是直接子代，不需要 .// 递归
        app_name = _collapse_text(item.findtext('appbranditem/sourcedisplayname') or '')
        item_title = _collapse_text(item.findtext('datatitle') or '')
        label = item_title or app_name or '小程序'
        return f"[小程序] {label}"
    if datatype == '22':
        feed_desc = _collapse_text(item.findtext('finderFeed/desc') or '')
        return f"[视频号] {feed_desc[:80]}" if feed_desc else '[视频号]'
    if datatype == '29':
        song = _collapse_text(item.findtext('datatitle') or '')
        artist = _collapse_text(item.findtext('datadesc') or '')
        if song and artist:
            return f"[音乐] {song} - {artist}"
        return f"[音乐] {song}" if song else '[音乐]'

    desc = _collapse_text(item.findtext('datadesc') or '')
    title_text = _collapse_text(item.findtext('datatitle') or '')
    fallback = desc or title_text
    return fallback if fallback else f"[未知类型 {datatype}]"


def _format_record_message_text(appmsg, title):
    """解析合并转发的聊天记录卡片（appmsg type=19, recorditem）。"""
    fallback_title = title or '聊天记录'
    record_node = appmsg.find('recorditem')
    if record_node is None or not record_node.text:
        return f"[聊天记录] {fallback_title}（待加载）"

    inner = _parse_xml_root(record_node.text, max_len=_RECORD_XML_PARSE_MAX_LEN)
    if inner is None:
        return f"[聊天记录] {fallback_title}"

    record_title = _collapse_text(inner.findtext('title') or '') or fallback_title
    is_chatroom = (inner.findtext('isChatRoom') or '').strip() == '1'
    datalist = inner.find('datalist')
    items = list(datalist.findall('dataitem')) if datalist is not None else []
    if not items:
        suffix = "（群聊转发，待加载）" if is_chatroom else "（待加载）"
        return f"[聊天记录] {record_title}{suffix}"

    header = f"[聊天记录] {record_title}"
    if is_chatroom:
        header += "（群聊转发）"
    header += f"，共 {len(items)} 条"

    lines = [header + ":"]
    for idx, item in enumerate(items[:_RECORD_MAX_ITEMS]):
        sender = _collapse_text(item.findtext('sourcename') or '')
        when = _collapse_text(item.findtext('sourcetime') or '')
        content = _format_record_dataitem(item)

        if len(content) > _RECORD_MAX_LINE_LEN:
            content = content[:_RECORD_MAX_LINE_LEN] + '…'

        # 0-based index 让用户能用 decode_record_item(chat, local_id, item_index) 引用
        prefix_parts = [f"[{idx}]"] + [p for p in (when, sender) if p]
        prefix = ' '.join(prefix_parts)
        lines.append(f"  {prefix}: {content}")

    if len(items) > _RECORD_MAX_ITEMS:
        lines.append(f"  …（还有 {len(items) - _RECORD_MAX_ITEMS} 条未显示）")

    return "\n".join(lines)


def _format_voip_message_text(content):
    if not content or '<voip' not in content:
        return None

    root = _parse_xml_root(content)
    if root is None:
        return "[通话]"

    raw_text = _collapse_text(root.findtext('.//msg') or '')
    if not raw_text:
        return "[通话]"

    status_map = {
        'Canceled': '已取消',
        'Line busy': '对方忙线',
        'Already answered elsewhere': '已在其他设备接听',
        'Declined on other device': '已在其他设备拒接',
        'Call canceled by caller': '主叫已取消',
        'Call not answered': '未接听',
        "Call wasn't answered": '未接听',
    }

    if raw_text.startswith('Duration:'):
        duration = raw_text.split(':', 1)[1].strip()
        return f"[通话] 通话时长 {duration}" if duration else "[通话]"

    return f"[通话] {status_map.get(raw_text, raw_text)}"


def _format_message_text(local_id, local_type, content, is_group, chat_username, chat_display_name, names, create_time=0):
    sender_from_content, text = _parse_message_content(content, local_type, is_group)
    base_type, _ = _split_msg_type(local_type)

    # 同一 chat 的消息可能跨 message_N.db 分片，导致 local_id 跨分片冲突。
    # 把 create_time 一起注入到输出，让 decode_file_message / decode_record_item
    # 能用 (local_id, create_time) 唯一定位 row。
    def _id_suffix():
        return f"(local_id={local_id}, ts={create_time})" if create_time else f"(local_id={local_id})"

    if base_type == 3:
        text = f"[图片] {_id_suffix()}"
    elif base_type == 47:
        text = "[表情]"
    elif base_type == 50:
        text = _format_voip_message_text(text) or "[通话]"
    elif base_type == 49:
        formatted = _format_app_message_text(
            text, local_type, is_group, chat_username, chat_display_name, names
        ) or "[链接/文件]"
        if formatted.startswith('[文件]'):
            formatted = f"{formatted} {_id_suffix()}"
        elif formatted.startswith('[聊天记录]'):
            # 多行：把 ID 后缀放在 header 末尾，":" 之前
            if '\n' in formatted:
                first_line, rest = formatted.split('\n', 1)
                first_line_no_colon = first_line.rstrip(':').rstrip()
                formatted = f"{first_line_no_colon} {_id_suffix()}:\n{rest}"
            else:
                formatted = f"{formatted} {_id_suffix()}"
        text = formatted
    elif base_type != 1:
        type_label = format_msg_type(local_type)
        text = f"[{type_label}] {text}" if text else f"[{type_label}]"

    return sender_from_content, text


def _is_safe_msg_table_name(table_name):
    return bool(re.fullmatch(r'Msg_[0-9a-f]{32}', table_name))


# 消息 DB 的 rel_keys
# 用 message_\d+\.db$ 匹配，自然排除 message_resource.db / message_fts_*.db
MSG_DB_KEYS = sorted([
    k for k in ALL_KEYS
    if any(v.startswith("message/") for v in key_path_variants(k))
    and any(re.search(r"message_\d+\.db$", v) for v in key_path_variants(k))
])


def _find_msg_table_for_user(username):
    """在所有 message_N.db 中查找用户的消息表，返回 (db_path, table_name)"""
    table_hash = hashlib.md5(username.encode()).hexdigest()
    table_name = f"Msg_{table_hash}"
    if not _is_safe_msg_table_name(table_name):
        return None, None

    for rel_key in MSG_DB_KEYS:
        path = _cache.get(rel_key)
        if not path:
            continue
        conn = sqlite3.connect(path)
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,)
            ).fetchone()
            if exists:
                conn.close()
                return path, table_name
        except Exception:
            pass
        finally:
            conn.close()

    return None, None


def _find_msg_tables_for_user(username):
    """返回用户在所有 message_N.db 中对应的消息表，按最新消息时间倒序排列。"""
    table_hash = hashlib.md5(username.encode()).hexdigest()
    table_name = f"Msg_{table_hash}"
    if not _is_safe_msg_table_name(table_name):
        return []

    matches = []
    for rel_key in MSG_DB_KEYS:
        path = _cache.get(rel_key)
        if not path:
            continue
        conn = sqlite3.connect(path)
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,)
            ).fetchone()
            if not exists:
                continue
            max_create_time = conn.execute(
                f"SELECT MAX(create_time) FROM [{table_name}]"
            ).fetchone()[0] or 0
            matches.append({
                'db_path': path,
                'table_name': table_name,
                'max_create_time': max_create_time,
            })
        except Exception:
            pass
        finally:
            conn.close()

    matches.sort(key=lambda item: item['max_create_time'], reverse=True)
    return matches


def _validate_pagination(limit, offset=0, limit_max=_QUERY_LIMIT_MAX):
    if limit <= 0:
        raise ValueError("limit 必须大于 0")
    if limit_max is not None and limit > limit_max:
        raise ValueError(f"limit 不能大于 {limit_max}")
    if offset < 0:
        raise ValueError("offset 不能小于 0")


def _parse_time_value(value, field_name, is_end=False):
    value = (value or '').strip()
    if not value:
        return None

    formats = [
        ('%Y-%m-%d %H:%M:%S', False),
        ('%Y-%m-%d %H:%M', False),
        ('%Y-%m-%d', True),
    ]
    for fmt, date_only in formats:
        try:
            dt = datetime.strptime(value, fmt)
            if date_only and is_end:
                dt = dt.replace(hour=23, minute=59, second=59)
            return int(dt.timestamp())
        except ValueError:
            continue

    raise ValueError(
        f"{field_name} 格式无效: {value}。支持 YYYY-MM-DD / YYYY-MM-DD HH:MM / YYYY-MM-DD HH:MM:SS"
    )


def _parse_time_range(start_time='', end_time=''):
    start_ts = _parse_time_value(start_time, 'start_time', is_end=False)
    end_ts = _parse_time_value(end_time, 'end_time', is_end=True)
    if start_ts is not None and end_ts is not None and start_ts > end_ts:
        raise ValueError('start_time 不能晚于 end_time')
    return start_ts, end_ts


def _build_message_filters(start_ts=None, end_ts=None, keyword=''):
    clauses = []
    params = []
    if start_ts is not None:
        clauses.append('create_time >= ?')
        params.append(start_ts)
    if end_ts is not None:
        clauses.append('create_time <= ?')
        params.append(end_ts)
    if keyword:
        clauses.append('message_content LIKE ?')
        params.append(f'%{keyword}%')
    return clauses, params


def _query_messages(conn, table_name, start_ts=None, end_ts=None, keyword='', limit=20, offset=0, oldest_first=False):
    if not _is_safe_msg_table_name(table_name):
        raise ValueError(f'非法消息表名: {table_name}')

    clauses, params = _build_message_filters(start_ts, end_ts, keyword)
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ''
    order = 'ASC' if oldest_first else 'DESC'
    sql = f"""
        SELECT local_id, local_type, create_time, real_sender_id, message_content,
               WCDB_CT_message_content
        FROM [{table_name}]
        {where_sql}
        ORDER BY create_time {order}
    """
    if limit is None:
        return conn.execute(sql, params).fetchall()
    sql += "\n        LIMIT ? OFFSET ?"
    return conn.execute(sql, (*params, limit, offset)).fetchall()


def _resolve_chat_context(chat_name):
    username = resolve_username(chat_name)
    if not username:
        return None

    names = get_contact_names()
    display_name = names.get(username, username)
    message_tables = _find_msg_tables_for_user(username)
    if not message_tables:
        return {
            'query': chat_name,
            'username': username,
            'display_name': display_name,
            'db_path': None,
            'table_name': None,
            'message_tables': [],
            'is_group': '@chatroom' in username,
        }

    primary = message_tables[0]
    return {
        'query': chat_name,
        'username': username,
        'display_name': display_name,
        'db_path': primary['db_path'],
        'table_name': primary['table_name'],
        'message_tables': message_tables,
        'is_group': '@chatroom' in username,
    }


def _resolve_chat_contexts(chat_names):
    if not chat_names:
        raise ValueError('chat_names 不能为空')

    resolved = []
    unresolved = []
    missing_tables = []
    seen = set()

    for chat_name in chat_names:
        name = (chat_name or '').strip()
        if not name:
            unresolved.append('(空)')
            continue
        ctx = _resolve_chat_context(name)
        if not ctx:
            unresolved.append(name)
            continue
        if not ctx['message_tables']:
            missing_tables.append(ctx['display_name'])
            continue
        if ctx['username'] in seen:
            continue
        seen.add(ctx['username'])
        resolved.append(ctx)

    return resolved, unresolved, missing_tables


def _normalize_chat_names(chat_name):
    if chat_name is None:
        return []
    if isinstance(chat_name, str):
        value = chat_name.strip()
        return [value] if value else []
    if isinstance(chat_name, (list, tuple, set)):
        normalized = []
        for item in chat_name:
            if item is None:
                continue
            value = str(item).strip()
            if value:
                normalized.append(value)
        return normalized
    value = str(chat_name).strip()
    return [value] if value else []


def _format_history_lines(rows, username, display_name, is_group, names, id_to_username):
    lines = []
    ctx = {
        'username': username,
        'display_name': display_name,
        'is_group': is_group,
    }
    for row in reversed(rows):
        _, line = _build_history_line(row, ctx, names, id_to_username)
        lines.append(line)
    return lines


def _build_search_entry(row, ctx, names, id_to_username):
    local_id, local_type, create_time, real_sender_id, content, ct = row
    content = _decompress_content(content, ct)
    if content is None:
        return None

    sender, text = _format_message_text(
        local_id, local_type, content, ctx['is_group'], ctx['username'], ctx['display_name'], names,
        create_time=create_time,
    )
    if text and len(text) > 300:
        text = text[:300] + '...'

    sender_label = _resolve_sender_label(
        real_sender_id,
        sender,
        ctx['is_group'],
        ctx['username'],
        ctx['display_name'],
        names,
        id_to_username,
    )
    time_str = datetime.fromtimestamp(create_time).strftime('%Y-%m-%d %H:%M')
    entry = f"[{time_str}] [{ctx['display_name']}]"
    if sender_label:
        entry += f" {sender_label}:"
    entry += f" {text}"
    return create_time, entry


def _build_history_line(row, ctx, names, id_to_username):
    local_id, local_type, create_time, real_sender_id, content, ct = row
    time_str = datetime.fromtimestamp(create_time).strftime('%Y-%m-%d %H:%M')
    content = _decompress_content(content, ct)
    if content is None:
        content = '(无法解压)'

    sender, text = _format_message_text(
        local_id, local_type, content, ctx['is_group'], ctx['username'], ctx['display_name'], names,
        create_time=create_time,
    )

    sender_label = _resolve_sender_label(
        real_sender_id, sender, ctx['is_group'], ctx['username'], ctx['display_name'], names, id_to_username
    )
    if sender_label:
        return create_time, f'[{time_str}] {sender_label}: {text}'
    return create_time, f'[{time_str}] {text}'


def _get_chat_message_tables(ctx):
    if ctx.get('message_tables'):
        return ctx['message_tables']
    if ctx.get('db_path') and ctx.get('table_name'):
        return [{'db_path': ctx['db_path'], 'table_name': ctx['table_name']}]
    return []


def _iter_table_contexts(ctx):
    for table in _get_chat_message_tables(ctx):
        yield {
            'query': ctx['query'],
            'username': ctx['username'],
            'display_name': ctx['display_name'],
            'db_path': table['db_path'],
            'table_name': table['table_name'],
            'is_group': ctx['is_group'],
        }


def _candidate_page_size(limit, offset):
    return limit + offset


def _message_query_batch_size(candidate_limit):
    return candidate_limit


def _history_query_batch_size(candidate_limit):
    return min(candidate_limit, _HISTORY_QUERY_BATCH_SIZE)


def _page_ranked_entries(entries, limit, offset, oldest_first=False):
    ordered = sorted(entries, key=lambda item: item[0], reverse=not oldest_first)
    paged = ordered[offset:offset + limit]
    paged.sort(key=lambda item: item[0])
    return paged


def _collect_chat_history_lines(ctx, names, start_ts=None, end_ts=None, limit=20, offset=0, oldest_first=False):
    collected = []
    failures = []
    candidate_limit = _candidate_page_size(limit, offset)
    batch_size = _history_query_batch_size(candidate_limit)

    for table_ctx in _iter_table_contexts(ctx):
        try:
            with closing(sqlite3.connect(table_ctx['db_path'])) as conn:
                id_to_username = _load_name2id_maps(conn)
                fetch_offset = 0
                collected_before_table = len(collected)
                # 当前页上的消息一定落在各分表最近的 offset+limit 条记录内。
                while len(collected) - collected_before_table < candidate_limit:
                    rows = _query_messages(
                        conn,
                        table_ctx['table_name'],
                        start_ts=start_ts,
                        end_ts=end_ts,
                        limit=batch_size,
                        offset=fetch_offset,
                        oldest_first=oldest_first,
                    )
                    if not rows:
                        break
                    fetch_offset += len(rows)

                    for row in rows:
                        try:
                            collected.append(_build_history_line(row, table_ctx, names, id_to_username))
                        except Exception as e:
                            failures.append(
                                f"{table_ctx['display_name']} local_id={row[0]} create_time={row[2]}: {e}"
                            )
                        if len(collected) - collected_before_table >= candidate_limit:
                            break

                    if len(rows) < batch_size:
                        break
        except Exception as e:
            failures.append(f"{table_ctx['db_path']}: {e}")

    paged = _page_ranked_entries(collected, limit, offset, oldest_first=oldest_first)
    return [line for _, line in paged], failures


def _collect_chat_search_entries(ctx, names, keyword, start_ts=None, end_ts=None, candidate_limit=20):
    collected = []
    failures = []
    contexts_by_db = {}
    for table_ctx in _iter_table_contexts(ctx):
        contexts_by_db.setdefault(table_ctx['db_path'], []).append(table_ctx)

    for db_path, db_contexts in contexts_by_db.items():
        try:
            with closing(sqlite3.connect(db_path)) as conn:
                db_entries, db_failures = _collect_search_entries(
                    conn,
                    db_contexts,
                    names,
                    keyword,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    candidate_limit=candidate_limit,
                )
                collected.extend(db_entries)
                failures.extend(db_failures)
        except Exception as e:
            failures.extend(f"{table_ctx['display_name']}: {e}" for table_ctx in db_contexts)

    return collected, failures


def _load_search_contexts_from_db(conn, db_path, names):
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
    ).fetchall()

    table_to_username = {}
    try:
        for (user_name,) in conn.execute("SELECT user_name FROM Name2Id").fetchall():
            if not user_name:
                continue
            table_hash = hashlib.md5(user_name.encode()).hexdigest()
            table_to_username[f"Msg_{table_hash}"] = user_name
    except sqlite3.Error:
        pass

    contexts = []
    for (table_name,) in tables:
        username = table_to_username.get(table_name, '')
        display_name = names.get(username, username) if username else table_name
        contexts.append({
            'query': display_name,
            'username': username,
            'display_name': display_name,
            'db_path': db_path,
            'table_name': table_name,
            'is_group': '@chatroom' in username,
        })
    return contexts


def _collect_search_entries(conn, contexts, names, keyword, start_ts=None, end_ts=None, candidate_limit=20):
    collected = []
    failures = []
    id_to_username = _load_name2id_maps(conn)
    batch_size = _message_query_batch_size(candidate_limit)

    for ctx in contexts:
        try:
            fetch_offset = 0
            collected_before_table = len(collected)
            # 全局分页只需要每个分表最新的 offset+limit 条有效命中，无需把整表命中读进内存。
            while len(collected) - collected_before_table < candidate_limit:
                rows = _query_messages(
                    conn,
                    ctx['table_name'],
                    start_ts=start_ts,
                    end_ts=end_ts,
                    keyword=keyword,
                    limit=batch_size,
                    offset=fetch_offset,
                )
                if not rows:
                    break
                fetch_offset += len(rows)

                for row in rows:
                    formatted = _build_search_entry(row, ctx, names, id_to_username)
                    if formatted:
                        collected.append(formatted)
                        if len(collected) - collected_before_table >= candidate_limit:
                            break

                if len(rows) < batch_size:
                    break
        except Exception as e:
            failures.append(f"{ctx['display_name']}: {e}")

    return collected, failures


def _page_search_entries(entries, limit, offset):
    return _page_ranked_entries(entries, limit, offset)


def _search_single_chat(ctx, keyword, start_ts, end_ts, start_time, end_time, limit, offset):
    names = get_contact_names()
    candidate_limit = _candidate_page_size(limit, offset)

    entries, failures = _collect_chat_search_entries(
        ctx,
        names,
        keyword,
        start_ts=start_ts,
        end_ts=end_ts,
        candidate_limit=candidate_limit,
    )

    paged = _page_search_entries(entries, limit, offset)

    if not paged:
        if failures:
            return "查询失败: " + "；".join(failures)
        return f"未在 {ctx['display_name']} 中找到包含 \"{keyword}\" 的消息"

    header = f"在 {ctx['display_name']} 中搜索 \"{keyword}\" 找到 {len(paged)} 条结果（offset={offset}, limit={limit}）"
    if start_time or end_time:
        header += f"\n时间范围: {start_time or '最早'} ~ {end_time or '最新'}"
    if failures:
        header += "\n查询失败: " + "；".join(failures)
    return header + ":\n\n" + "\n\n".join(item[1] for item in paged)


def _search_multiple_chats(chat_names, keyword, start_ts, end_ts, start_time, end_time, limit, offset):
    try:
        resolved_contexts, unresolved, missing_tables = _resolve_chat_contexts(chat_names)
    except ValueError as e:
        return f"错误: {e}"

    if not resolved_contexts:
        details = []
        if unresolved:
            details.append("未找到联系人: " + "、".join(unresolved))
        if missing_tables:
            details.append("无消息表: " + "、".join(missing_tables))
        suffix = f"\n{chr(10).join(details)}" if details else ""
        return f"错误: 没有可查询的聊天对象{suffix}"

    names = get_contact_names()
    candidate_limit = _candidate_page_size(limit, offset)
    collected = []
    failures = []
    for ctx in resolved_contexts:
        chat_entries, chat_failures = _collect_chat_search_entries(
            ctx,
            names,
            keyword,
            start_ts=start_ts,
            end_ts=end_ts,
            candidate_limit=candidate_limit,
        )
        collected.extend(chat_entries)
        failures.extend(chat_failures)

    paged = _page_search_entries(collected, limit, offset)

    notes = []
    if unresolved:
        notes.append("未找到联系人: " + "、".join(unresolved))
    if missing_tables:
        notes.append("无消息表: " + "、".join(missing_tables))
    if failures:
        notes.append("查询失败: " + "；".join(failures))

    if not paged:
        header = f"在 {len(resolved_contexts)} 个聊天对象中未找到包含 \"{keyword}\" 的消息"
        if start_time or end_time:
            header += f"\n时间范围: {start_time or '最早'} ~ {end_time or '最新'}"
        if notes:
            header += "\n" + "\n".join(notes)
        return header

    header = (
        f"在 {len(resolved_contexts)} 个聊天对象中搜索 \"{keyword}\" 找到 {len(paged)} 条结果"
        f"（offset={offset}, limit={limit}）"
    )
    if start_time or end_time:
        header += f"\n时间范围: {start_time or '最早'} ~ {end_time or '最新'}"
    if notes:
        header += "\n" + "\n".join(notes)
    return header + ":\n\n" + "\n\n".join(item[1] for item in paged)


def _search_all_messages(keyword, start_ts, end_ts, start_time, end_time, limit, offset):
    names = get_contact_names()
    collected = []
    failures = []
    candidate_limit = _candidate_page_size(limit, offset)

    for rel_key in MSG_DB_KEYS:
        path = _cache.get(rel_key)
        if not path:
            continue

        try:
            with closing(sqlite3.connect(path)) as conn:
                contexts = _load_search_contexts_from_db(conn, path, names)
                db_entries, db_failures = _collect_search_entries(
                    conn,
                    contexts,
                    names,
                    keyword,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    candidate_limit=candidate_limit,
                )
                collected.extend(db_entries)
                failures.extend(db_failures)
        except Exception as e:
            failures.append(f"{rel_key}: {e}")

    paged = _page_search_entries(collected, limit, offset)

    if not paged:
        header = f"未找到包含 \"{keyword}\" 的消息"
        if start_time or end_time:
            header += f"\n时间范围: {start_time or '最早'} ~ {end_time or '最新'}"
        if failures:
            header += "\n查询失败: " + "；".join(failures)
        return header

    header = f"搜索 \"{keyword}\" 找到 {len(paged)} 条结果（offset={offset}, limit={limit}）"
    if start_time or end_time:
        header += f"\n时间范围: {start_time or '最早'} ~ {end_time or '最新'}"
    if failures:
        header += "\n查询失败: " + "；".join(failures)
    return header + ":\n\n" + "\n\n".join(item[1] for item in paged)


# ============ MCP Server ============

mcp = FastMCP("wechat", instructions="查询微信消息、联系人等数据")

# 新消息追踪
_last_check_state = {}  # {username: last_timestamp}


@mcp.tool()
def get_recent_sessions(limit: int = 20) -> str:
    """获取微信最近会话列表，包含最新消息摘要、未读数、时间等。
    用于了解最近有哪些人/群在聊天。

    Args:
        limit: 返回的会话数量，默认20
    """
    path = _cache.get(os.path.join("session", "session.db"))
    if not path:
        return "错误: 无法解密 session.db"

    names = get_contact_names()
    with closing(sqlite3.connect(path)) as conn:
        rows = conn.execute("""
            SELECT username, unread_count, summary, last_timestamp,
                   last_msg_type, last_msg_sender, last_sender_display_name
            FROM SessionTable
            WHERE last_timestamp > 0
            ORDER BY last_timestamp DESC
            LIMIT ?
        """, (limit,)).fetchall()

    results = []
    for r in rows:
        username, unread, summary, ts, msg_type, sender, sender_name = r
        display = names.get(username, username)
        is_group = '@chatroom' in username

        if isinstance(summary, bytes):
            try:
                summary = _zstd_dctx.decompress(summary).decode('utf-8', errors='replace')
            except Exception:
                summary = '(压缩内容)'
        if isinstance(summary, str) and ':\n' in summary:
            summary = summary.split(':\n', 1)[1]

        sender_display = ''
        if is_group and sender:
            sender_display = names.get(sender, sender_name or sender)

        time_str = datetime.fromtimestamp(ts).strftime('%m-%d %H:%M')

        entry = f"[{time_str}] {display}"
        if is_group:
            entry += " [群]"
        if unread and unread > 0:
            entry += f" ({unread}条未读)"
        entry += f"\n  {format_msg_type(msg_type)}: "
        if sender_display:
            entry += f"{sender_display}: "
        entry += str(summary or "(无内容)")

        results.append(entry)

    return f"最近 {len(results)} 个会话:\n\n" + "\n\n".join(results)


@mcp.tool()
def get_chat_history(chat_name: str, limit: int = 50, offset: int = 0, start_time: str = "", end_time: str = "", oldest_first: bool = False) -> str:
    """获取指定聊天的消息记录。

    Args:
        chat_name: 聊天对象的名字、备注名或wxid，自动模糊匹配
        limit: 返回的消息数量，默认50；支持较大的值，建议配合 offset 分页使用
        offset: 分页偏移量，默认0
        start_time: 起始时间，支持 YYYY-MM-DD / YYYY-MM-DD HH:MM / YYYY-MM-DD HH:MM:SS
        end_time: 结束时间，支持 YYYY-MM-DD / YYYY-MM-DD HH:MM / YYYY-MM-DD HH:MM:SS
        oldest_first: 为 True 时返回最早的消息（默认 False 返回最新消息）
    """
    try:
        _validate_pagination(limit, offset, limit_max=None)
        start_ts, end_ts = _parse_time_range(start_time, end_time)
    except ValueError as e:
        return f"错误: {e}"

    ctx = _resolve_chat_context(chat_name)
    if not ctx:
        return f"找不到聊天对象: {chat_name}\n提示: 可以用 get_contacts(query='{chat_name}') 搜索联系人"
    if not ctx['db_path']:
        return f"找不到 {ctx['display_name']} 的消息记录（可能在未解密的DB中或无消息）"

    names = get_contact_names()
    lines, failures = _collect_chat_history_lines(
        ctx,
        names,
        start_ts=start_ts,
        end_ts=end_ts,
        limit=limit,
        offset=offset,
        oldest_first=oldest_first,
    )

    if not lines:
        if failures:
            return "查询失败: " + "；".join(failures)
        return f"{ctx['display_name']} 无消息记录"

    header = f"{ctx['display_name']} 的消息记录（返回 {len(lines)} 条，offset={offset}, limit={limit}）"
    if ctx['is_group']:
        header += " [群聊]"
    if start_time or end_time:
        header += f"\n时间范围: {start_time or '最早'} ~ {end_time or '最新'}"
    if failures:
        header += "\n查询失败: " + "；".join(failures)
    return header + ":\n\n" + "\n".join(lines)


@mcp.tool()
def search_messages(
    keyword: str,
    chat_name: str | list[str] | None = None,
    start_time: str = "",
    end_time: str = "",
    limit: int = 20,
    offset: int = 0,
) -> str:
    """搜索消息内容，支持全库、单个聊天对象、多个聊天对象，以及时间范围和分页。

    Args:
        keyword: 搜索关键词
        chat_name: 聊天对象名称，可为空、单个字符串或字符串列表
        start_time: 起始时间，可为空
        end_time: 结束时间，可为空
        limit: 返回的结果数量，默认20，最大500
        offset: 分页偏移量，默认0
    """
    if not keyword or len(keyword) < 1:
        return "请提供搜索关键词"

    chat_names = _normalize_chat_names(chat_name)

    try:
        _validate_pagination(limit, offset)
        start_ts, end_ts = _parse_time_range(start_time, end_time)
    except ValueError as e:
        return f"错误: {e}"

    if len(chat_names) == 1:
        ctx = _resolve_chat_context(chat_names[0])
        if not ctx:
            return f"找不到聊天对象: {chat_names[0]}\n提示: 可以用 get_contacts(query='{chat_names[0]}') 搜索联系人"
        if not ctx['db_path']:
            return f"找不到 {ctx['display_name']} 的消息记录（可能在未解密的DB中或无消息）"
        return _search_single_chat(
            ctx,
            keyword,
            start_ts,
            end_ts,
            start_time,
            end_time,
            limit,
            offset,
        )

    if len(chat_names) > 1:
        return _search_multiple_chats(
            chat_names,
            keyword,
            start_ts,
            end_ts,
            start_time,
            end_time,
            limit,
            offset,
        )

    return _search_all_messages(
        keyword,
        start_ts,
        end_ts,
        start_time,
        end_time,
        limit,
        offset,
    )

@mcp.tool()
def get_contacts(query: str = "", limit: int = 50) -> str:
    """搜索或列出微信联系人。

    Args:
        query: 搜索关键词（匹配昵称、备注名、wxid），留空列出所有
        limit: 返回数量，默认50
    """
    contacts = get_contact_full()
    if not contacts:
        return "错误: 无法加载联系人数据"

    if query:
        q = query.lower()
        filtered = [
            c for c in contacts
            if q in c['nick_name'].lower()
            or q in c['remark'].lower()
            or q in c['username'].lower()
        ]
    else:
        filtered = contacts

    filtered = filtered[:limit]

    if not filtered:
        return f"未找到匹配 \"{query}\" 的联系人"

    lines = []
    for c in filtered:
        line = c['username']
        if c['remark']:
            line += f"  备注: {c['remark']}"
        if c['nick_name']:
            line += f"  昵称: {c['nick_name']}"
        lines.append(line)

    header = f"找到 {len(filtered)} 个联系人"
    if query:
        header += f"（搜索: {query}）"
    return header + ":\n\n" + "\n".join(lines)


@mcp.tool()
def get_contact_tags() -> str:
    """列出所有微信联系人标签及成员数量。"""
    tags = _load_contact_tags()
    if not tags:
        return "未找到标签数据（contact_label 表可能不存在）"

    sorted_tags = sorted(tags.values(), key=lambda t: t['sort_order'])
    total_assoc = sum(len(t['members']) for t in sorted_tags)

    lines = [f"共 {len(sorted_tags)} 个标签，{total_assoc} 个关联:\n"]
    for t in sorted_tags:
        lines.append(f"  [{t['name']}] {len(t['members'])}人")
    return "\n".join(lines)


@mcp.tool()
def get_tag_members(tag_name: str) -> str:
    """获取指定标签下的所有联系人。支持模糊匹配标签名。

    Args:
        tag_name: 标签名称，支持精确和模糊匹配
    """
    tags = _load_contact_tags()
    if not tags:
        return "未找到标签数据（contact_label 表可能不存在）"

    q = tag_name.strip().lower()

    # 精确匹配
    exact = [t for t in tags.values() if t['name'].lower() == q]
    if exact:
        matched = exact[0]
    else:
        # 模糊匹配 (contains)
        fuzzy = [t for t in tags.values() if q in t['name'].lower()]
        if not fuzzy:
            all_names = [t['name'] for t in sorted(tags.values(), key=lambda t: t['sort_order'])]
            return f"未找到匹配 \"{tag_name}\" 的标签。\n\n现有标签: {', '.join(all_names)}"
        if len(fuzzy) == 1:
            matched = fuzzy[0]
        else:
            names = [t['name'] for t in fuzzy]
            return f"找到 {len(fuzzy)} 个匹配的标签，请指定:\n" + "\n".join(f"  [{n}]" for n in names)

    members = matched['members']
    if not members:
        return f"标签 [{matched['name']}] 没有成员"

    lines = [f"标签 [{matched['name']}] 共 {len(members)} 人:\n"]
    for m in members:
        line = m['username']
        if m['display_name'] != m['username']:
            line += f"  {m['display_name']}"
        lines.append(f"  {line}")
    return "\n".join(lines)


@mcp.tool()
def get_new_messages() -> str:
    """获取自上次调用以来的新消息。首次调用返回最近的会话状态。"""
    global _last_check_state

    path = _cache.get(os.path.join("session", "session.db"))
    if not path:
        return "错误: 无法解密 session.db"

    names = get_contact_names()
    with closing(sqlite3.connect(path)) as conn:
        rows = conn.execute("""
            SELECT username, unread_count, summary, last_timestamp,
                   last_msg_type, last_msg_sender, last_sender_display_name
            FROM SessionTable
            WHERE last_timestamp > 0
            ORDER BY last_timestamp DESC
        """).fetchall()

    curr_state = {}
    for r in rows:
        username, unread, summary, ts, msg_type, sender, sender_name = r
        curr_state[username] = {
            'unread': unread, 'summary': summary, 'timestamp': ts,
            'msg_type': msg_type, 'sender': sender or '', 'sender_name': sender_name or '',
        }

    if not _last_check_state:
        _last_check_state = {u: s['timestamp'] for u, s in curr_state.items()}
        # 首次调用，返回有未读的会话
        unread_msgs = []
        for username, s in curr_state.items():
            if s['unread'] and s['unread'] > 0:
                display = names.get(username, username)
                is_group = '@chatroom' in username
                summary = s['summary']
                if isinstance(summary, bytes):
                    try:
                        summary = _zstd_dctx.decompress(summary).decode('utf-8', errors='replace')
                    except Exception:
                        summary = '(压缩内容)'
                if isinstance(summary, str) and ':\n' in summary:
                    summary = summary.split(':\n', 1)[1]
                time_str = datetime.fromtimestamp(s['timestamp']).strftime('%H:%M')
                tag = "[群]" if is_group else ""
                unread_msgs.append(f"[{time_str}] {display}{tag} ({s['unread']}条未读): {summary}")

        if unread_msgs:
            return f"当前 {len(unread_msgs)} 个未读会话:\n\n" + "\n".join(unread_msgs)
        return "当前无未读消息（已记录状态，下次调用将返回新消息）"

    # 对比上次状态
    new_msgs = []
    for username, s in curr_state.items():
        prev_ts = _last_check_state.get(username, 0)
        if s['timestamp'] > prev_ts:
            display = names.get(username, username)
            is_group = '@chatroom' in username
            summary = s['summary']
            if isinstance(summary, bytes):
                try:
                    summary = _zstd_dctx.decompress(summary).decode('utf-8', errors='replace')
                except Exception:
                    summary = '(压缩内容)'
            if isinstance(summary, str) and ':\n' in summary:
                summary = summary.split(':\n', 1)[1]

            sender_display = ''
            if is_group and s['sender']:
                sender_display = names.get(s['sender'], s['sender_name'] or s['sender'])

            time_str = datetime.fromtimestamp(s['timestamp']).strftime('%H:%M:%S')
            entry = f"[{time_str}] {display}"
            if is_group:
                entry += " [群]"
            entry += f": {format_msg_type(s['msg_type'])}"
            if sender_display:
                entry += f" ({sender_display})"
            entry += f" - {summary}"
            new_msgs.append((s['timestamp'], entry))

    _last_check_state = {u: s['timestamp'] for u, s in curr_state.items()}

    if not new_msgs:
        return "无新消息"

    new_msgs.sort(key=lambda x: x[0])
    entries = [m[1] for m in new_msgs]
    return f"{len(entries)} 条新消息:\n\n" + "\n".join(entries)


# ============ 图片解密 ============

_image_aes_key = _cfg.get("image_aes_key")  # V2 格式 AES key (从微信内存提取)
_image_xor_key = _cfg.get("image_xor_key", 0x88)
_image_resolver = ImageResolver(
    WECHAT_BASE_DIR, DECODED_IMAGE_DIR, _cache,
    aes_key=_image_aes_key, xor_key=_image_xor_key,
)


@mcp.tool()
def decode_image(chat_name: str, local_id: int) -> str:
    """解密微信聊天中的一张图片。

    先用 get_chat_history 查看消息，图片消息会显示 local_id，
    然后用此工具解密对应图片。

    Args:
        chat_name: 聊天对象的名字、备注名或wxid
        local_id: 图片消息的 local_id（从 get_chat_history 获取）
    """
    username = resolve_username(chat_name)
    if not username:
        return f"找不到聊天对象: {chat_name}"

    result = _image_resolver.decode_image(username, local_id)
    if result['success']:
        return (
            f"解密成功!\n"
            f"  文件: {result['path']}\n"
            f"  格式: {result['format']}\n"
            f"  大小: {result['size']:,} bytes\n"
            f"  MD5: {result['md5']}"
        )
    else:
        error = result['error']
        if 'md5' in result:
            error += f"\n  MD5: {result['md5']}"
        return f"解密失败: {error}"


@mcp.tool()
def decode_file_message(chat_name: str, local_id: int, create_time: int = 0) -> str:
    """获取微信聊天中外层文件消息（PDF/docx/xlsx 等）的本地副本路径。

    微信会把对方发来的文件下载到 ~/Library/.../msg/file/{YYYY-MM}/原文件名.{ext}
    （macOS）。本工具从消息记录解析出文件名/大小，在本地缓存中精确定位，
    然后返回原始路径，可直接交给 Read/PDF 工具读取。

    使用流程：先用 get_chat_history 找到 [文件] xxx.pdf (local_id=N, ts=T)，
    把 N 和 T 一起传给本工具。create_time(ts) 用于跨分片场景下唯一定位。

    Args:
        chat_name: 聊天对象的名字、备注名或wxid
        local_id: 文件消息的 local_id（从 get_chat_history 获取）
        create_time: 消息的 unix 时间戳，从 get_chat_history 输出 ts=N 部分获取。
            用于在 local_id 跨分片冲突时唯一定位；传 0 时若多个分片含同 local_id 会报歧义错误
    """
    try:
        local_id = int(local_id)
        create_time = int(create_time)
    except (TypeError, ValueError):
        return "错误: local_id 和 create_time 必须是整数"

    username = resolve_username(chat_name)
    if not username:
        return f"找不到聊天对象: {chat_name}"

    # 同一 chat 的消息可能分散在多个 message_N.db 分片中。扫所有分片收集 row，
    # 多于一条就报歧义错误（避免 silent decoding wrong message）。
    shards = _find_msg_tables_for_user(username)
    if not shards:
        return f"找不到 {chat_name} 的消息表"

    # 扫所有分片收集 row。如果调用者传了 create_time，用 (local_id, create_time)
    # 精确匹配；否则只按 local_id 收集，多匹配时报歧义并提示加 create_time。
    matches = []
    for shard in shards:
        if not _is_safe_msg_table_name(shard['table_name']):
            continue
        with closing(sqlite3.connect(shard['db_path'])) as conn:
            if create_time:
                candidate_row = conn.execute(
                    f"SELECT local_type, create_time, message_content, WCDB_CT_message_content "
                    f"FROM [{shard['table_name']}] WHERE local_id=? AND create_time=?",
                    (local_id, create_time)
                ).fetchone()
            else:
                candidate_row = conn.execute(
                    f"SELECT local_type, create_time, message_content, WCDB_CT_message_content "
                    f"FROM [{shard['table_name']}] WHERE local_id=?",
                    (local_id,)
                ).fetchone()
        if candidate_row:
            matches.append((shard['db_path'], candidate_row))
    if not matches:
        if create_time:
            return f"找不到 (local_id={local_id}, create_time={create_time}) 的消息（已扫描 {len(shards)} 个分片）"
        return f"找不到 local_id={local_id} 的消息（已扫描 {len(shards)} 个分片）"
    if len(matches) > 1:
        details = []
        for db_p, r in matches:
            ct = r[1]
            ts_str = datetime.fromtimestamp(ct).isoformat() if ct else '?'
            details.append(f"{os.path.basename(db_p)} create_time={ct} ({ts_str})")
        return (
            f"local_id={local_id} 在 {len(matches)} 个分片中都存在，无法唯一定位:\n  "
            + '\n  '.join(details)
            + f"\n请加 create_time 参数：decode_file_message(chat_name, local_id={local_id}, create_time=N)"
        )

    _, row = matches[0]
    local_type, create_time, content, ct_compress = row
    base_type, _ = _split_msg_type(local_type)
    if base_type != 49:
        return (
            f"不是文件消息（local_type={local_type}，base_type={base_type}），"
            f"文件消息应为 base_type=49 且 appmsg type=6"
        )

    xml_text = _decompress_content(content, ct_compress)
    if not xml_text:
        return "消息 content 为空或无法解码"

    # 复用项目内现有 helper 剥离群聊 sender 前缀，避免自己写启发式
    is_group = username.endswith('@chatroom')
    _, xml_text = _parse_message_content(xml_text, local_type, is_group)

    root = _parse_app_message_outer(xml_text)
    if root is None:
        return "无法解析消息 XML"

    appmsg = root.find('.//appmsg')
    if appmsg is None:
        return "消息中没有 appmsg 段（可能不是文件类型）"

    # 必须是 appmsg type=6 (文件)，否则可能是链接/小程序/合并转发等带 title 的卡片，
    # 按 title/size 全盘搜会误命中无关本地文件并伪装成"找到了"。
    app_type_in_msg = _parse_int(_collapse_text(appmsg.findtext('type') or ''), 0)
    if app_type_in_msg != 6:
        return (
            f"不是文件消息（appmsg type={app_type_in_msg}）。"
            f"文件消息要求 appmsg type=6；type=19 请用 decode_record_item，"
            f"type=5/33/36/44 等是链接/小程序，没有可下载的本地文件"
        )

    raw_title = _collapse_text(appmsg.findtext('title') or '')
    fileext = _collapse_text(appmsg.findtext('.//fileext') or '')
    totallen = _parse_int(_collapse_text(appmsg.findtext('.//totallen') or ''), 0)
    # md5 字段在 type=6 外层（不是 appattach 子节点）—— 用于强校验候选文件归属
    expected_md5 = _collapse_text(appmsg.findtext('md5') or '').lower()

    # 没有 appattach 节点 = 不是真正的文件消息（type=6 必带 appattach）
    if appmsg.find('appattach') is None:
        return "消息没有 appattach 节点（可能 schema 异常或不是真文件消息）"

    if not raw_title:
        return "消息中没有文件名 (title)"

    # title 来自不可信的 message XML，对方可能发恶意消息（含绝对路径或 ../）。
    # 必须 sanitize 成 safe basename 才能拼路径 + glob，否则有 path-traversal 风险。
    title = _safe_basename(raw_title)
    if not title:
        return f"消息中的文件名 {raw_title!r} 不安全（含绝对路径/路径分隔符/..），拒绝处理"

    # 性能优化：先按消息时间精确定位 msg/file/{YYYY-MM}/，命中即返回；
    # 否则才退回 walk 全盘 os.walk（msg/attach 含数十万小文件，全盘扫描可达数秒）
    candidates = []
    msg_file_dir = os.path.join(WECHAT_BASE_DIR, 'msg/file')
    if create_time and os.path.isdir(msg_file_dir):
        # 同名文件可能落到收到消息的当月、上一月或下一月（罕见跨月边界）
        ts_dt = datetime.fromtimestamp(create_time)
        candidate_months = {
            ts_dt.strftime('%Y-%m'),
            (ts_dt - timedelta(days=31)).strftime('%Y-%m'),
            (ts_dt + timedelta(days=31)).strftime('%Y-%m'),
        }
        escaped_stem = glob.escape(os.path.splitext(title)[0])
        ext = os.path.splitext(title)[1]
        for ym in candidate_months:
            month_dir = os.path.join(msg_file_dir, ym)
            if not os.path.isdir(month_dir):
                continue
            # 精确匹配 + 同名 (1)(2) 后缀变体
            for pattern in (
                glob.escape(title),
                f"{escaped_stem}*{glob.escape(ext)}" if ext else f"{escaped_stem}*",
            ):
                for hit in glob.glob(os.path.join(month_dir, pattern)):
                    # 有 totallen 时立刻 size 验证：避免月扫命中"同名但 size 不对"的副本
                    # 阻塞 walk 兜底，最终返回错误文件
                    if totallen:
                        try:
                            if os.path.getsize(hit) != totallen:
                                continue
                        except OSError:
                            continue
                    if hit not in candidates:
                        candidates.append(hit)

    # 退路：未命中或没 create_time 时只 walk msg/file（slow path 兜底）。
    # 文件名匹配严格化：只接受精确匹配或 wechat 自动加副本的 "(N)" 后缀变体，
    # 不做 stem 子串匹配——避免 "某某论文.pdf" 被当成 "论文.pdf"。
    if not candidates:
        d = os.path.join(WECHAT_BASE_DIR, 'msg/file')
        stem, ext = os.path.splitext(title)
        copy_pattern = re.compile(
            r'^' + re.escape(stem) + r' ?\(\d+\)' + re.escape(ext) + r'$'
        )
        if os.path.isdir(d):
            for root_dir, _, files in os.walk(d):
                for f in files:
                    if f.startswith('.'):
                        continue
                    full = os.path.join(root_dir, f)
                    is_exact = (f == title)
                    is_copy_variant = bool(copy_pattern.match(f))
                    if not (is_exact or is_copy_variant):
                        continue
                    if totallen:
                        try:
                            if os.path.getsize(full) != totallen:
                                continue
                        except OSError:
                            continue
                    candidates.append(full)

    if not candidates:
        return (
            f"在本地缓存中找不到 {title}\n"
            f"  期望路径模式: {WECHAT_BASE_DIR}/msg/file/YYYY-MM/{title}\n"
            f"  可能原因：从未在 PC/Mac 微信打开过 / 已被清理"
        )

    # 严格 size 过滤（如果 totallen 已知，不匹配的全淘汰）
    if totallen:
        candidates = [c for c in candidates if os.path.getsize(c) == totallen]
        if not candidates:
            return (
                f"在本地缓存中找不到 {title} (期望 size={totallen:,})\n"
                f"  说明：找到了同名文件但 size 都不匹配——可能从未真正下载完整 / 已被清理"
            )

    # 路径绑定策略：有 md5 → cryptographic verify；没 md5 → heuristic +
    # warning。本工具是用户主动通过 MCP 调用，path 只在本地对话显示，所以
    # 没 md5 时不强制 fail-closed。
    cache_root = os.path.join(WECHAT_BASE_DIR, 'msg')
    md5_verified = False
    if expected_md5 and len(expected_md5) == 32:
        # 用 md5 过滤候选——同 md5 = 真同一文件副本。
        md5_match = []
        md5_errors = []
        for c in candidates:
            if not _path_under_root(c, cache_root):
                md5_errors.append(f"{c}: 不在 {cache_root} 下，跳过")
                continue
            actual_md5, err = _md5_file_chunked(c)
            if err:
                md5_errors.append(f"{c}: {err}")
                continue
            if actual_md5 == expected_md5:
                md5_match.append(c)
                break  # 多候选共享同 md5 = 同一文件副本，第一个命中即停
        if not md5_match:
            info = (
                f"⚠️ 候选文件 md5 都不匹配，拒绝返回错文件:\n"
                f"  期望 md5: {expected_md5}\n"
                f"  说明：找到 {len(candidates)} 个同名同 size 的本地文件但 md5 都不对。"
                f"目标文件可能未在 wechat 客户端打开过，或已被清理。"
            )
            if md5_errors:
                info += "\n  校验异常：\n    " + "\n    ".join(md5_errors)
            return info
        candidates = md5_match
        md5_verified = True

    # 没 md5 时多 candidates 仍 fail-closed（避免 silent mtime pick）
    if len(candidates) > 1 and not md5_verified:
        details = []
        for c in candidates:
            try:
                mt = datetime.fromtimestamp(os.path.getmtime(c)).isoformat()
            except OSError:
                mt = '?'
            details.append(f"{c} (mtime={mt})")
        return (
            f"在本地缓存找到 {len(candidates)} 个匹配的副本，无法唯一定位"
            f"（同名同 size 多份，且消息 XML 没含 md5 用于强校验）:\n  "
            + '\n  '.join(details)
            + f"\n请人工 inspect mtime / 上下文区分"
        )

    chosen = candidates[0]
    if not _path_under_root(chosen, cache_root):
        return f"匹配到的路径 {chosen!r} 不在 {cache_root} 下，拒绝返回（可能是 symlink 攻击）"

    binding_note = (
        "✅ md5 校验通过，路径与消息唯一绑定"
        if md5_verified else
        f"⚠️  消息 XML 没含 md5，路径基于 (filename+size) 启发式匹配——"
        f"如果同 chat 缓存里另有同名同 size 的不相关文件，可能返回错副本，请人工验证。"
    )
    return (
        f"找到本地文件:\n"
        f"  路径: {chosen}\n"
        f"  大小: {os.path.getsize(chosen):,} bytes\n"
        f"  扩展名: {fileext or os.path.splitext(title)[1].lstrip('.') or '?'}\n"
        f"  期望大小: {totallen:,} bytes\n"
        f"  {binding_note}"
    )


@mcp.tool()
def decode_record_item(chat_name: str, local_id: int, item_index: int, create_time: int = 0) -> str:
    """获取合并转发聊天记录中某个内嵌文件/图片的本地副本路径。

    使用流程：
    1. 先用 get_chat_history 找到 [聊天记录] xxx (local_id=N, ts=T) 卡片，记下 N 和 T，
       以及展开行里 [item_index] 前缀（0-based）
    2. 用本工具拿本地路径，create_time 传 history 里的 ts 部分
    3. 如果未下载，工具会精确告诉你去 wechat 客户端点击合并卡片里的第几项触发下载

    注意：合并转发里的内嵌文件只有在用户**点击查看**后 wechat 才会下载到本地。
    没点过的 dataitem 用本工具会得到"未下载"提示。

    Args:
        chat_name: 聊天对象的名字、备注名或wxid
        local_id: 合并转发消息（带"[聊天记录]"标记）的 local_id
        item_index: dataitem 在 datalist 里的 0-based 索引（history 输出里的 [N] 前缀）
        create_time: 消息的 unix 时间戳；用于跨分片唯一定位，传 0 时多匹配会报歧义
    """
    try:
        local_id = int(local_id)
        item_index = int(item_index)
        create_time = int(create_time)
    except (TypeError, ValueError):
        return "错误: local_id / item_index / create_time 必须是整数"

    username = resolve_username(chat_name)
    if not username:
        return f"找不到聊天对象: {chat_name}"

    # 多分片扫描 + ambiguity 检测（避免 silent decoding wrong message，参考 decode_file_message）
    shards = _find_msg_tables_for_user(username)
    if not shards:
        return f"找不到 {chat_name} 的消息表"

    matches = []
    for shard in shards:
        if not _is_safe_msg_table_name(shard['table_name']):
            continue
        with closing(sqlite3.connect(shard['db_path'])) as conn:
            if create_time:
                candidate_row = conn.execute(
                    f"SELECT local_type, create_time, message_content, WCDB_CT_message_content "
                    f"FROM [{shard['table_name']}] WHERE local_id=? AND create_time=?",
                    (local_id, create_time)
                ).fetchone()
            else:
                candidate_row = conn.execute(
                    f"SELECT local_type, create_time, message_content, WCDB_CT_message_content "
                    f"FROM [{shard['table_name']}] WHERE local_id=?",
                    (local_id,)
                ).fetchone()
        if candidate_row:
            matches.append((shard['table_name'], candidate_row))
    if not matches:
        if create_time:
            return f"找不到 (local_id={local_id}, create_time={create_time}) 的消息（已扫描 {len(shards)} 个分片）"
        return f"找不到 local_id={local_id} 的消息（已扫描 {len(shards)} 个分片）"
    if len(matches) > 1:
        details = []
        for tn, r in matches:
            ts_str = datetime.fromtimestamp(r[1]).isoformat() if r[1] else '?'
            details.append(f"table={tn[:12]}... create_time={r[1]} ({ts_str})")
        return (
            f"local_id={local_id} 在 {len(matches)} 个分片中都存在，无法唯一定位:\n  "
            + '\n  '.join(details)
            + f"\n请加 create_time 参数：decode_record_item(chat_name, local_id={local_id}, item_index={item_index}, create_time=N)"
        )

    table_name, row = matches[0]
    local_type, _create_time, content, ct_compress = row
    base_type, _ = _split_msg_type(local_type)
    if base_type != 49:
        return (
            f"不是合并转发消息（local_type={local_type}, base_type={base_type}），"
            f"合并转发应为 base_type=49 + appmsg type=19"
        )

    xml_text = _decompress_content(content, ct_compress)
    if not xml_text:
        return "消息 content 为空或无法解码"

    # 复用项目内现有 helper 剥离群聊 sender 前缀，避免自己写启发式
    is_group = username.endswith('@chatroom')
    _, xml_text = _parse_message_content(xml_text, local_type, is_group)

    root = _parse_app_message_outer(xml_text)
    if root is None:
        return "无法解析消息 XML（可能不是合并转发消息）"
    appmsg = root.find('.//appmsg')
    if appmsg is None:
        return "消息中没有 appmsg 段"

    app_type = _parse_int(_collapse_text(appmsg.findtext('type') or ''), 0)
    if app_type != 19:
        return (
            f"不是合并转发消息（appmsg type={app_type}），"
            f"合并转发应为 type=19。请用 decode_file_message 处理外层独立文件"
        )

    record_node = appmsg.find('recorditem')
    if record_node is None or not record_node.text:
        return "消息中没有 recorditem（datalist 还未加载，请在 wechat 中点开此卡片让客户端拉取）"

    inner = _parse_xml_root(record_node.text, max_len=_RECORD_XML_PARSE_MAX_LEN)
    if inner is None:
        return "无法解析 recorditem 内嵌 XML"

    datalist = inner.find('datalist')
    items = list(datalist.findall('dataitem')) if datalist is not None else []
    if not items:
        return "datalist 为空（合并记录还未加载内容）"
    if item_index < 0 or item_index >= len(items):
        return f"item_index={item_index} 超出范围（共 {len(items)} 条 dataitem，0-based）"

    item = items[item_index]
    datatype = (item.get('datatype') or '').strip()
    raw_datatitle = _collapse_text(item.findtext('datatitle') or '')
    # datatitle 来自不可信 XML，sanitize 防 path traversal
    datatitle = _safe_basename(raw_datatitle) if raw_datatitle else ''
    if raw_datatitle and not datatitle:
        return f"该 dataitem 的 datatitle {raw_datatitle!r} 不安全（含绝对路径/分隔符/..），拒绝处理"
    datasize = _parse_int(_collapse_text(item.findtext('datasize') or ''), 0)
    datafmt = _collapse_text(item.findtext('datafmt') or '')
    sourcename = _collapse_text(item.findtext('sourcename') or '')
    # fullmd5 是文件内容唯一标识，用于把候选绑定到这条 record，避免误命中
    # 同 chat 内别条 record 的同名同 size 文件。
    expected_md5 = _collapse_text(item.findtext('fullmd5') or '').lower()

    type_label = _RECORD_DATATYPE_LABEL.get(datatype, f'datatype={datatype}')

    if datatype == '1':
        text_content = _collapse_text(item.findtext('datadesc') or '')
        return (
            f"该 dataitem 是文本，无需下载:\n"
            f"  发送者: {sourcename}\n"
            f"  内容: {text_content}"
        )

    # 仅以下 datatype 在 wechat 缓存里有真本地 binary（图片/语音/视频/文件）；
    # 其他类型如链接/位置/名片/小程序/视频号/嵌套聊天记录等只是 metadata，
    # 没有可下载的本地副本。不在白名单里的 datatype 直接拒绝，避免 wildcard
    # sub='*' 通配命中无关 record 的同名文件。
    subdir_map = _RECORD_BINARY_SUBDIR
    if datatype not in subdir_map:
        return (
            f"该 dataitem 类型 [{type_label}] 没有本地 binary 文件，无需下载\n"
            f"  发送者: {sourcename}\n"
            f"  标题: {datatitle or '(无)'}\n"
            f"  说明：仅 datatype=2/4/5/8（图片/语音/视频/文件）有可下载内容；"
            f"链接/位置/名片/小程序/视频号/嵌套聊天记录等是 metadata-only。"
            f"\n如果你需要这条 dataitem 的 metadata 详情，看 get_chat_history 输出里"
            f"已展开的 [{item_index}] 行内容即可。"
        )

    table_hash = table_name.replace('Msg_', '', 1)
    attach_dir = os.path.join(WECHAT_BASE_DIR, 'msg/attach', table_hash)

    candidates = []
    if os.path.isdir(attach_dir):
        import glob as glob_mod
        sub = subdir_map.get(datatype, '*')
        idx_str = str(item_index)

        # datatype=2 图片走 flat 文件命名 (Img/0_t / Img/0 / Img/0.{ext})，
        # 不像文件类的 F/{idx}/{filename}。
        if datatype == '2':
            flat_patterns = [
                f"{idx_str}_t",
                idx_str,
                f"{idx_str}.*",
                f"{idx_str}_*",
            ]
            for fp in flat_patterns:
                for hit in glob.glob(os.path.join(attach_dir, '*/Rec/*', sub, fp)):
                    if datasize:
                        try:
                            if os.path.getsize(hit) != datasize:
                                continue
                        except OSError:
                            continue
                    if hit not in candidates:
                        candidates.append(hit)

        # 文件 / 视频 / 语音类: F|V|A/{idx}/{filename}
        if datatype != '2' and datatitle:
            escaped_title = glob.escape(datatitle)
            for hit in glob.glob(os.path.join(attach_dir, '*/Rec/*', sub, idx_str, escaped_title)):
                if datasize:
                    try:
                        if os.path.getsize(hit) != datasize:
                            continue
                    except OSError:
                        continue
                if hit not in candidates:
                    candidates.append(hit)

        # size only 兜底：仅在 datatitle 缺失且非 image（image 已上面处理）时启用
        if not candidates and not datatitle and datasize and datatype != '2':
            for hit in glob.glob(os.path.join(attach_dir, '*/Rec/*', sub, idx_str, '*')):
                try:
                    if os.path.getsize(hit) == datasize:
                        candidates.append(hit)
                except OSError:
                    pass

    if not candidates:
        return (
            f"在本地缓存中找不到此 dataitem（很可能未在 wechat 客户端点击查看过）\n"
            f"  消息: {chat_name} 的 local_id={local_id}\n"
            f"  dataitem[{item_index}]: {sourcename}: [{type_label}] {datatitle or '(无标题)'}\n"
            f"  期望大小: {datasize:,} bytes\n"
            f"  期望路径模式: {attach_dir}/YYYY-MM/Rec/*/{subdir_map.get(datatype, '?')}/{item_index}/{datatitle}\n"
            f"  解决方法: 在 wechat 客户端打开此合并记录卡片，点击第 {item_index + 1} 项让客户端下载，再试"
        )

    # 注意：早 ambiguity check（在 md5 filter 之前）已经被删除——它会让有 fullmd5
    # 但多 candidates 的合理 case silent 失败。md5 filter 后再做歧义判断（见下方）。
    # 威胁模型：本工具是用户主动通过 MCP 调用 + path 只在本地显示。
    # 跟 decode_file_message 一致路线：有 md5 强校验，没 md5 fallback 到
    # heuristic + warning（实用 over 严格）。
    cache_root = os.path.join(WECHAT_BASE_DIR, 'msg')
    md5_verified = False
    if expected_md5 and len(expected_md5) == 32:
        md5_match = []
        md5_errors = []
        for c in candidates:
            if not _path_under_root(c, cache_root):
                md5_errors.append(f"{c}: 不在 {cache_root} 下，跳过")
                continue
            actual_md5, err = _md5_file_chunked(c)
            if err:
                md5_errors.append(f"{c}: {err}")
                continue
            if actual_md5 == expected_md5:
                md5_match.append(c)
                break  # 多候选共享同 md5 = 同一文件副本，第一个命中即停
        if not md5_match:
            info = (
                f"⚠️ 候选文件 md5 都不匹配，拒绝返回错文件:\n"
                f"  期望 md5: {expected_md5}\n"
                f"  说明：候选 {len(candidates)} 个，md5 都不对。"
                f"目标 dataitem 可能未在 wechat 客户端点开过，请点击第 {item_index + 1} 项触发下载。"
            )
            if md5_errors:
                info += "\n  校验异常：\n    " + "\n    ".join(md5_errors)
            return info
        candidates = md5_match
        md5_verified = True

    # 没 fullmd5 时多 candidates 仍 fail-closed
    if len(candidates) > 1 and not md5_verified:
        details = []
        for c in candidates:
            try:
                mt = datetime.fromtimestamp(os.path.getmtime(c)).isoformat()
            except OSError:
                mt = '?'
            details.append(f"{c} (mtime={mt})")
        return (
            f"找到 {len(candidates)} 个匹配的本地副本，无法唯一定位"
            f"（同位置同名同 size 多份，且 dataitem XML 没含 fullmd5 用于强校验）:\n  "
            + '\n  '.join(details)
            + f"\n请人工 inspect mtime / 上下文区分"
        )

    chosen = candidates[0]
    if not _path_under_root(chosen, cache_root):
        return f"匹配到的路径 {chosen!r} 不在 {cache_root} 下，拒绝返回（可能是 symlink 攻击）"

    binding_note = (
        "✅ md5 校验通过，路径与 dataitem 唯一绑定"
        if md5_verified else
        f"⚠️  此 dataitem XML 没含 fullmd5，路径基于 (item_index+filename+size) 启发式匹配——"
        f"如果同 chat 内多条合并卡片碰巧含同位置同名同 size 的文件，可能返回别条 record 的副本，请人工验证。"
    )
    return (
        f"找到本地文件:\n"
        f"  路径: {chosen}\n"
        f"  大小: {os.path.getsize(chosen):,} bytes\n"
        f"  期望大小: {datasize:,} bytes\n"
        f"  发送者: {sourcename}\n"
        f"  类型: [{type_label}] {datatitle or '(无标题)'}\n"
        f"  {binding_note}"
    )


@mcp.tool()
def get_chat_images(chat_name: str, limit: int = 20) -> str:
    """列出某个聊天中的图片消息。

    返回图片的时间、local_id、MD5、文件大小等信息。
    可以配合 decode_image 工具解密指定图片。

    Args:
        chat_name: 聊天对象的名字、备注名或wxid
        limit: 返回数量，默认20
    """
    username = resolve_username(chat_name)
    if not username:
        return f"找不到聊天对象: {chat_name}"

    names = get_contact_names()
    display_name = names.get(username, username)

    db_path, table_name = _find_msg_table_for_user(username)
    if not db_path:
        return f"找不到 {display_name} 的消息记录"

    images = _image_resolver.list_chat_images(db_path, table_name, username, limit)
    if not images:
        return f"{display_name} 无图片消息"

    lines = []
    for img in images:
        time_str = datetime.fromtimestamp(img['create_time']).strftime('%Y-%m-%d %H:%M')
        line = f"[{time_str}] local_id={img['local_id']}"
        if img.get('md5'):
            line += f"  MD5={img['md5']}"
        if img.get('size'):
            size_kb = img['size'] / 1024
            line += f"  {size_kb:.0f}KB"
        if not img.get('md5'):
            line += "  (无资源信息)"
        lines.append(line)

    return f"{display_name} 的 {len(lines)} 张图片:\n\n" + "\n".join(lines)


# ============ 语音解密 ============

DECODED_VOICE_DIR = os.path.join(SCRIPT_DIR, "decoded_voices")

# media DB 与 message DB 同样会分片（media_0.db、media_1.db…），
# 每个分片各有独立的 Name2Id / VoiceInfo 表。
MEDIA_DB_KEYS = sorted([
    k for k in ALL_KEYS
    if any(v.startswith("message/") for v in key_path_variants(k))
    and any(re.search(r"media_\d+\.db$", v) for v in key_path_variants(k))
])


def _iter_media_db_paths():
    for rel_key in MEDIA_DB_KEYS:
        path = _cache.get(rel_key)
        if path:
            yield path


def _get_chat_name_id(conn, username):
    row = conn.execute(
        "SELECT rowid FROM Name2Id WHERE user_name = ?", (username,)
    ).fetchone()
    return row[0] if row else None


def _fetch_voice_row(username, local_id):
    """遍历所有 media DB 分片，返回 (voice_data, create_time)；找不到返回 None。"""
    for media_db in _iter_media_db_paths():
        with closing(sqlite3.connect(media_db)) as conn:
            chat_name_id = _get_chat_name_id(conn, username)
            if chat_name_id is None:
                continue
            row = conn.execute(
                "SELECT voice_data, create_time FROM VoiceInfo "
                "WHERE chat_name_id = ? AND local_id = ?",
                (chat_name_id, local_id),
            ).fetchone()
            if row:
                return row
    return None


def _silk_to_wav(voice_data, create_time, username, local_id):
    """Decode SILK voice blob to WAV file, return output path."""
    # pypi 上有多个 SILK 相关包名（silk-python / pysilk / pilk），
    # 这里用的是 synodriver/pysilk —— 安装包名 silk-python，import 名 pysilk
    import pysilk
    data = bytes(voice_data)
    silk_data = data[1:] if data[0] == 0x02 else data
    os.makedirs(DECODED_VOICE_DIR, exist_ok=True)
    time_str = datetime.fromtimestamp(create_time).strftime('%Y%m%d_%H%M%S')
    out_path = os.path.join(DECODED_VOICE_DIR, f"{username}_{time_str}_{local_id}.wav")
    inp = io.BytesIO(silk_data)
    out = io.BytesIO()
    pysilk.decode(inp, out, 24000)
    pcm = out.getvalue()
    with wave.open(out_path, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(pcm)
    return out_path, len(pcm)


@mcp.tool()
def get_voice_messages(chat_name: str, limit: int = 20) -> str:
    """列出某个聊天中的语音消息。

    返回语音的时间、local_id 和大小，可配合 decode_voice 工具解码。

    Args:
        chat_name: 聊天对象的名字、备注名或wxid
        limit: 返回数量，默认20
    """
    username = resolve_username(chat_name)
    if not username:
        return f"找不到聊天对象: {chat_name}"

    names = get_contact_names()
    display_name = names.get(username, username)

    if not MEDIA_DB_KEYS:
        return "找不到 media DB"

    # 从每个分片各取最多 limit 条后合并再截断：分片若有时间重叠也不会漏最新消息
    rows = []
    for media_db in _iter_media_db_paths():
        with closing(sqlite3.connect(media_db)) as conn:
            chat_name_id = _get_chat_name_id(conn, username)
            if chat_name_id is None:
                continue
            rows.extend(conn.execute(
                "SELECT local_id, create_time, length(voice_data) FROM VoiceInfo "
                "WHERE chat_name_id = ? ORDER BY create_time DESC LIMIT ?",
                (chat_name_id, limit),
            ).fetchall())

    if not rows:
        return f"{display_name} 无语音消息"

    rows.sort(key=lambda r: r[1], reverse=True)
    rows = rows[:limit]

    lines = []
    for local_id, create_time, size in rows:
        time_str = datetime.fromtimestamp(create_time).strftime('%Y-%m-%d %H:%M')
        lines.append(f"[{time_str}] local_id={local_id}  {size/1024:.0f}KB")

    return f"{display_name} 的 {len(lines)} 条语音消息:\n\n" + "\n".join(lines)


@mcp.tool()
def decode_voice(chat_name: str, local_id: int) -> str:
    """解码微信语音消息为 WAV 文件。

    先用 get_voice_messages 获取 local_id，再用此工具解码。
    输出文件保存在 decoded_voices/ 目录。

    依赖: pip install silk-python (import 名为 pysilk)

    Args:
        chat_name: 聊天对象的名字、备注名或wxid
        local_id: 语音消息的 local_id（从 get_voice_messages 获取）
    """
    try:
        import pysilk  # noqa: F401
    except ImportError:
        return "缺少依赖: pip install silk-python"

    username = resolve_username(chat_name)
    if not username:
        return f"找不到聊天对象: {chat_name}"

    row = _fetch_voice_row(username, local_id)
    if row is None:
        return f"找不到 local_id={local_id} 的语音消息"

    voice_data, create_time = row
    out_path, pcm_len = _silk_to_wav(voice_data, create_time, username, local_id)
    duration_s = pcm_len / (24000 * 2)
    return (
        f"解码成功!\n"
        f"  文件: {out_path}\n"
        f"  时长: {duration_s:.1f}秒\n"
        f"  大小: {os.path.getsize(out_path):,} bytes"
    )


# ============ 语音转录缓存 ============
#
# Whisper 转录耗时（CPU 下每条数秒到数十秒），且结果是确定性的
# （同一段 voice_data → 同一段 text），非常适合缓存。
#
# 缓存 key 用 json.dumps([username, local_id])：local_id 在单个 username 下
# 稳定唯一，套一层 JSON 序列化保证 username 里若含分隔符也不会与其它条目碰撞。
#
# 写入走 temp + os.replace 原子替换，避免进程中途被杀导致整份缓存损坏
# （Whisper 的单次代价远高于 DBCache，破档不可接受）。
#
# 条目里记录 model_size：Whisper 升级默认模型后，旧条目自动视为失效并重跑。

VOICE_TRANSCRIPTION_CACHE_FILE = os.path.join(SCRIPT_DIR, "voice_transcriptions.json")

_voice_transcription_cache = None  # 懒加载 dict；None 表示尚未加载
_voice_transcription_cache_lock = threading.Lock()
_voice_transcription_save_warned = False  # 写失败仅首次写 stderr，避免刷屏


def _voice_transcription_cache_key(username, local_id):
    """构造缓存 key。用 json.dumps 兜底 username 里可能出现的分隔符。"""
    return json.dumps([username, int(local_id)], ensure_ascii=False)


def _load_voice_transcription_cache():
    """加载缓存到模块级 dict，返回该 dict。

    文件不存在 → 空 dict。JSON 损坏或 payload 非 dict → 空 dict
    （与上游 DBCache 的容错风格一致：缓存坏了不要拖垮工具调用）。
    """
    global _voice_transcription_cache
    with _voice_transcription_cache_lock:
        if _voice_transcription_cache is not None:
            return _voice_transcription_cache
        if not os.path.exists(VOICE_TRANSCRIPTION_CACHE_FILE):
            _voice_transcription_cache = {}
            return _voice_transcription_cache
        try:
            with open(VOICE_TRANSCRIPTION_CACHE_FILE, encoding="utf-8") as f:
                loaded = json.load(f)
            _voice_transcription_cache = loaded if isinstance(loaded, dict) else {}
        except (json.JSONDecodeError, OSError):
            _voice_transcription_cache = {}
        return _voice_transcription_cache


def _save_voice_transcription_cache():
    """持久化缓存到磁盘。

    - 原子写：先写 .tmp 再 os.replace，避免 crash 中途留下半截文件。
    - 未加载过也允许保存：此时把 module 状态初始化为空 dict，避免上层
      代码因调用顺序错误而静默丢数据。
    - OSError 不抛：避免转录成功但落盘失败时让工具调用也失败；但首次
      失败会在 stderr 打一行警告，用户知道磁盘满 / 权限问题需要处理。
    """
    global _voice_transcription_cache, _voice_transcription_save_warned
    with _voice_transcription_cache_lock:
        if _voice_transcription_cache is None:
            _voice_transcription_cache = {}
        tmp_path = VOICE_TRANSCRIPTION_CACHE_FILE + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(_voice_transcription_cache, f, ensure_ascii=False)
            os.replace(tmp_path, VOICE_TRANSCRIPTION_CACHE_FILE)
        except OSError as exc:
            if not _voice_transcription_save_warned:
                print(
                    f"[voice_cache] 写入失败（后续不再提示）: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                _voice_transcription_save_warned = True
            # 清理可能残留的 .tmp
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass


# ============ 语音转录后端 ============
#
# 默认 local: 完全保留原有行为，CPU 上跑本地 Whisper。
# opt-in openai: 需要 transcription_backend="openai" 且 openai_api_key 都齐
# 才会上云；任一缺失静默回退 local + stderr 一行警告（用户感知到误配置但不阻塞）。
# 详见 README "语音转录隐私" 章节。

TRANSCRIPTION_BACKEND = _cfg.get("transcription_backend", "local")
LOCAL_WHISPER_MODEL = _cfg.get("local_whisper_model", "base")
OPENAI_API_KEY = _cfg.get("openai_api_key", "")

OPENAI_WHISPER_MODEL = "whisper-1"           # OpenAI 当前唯一型号
OPENAI_AUDIO_LIMIT_BYTES = 25 * 1024 * 1024  # OpenAI 25MB 上限

_whisper_model = None
_openai_client = None
_openai_warning_emitted = False
_fallback_warning_emitted = False

# whisper.cpp 后端（macOS Metal GPU 加速）
# 路径选项均为可选，默认自动检测
WHISPER_CPP_BINARY = _cfg.get("whisper_cpp_binary", "")
WHISPER_CPP_MODEL = _cfg.get("whisper_cpp_model", "")
WHISPER_CPP_LANGUAGE = _cfg.get("whisper_cpp_language", "zh")
WHISPER_CPP_THREADS = _cfg.get("whisper_cpp_threads", 0)

_WHISPER_CPP_BINARY_SEARCH_PATHS = [
    "/opt/homebrew/bin/whisper-cpp",
    "/usr/local/bin/whisper-cpp",
    os.path.expanduser("~/.local/bin/whisper-cpp"),
]

_WHISPER_CPP_MODEL_SEARCH_PATHS = [
    os.path.expanduser("~/Library/Application Support/whisper-cpp"),
    os.path.expanduser("~/Library/Application Support/Recordly/whisper"),
    os.path.expanduser("~/whisper-models"),
    os.path.expanduser("~/models"),
    os.path.expanduser("~/Downloads"),
    "/opt/homebrew/share/whisper-cpp/models",
    "/usr/local/share/whisper-cpp/models",
]

_whisper_cpp_binary_resolved = None   # None=未检测, ""=未找到, str=路径
_whisper_cpp_model_resolved = None    # 同上


def _resolve_whisper_cpp_binary():
    global _whisper_cpp_binary_resolved
    if _whisper_cpp_binary_resolved is not None:
        return _whisper_cpp_binary_resolved
    if WHISPER_CPP_BINARY:
        if os.path.isfile(WHISPER_CPP_BINARY) and os.access(WHISPER_CPP_BINARY, os.X_OK):
            _whisper_cpp_binary_resolved = WHISPER_CPP_BINARY
            return _whisper_cpp_binary_resolved
    for p in _WHISPER_CPP_BINARY_SEARCH_PATHS:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            _whisper_cpp_binary_resolved = p
            return _whisper_cpp_binary_resolved
    _whisper_cpp_binary_resolved = ""
    return ""


def _resolve_whisper_cpp_model():
    global _whisper_cpp_model_resolved
    if _whisper_cpp_model_resolved is not None:
        return _whisper_cpp_model_resolved
    if WHISPER_CPP_MODEL:
        if os.path.isfile(WHISPER_CPP_MODEL):
            _whisper_cpp_model_resolved = WHISPER_CPP_MODEL
            return _whisper_cpp_model_resolved
    for search_dir in _WHISPER_CPP_MODEL_SEARCH_PATHS:
        if not os.path.isdir(search_dir):
            continue
        for f in sorted(os.listdir(search_dir)):
            if f.startswith("ggml-") and f.endswith(".bin"):
                _whisper_cpp_model_resolved = os.path.join(search_dir, f)
                return _whisper_cpp_model_resolved
    _whisper_cpp_model_resolved = ""
    return ""


def _resolve_active_backend():
    """两因素 opt-in：openai 需要 flag + key 都齐才生效。
    whisper_cpp 需要 binary 可检测到，否则回退 local。"""
    global _fallback_warning_emitted
    if TRANSCRIPTION_BACKEND == "openai":
        if not OPENAI_API_KEY:
            if not _fallback_warning_emitted:
                print(
                    "[whisper] transcription_backend=openai 但未配置 openai_api_key，"
                    "回退到本地模型",
                    file=sys.stderr, flush=True,
                )
                _fallback_warning_emitted = True
            return "local"
        return "openai"
    if TRANSCRIPTION_BACKEND == "whisper_cpp":
        if not _resolve_whisper_cpp_binary():
            if not _fallback_warning_emitted:
                print(
                    "[whisper] transcription_backend=whisper_cpp 但未找到 "
                    "whisper-cpp 二进制文件，回退到本地模型。"
                    "安装: brew install whisper-cpp",
                    file=sys.stderr, flush=True,
                )
                _fallback_warning_emitted = True
            return "local"
        return "whisper_cpp"
    return "local"


def _cache_signature():
    """当前生效后端 + 模型，用作缓存命中判定 + 落盘字段。"""
    backend = _resolve_active_backend()
    if backend == "openai":
        return {"backend": "openai", "model_size": OPENAI_WHISPER_MODEL}
    if backend == "whisper_cpp":
        model_path = _resolve_whisper_cpp_model()
        model_name = os.path.basename(model_path) if model_path else "unknown"
        return {"backend": "whisper_cpp", "model_size": model_name}
    return {"backend": "local", "model_size": LOCAL_WHISPER_MODEL}


def _get_whisper_model(model_size=None):
    global _whisper_model
    if model_size is None:
        model_size = LOCAL_WHISPER_MODEL
    if _whisper_model is None:
        import whisper
        _whisper_model = whisper.load_model(model_size)
    return _whisper_model


def _transcribe_local(wav_path):
    model = _get_whisper_model()
    result = model.transcribe(wav_path)
    return {
        "language": result.get("language", "unknown"),
        "text": result.get("text", "").strip(),
    }


def _transcribe_openai(wav_path):
    """通过 OpenAI Whisper API 转录。失败抛 RuntimeError，调用方负责面向用户的提示。"""
    global _openai_client, _openai_warning_emitted

    # 尺寸预检：放在 SDK 导入和实例化之前，确保超限文件绝不上传
    size = os.path.getsize(wav_path)
    if size > OPENAI_AUDIO_LIMIT_BYTES:
        raise RuntimeError(
            f"音频 {size / 1024 / 1024:.1f}MB 超过 OpenAI 25MB 上限，"
            "提前拒绝以避免无谓上传"
        )

    try:
        from openai import OpenAI
        from openai import AuthenticationError, RateLimitError, APIError
    except ImportError:
        raise RuntimeError("缺少依赖: pip install openai")

    if not _openai_warning_emitted:
        print(
            "[whisper] 已启用 OpenAI Whisper API，"
            "语音将上传至 OpenAI 服务器进行转录",
            file=sys.stderr, flush=True,
        )
        _openai_warning_emitted = True

    if _openai_client is None:
        _openai_client = OpenAI(api_key=OPENAI_API_KEY)

    try:
        with open(wav_path, "rb") as f:
            result = _openai_client.audio.transcriptions.create(
                model=OPENAI_WHISPER_MODEL,
                file=f,
                response_format="verbose_json",
            )
    except AuthenticationError:
        raise RuntimeError("OpenAI 鉴权失败 (401)：检查 openai_api_key")
    except RateLimitError:
        raise RuntimeError("OpenAI 限流 (429)：稍后重试")
    except APIError as e:
        raise RuntimeError(f"OpenAI API 错误: {e}")

    return {
        "language": getattr(result, "language", "unknown"),
        "text": (getattr(result, "text", "") or "").strip(),
    }


def _transcribe_whisper_cpp(wav_path):
    """通过 whisper-cpp CLI（Metal GPU 加速）转录。失败抛 RuntimeError。"""
    binary = _resolve_whisper_cpp_binary()
    if not binary:
        raise RuntimeError("whisper-cpp binary 未找到。安装: brew install whisper-cpp")
    model = _resolve_whisper_cpp_model()
    if not model:
        raise RuntimeError(
            "whisper.cpp 模型未找到。通过 config.json whisper_cpp_model 指定路径，"
            "或下载: https://huggingface.co/ggerganov/whisper.cpp"
        )

    threads = WHISPER_CPP_THREADS
    if not threads:
        try:
            threads = min(os.cpu_count() or 4, 8)
        except Exception:
            threads = 4

    try:
        cmd = [
            binary,
            "-m", model,
            "-f", wav_path,
            "-l", WHISPER_CPP_LANGUAGE,
            "-t", str(threads),
            "--no-fallback",
            "-otxt",
        ]
        subprocess.run(cmd, capture_output=True, text=True, timeout=120)

        txt_path = f"{wav_path}.txt"
        if os.path.isfile(txt_path):
            with open(txt_path, encoding="utf-8") as f:
                text = f.read().strip()
            os.unlink(txt_path)
            return {"language": WHISPER_CPP_LANGUAGE, "text": text or ""}
        return {"language": WHISPER_CPP_LANGUAGE, "text": ""}
    except subprocess.TimeoutExpired:
        raise RuntimeError("whisper-cpp 超时 (120s)")
    except Exception as e:
        raise RuntimeError(f"whisper-cpp 转录失败: {e}")


def _transcribe(wav_path, backend):
    if backend == "openai":
        return _transcribe_openai(wav_path)
    if backend == "whisper_cpp":
        return _transcribe_whisper_cpp(wav_path)
    return _transcribe_local(wav_path)


@mcp.tool()
def transcribe_voice(chat_name: str, local_id: int) -> str:
    """将微信语音消息转录为文字（自动检测语言，保留原语言）。

    首次转录会先解码 SILK 语音为 WAV，再用 Whisper 转录；结果缓存到
    voice_transcriptions.json，重复调用直接返回缓存（跳过 SILK 解码
    和 Whisper 推理）。后端切换或本地模型升级（如 base → small）后，
    旧条目自动视为失效并重新转录。首次运行本地模型会下载约 145MB 权重。

    后端由 config.json 中 transcription_backend 字段控制（local/openai/whisper_cpp）。
    详见 README "语音转录隐私" 章节。

    依赖:
      - 本地后端: pip install silk-python openai-whisper
        (silk-python 的 import 名为 pysilk)
      - OpenAI 后端: pip install silk-python openai
      - whisper_cpp 后端: brew install whisper-cpp (macOS)

    Args:
        chat_name: 聊天对象的名字、备注名或wxid
        local_id: 语音消息的 local_id（从 get_voice_messages 获取）
    """
    username = resolve_username(chat_name)
    if not username:
        return f"找不到聊天对象: {chat_name}"

    sig = _cache_signature()
    cache_key = _voice_transcription_cache_key(username, local_id)
    cache = _load_voice_transcription_cache()
    entry = cache.get(cache_key)
    # 命中要求 backend + model_size 都匹配。
    # 旧条目 (PR #58 schema) 缺 backend 字段，回填默认 "local" 保持向前兼容
    # —— 那时唯一存在的后端就是 local，语义上等价。
    if (
        isinstance(entry, dict)
        and "text" in entry
        and entry.get("backend", "local") == sig["backend"]
        and entry.get("model_size") == sig["model_size"]
    ):
        # 命中缓存：跳过 DB 查询、SILK 解码、转录。
        # 条目里存了 create_time，即使源 DB 中消息已被清理仍能返回历史转录。
        lang = entry.get("language", "unknown")
        cached_ts = entry.get("create_time")
        if isinstance(cached_ts, int):
            time_label = datetime.fromtimestamp(cached_ts).strftime('%Y-%m-%d %H:%M')
        else:
            time_label = "-"
        return f"[{time_label}] ({lang})\n{entry['text']}"

    # 未命中：本地后端才需要 whisper 包，云后端在 _transcribe_openai 内单独检查
    if sig["backend"] == "local":
        try:
            import whisper  # noqa: F401
        except ImportError:
            return "缺少依赖: pip install openai-whisper"
    # SILK 解码两条路径都需要
    try:
        import pysilk  # noqa: F401
    except ImportError:
        return "缺少依赖: pip install silk-python"

    row = _fetch_voice_row(username, local_id)
    if row is None:
        return f"找不到 local_id={local_id} 的语音消息"

    voice_data, create_time = row
    wav_path, _ = _silk_to_wav(voice_data, create_time, username, local_id)

    try:
        result = _transcribe(wav_path, sig["backend"])
    except RuntimeError as e:
        return str(e)
    text = result["text"]
    lang = result["language"]

    # 写缓存：即使 text 为空也缓存（Whisper 偶尔对静音/极短片段返回空），
    # 配合 backend + model_size 字段，切换后端或升级模型后会自动重转。
    cache[cache_key] = {
        "text": text,
        "language": lang,
        "create_time": int(create_time),
        "backend": sig["backend"],
        "model_size": sig["model_size"],
    }
    _save_voice_transcription_cache()

    time_label = datetime.fromtimestamp(create_time).strftime('%Y-%m-%d %H:%M')
    return f"[{time_label}] ({lang})\n{text}"


if __name__ == "__main__":
    mcp.run()
