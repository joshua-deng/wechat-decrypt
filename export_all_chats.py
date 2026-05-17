#!/usr/bin/env python3
"""批量导出所有微信聊天记录为 JSON 文件，可选附带语音转录。

此脚本将导出所有会话的聊天记录，输出格式与 export_chat.py 完全一致。
支持导出到指定目录，默认输出到 ./exported_chats 目录。

语音转录通过 mcp_server 的 backend 配置驱动（config.json 中设置
transcription_backend 为 whisper_cpp / openai / local）。未启用 backend
或缺少依赖时仅导出文本消息，不报错。

用法:
    python3 export_all_chats.py                         # 全量导出所有会话
    python3 export_all_chats.py --list-chats            # 只列出会话清单
    python3 export_all_chats.py --select                # 交互选择会话后导出
    python3 export_all_chats.py --chats "1,3-5,张三"     # 按编号/范围/关键词导出
    python3 export_all_chats.py --write-plan-csv export_plan.csv
    python3 export_all_chats.py output_dir --from-plan-csv export_plan.csv
    python3 export_all_chats.py --with-transcriptions   # 全量导出 + 转录语音
    python3 export_all_chats.py -i                      # 增量（只导出最新消息）
    python3 export_all_chats.py --start 2025-01-01      # 按日期范围
    python3 export_all_chats.py --end 2025-01-31
    python3 export_all_chats.py --start 2025-01-01 --end 2025-01-31 -t
"""

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime

import mcp_server

# 尝试导入 tqdm 作为进度条（可选）
try:
    from tqdm import tqdm as _tqdm
except ImportError:
    _tqdm = None

from chat_export_helpers import _extract_content, _msg_type_str, _resolve_sender


PLAN_CSV_FIELDS = [
    "export",
    "index",
    "username",
    "chat_name",
    "chat_type",
    "message_count",
    "first_time",
    "last_time",
    "attachment_estimated_bytes",
    "attachment_scanned_bytes",
    "total_estimated_bytes",
    "size_status",
]

EXPORT_INDEX_FILE = "_export_index.json"
EXPORT_INDEX_VERSION = 1
PLAN_MODE_BLACKLIST = "blacklist"
PLAN_MODE_WHITELIST = "whitelist"
_UNSAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|]')


def _parse_timestamp(ts_str):
    """解析时间字符串返回 unix timestamp。
    支持格式: '2025-01-01', '2025-01-01 14:30', '2025-01-01T14:30:00'
    """
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(ts_str.strip(), fmt)
            return int(dt.timestamp())
        except ValueError:
            pass
    try:
        return int(ts_str)
    except ValueError:
        return None


def _get_last_message_ts(json_path):
    """读取已有 JSON 的最后一条消息时间戳"""
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
        msgs = data.get("messages", [])
        if msgs:
            return msgs[-1].get("timestamp", 0)
    except (json.JSONDecodeError, IOError, KeyError):
        pass
    return 0


def _get_existing_messages(json_path):
    """读取已有 JSON 的消息列表（增量合并用）"""
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("messages", [])
    except (json.JSONDecodeError, IOError, KeyError):
        return []


def _export_index_path(output_dir):
    return os.path.join(output_dir, EXPORT_INDEX_FILE)


def _empty_export_index():
    return {"version": EXPORT_INDEX_VERSION, "chats": {}}


def _safe_export_filename_part(value):
    cleaned = _UNSAFE_FILENAME_RE.sub("_", str(value or "")).strip()
    return cleaned or "unknown"


def _export_filename(display_name, is_group, username=None):
    prefix = "group" if is_group else "single"
    label = display_name or username or "unknown"
    return f"{_safe_export_filename_part(f'{prefix}_{label}')}.json"


def _collision_export_filename(filename, username, suffix=None):
    stem, ext = os.path.splitext(filename)
    user_part = _safe_export_filename_part(username)
    extra = f"__{suffix}" if suffix else ""
    return f"{stem}__{user_part}{extra}{ext or '.json'}"


def _safe_index_filename(filename):
    filename = str(filename or "")
    if not filename:
        return ""
    if filename != os.path.basename(filename):
        return ""
    if filename == EXPORT_INDEX_FILE:
        return ""
    return filename


def _read_json_string_field(prefix, field):
    pattern = rf'"{re.escape(field)}"\s*:\s*("(?:(?:\\.)|[^"\\])*")'
    match = re.search(pattern, prefix)
    if not match:
        return ""
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return ""
    return value if isinstance(value, str) else ""


def _read_export_file_identity(path):
    """只读取 JSON 文件头部，避免为建索引加载巨大 messages 数组。"""
    try:
        with open(path, encoding="utf-8") as f:
            prefix = f.read(256 * 1024)
    except OSError:
        return {}

    username = _read_json_string_field(prefix, "username")
    if not username:
        return {}
    return {
        "username": username,
        "chat": _read_json_string_field(prefix, "chat"),
        "is_group": bool(re.search(r'"is_group"\s*:\s*true', prefix)),
        "exported_at": _read_json_string_field(prefix, "exported_at"),
        "date_first_msg": _read_json_string_field(prefix, "date_first_msg"),
        "date_last_msg": _read_json_string_field(prefix, "date_last_msg"),
    }


def _file_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return -1


def _index_entry_from_identity(filename, identity):
    return {
        "username": identity["username"],
        "is_group": bool(identity.get("is_group")),
        "current_chat_name": identity.get("chat", ""),
        "current_file": filename,
        "previous_files": [],
        "last_exported_at": identity.get("exported_at", ""),
        "date_first_msg": identity.get("date_first_msg", ""),
        "date_last_msg": identity.get("date_last_msg", ""),
    }


def _bootstrap_export_index(output_dir):
    index = _empty_export_index()
    if not os.path.isdir(output_dir):
        return index

    for filename in os.listdir(output_dir):
        safe_filename = _safe_index_filename(filename)
        if not safe_filename or not safe_filename.lower().endswith(".json"):
            continue
        path = os.path.join(output_dir, safe_filename)
        if not os.path.isfile(path):
            continue
        identity = _read_export_file_identity(path)
        username = identity.get("username")
        if not username:
            continue

        chats = index["chats"]
        entry = chats.get(username)
        if entry is None:
            chats[username] = _index_entry_from_identity(safe_filename, identity)
            continue

        previous = set(entry.get("previous_files") or [])
        current_file = entry.get("current_file")
        current_path = os.path.join(output_dir, current_file)
        if _file_mtime(path) >= _file_mtime(current_path):
            if current_file:
                previous.add(current_file)
            entry.update(_index_entry_from_identity(safe_filename, identity))
        else:
            previous.add(safe_filename)
        entry["previous_files"] = sorted(
            f for f in previous
            if _safe_index_filename(f) and f != entry.get("current_file")
        )

    return index


def _normalize_export_index(data):
    if not isinstance(data, dict) or not isinstance(data.get("chats"), dict):
        return None

    index = _empty_export_index()
    for username, entry in data["chats"].items():
        if not username or not isinstance(entry, dict):
            continue
        current_file = _safe_index_filename(entry.get("current_file"))
        if not current_file:
            continue
        previous = []
        for item in entry.get("previous_files") or []:
            filename = _safe_index_filename(item)
            if filename and filename != current_file and filename not in previous:
                previous.append(filename)
        index["chats"][str(username)] = {
            "username": str(username),
            "is_group": bool(entry.get("is_group")),
            "current_chat_name": entry.get("current_chat_name", ""),
            "current_file": current_file,
            "previous_files": previous,
            "last_exported_at": entry.get("last_exported_at", ""),
            "date_first_msg": entry.get("date_first_msg", ""),
            "date_last_msg": entry.get("date_last_msg", ""),
        }
    return index


def _load_export_index(output_dir):
    path = _export_index_path(output_dir)
    if not os.path.isfile(path):
        return _bootstrap_export_index(output_dir)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return _bootstrap_export_index(output_dir)
    return _normalize_export_index(data) or _bootstrap_export_index(output_dir)


def _write_export_index(output_dir, index):
    os.makedirs(output_dir or ".", exist_ok=True)
    path = _export_index_path(output_dir)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _choose_export_filename(output_dir, desired_filename, username):
    desired_path = os.path.join(output_dir, desired_filename)
    if not os.path.exists(desired_path):
        return desired_filename
    if _read_export_file_identity(desired_path).get("username") == username:
        return desired_filename

    for counter in range(1, 1000):
        suffix = None if counter == 1 else str(counter)
        candidate = _collision_export_filename(desired_filename, username, suffix)
        candidate_path = os.path.join(output_dir, candidate)
        if not os.path.exists(candidate_path):
            return candidate
        if _read_export_file_identity(candidate_path).get("username") == username:
            return candidate
    raise RuntimeError(f"无法为 {username} 生成不冲突的导出文件名")


def _resolve_indexed_export_path(output_dir, username, display_name, is_group):
    os.makedirs(output_dir or ".", exist_ok=True)
    index = _load_export_index(output_dir)
    chats = index.setdefault("chats", {})
    desired_filename = _export_filename(display_name, is_group, username)
    entry = chats.get(username)
    previous = set(entry.get("previous_files") or []) if entry else set()

    current_file = _safe_index_filename(entry.get("current_file")) if entry else ""
    if current_file:
        current_path = os.path.join(output_dir, current_file)
        if os.path.isfile(current_path):
            target_file = _choose_export_filename(
                output_dir, desired_filename, username
            )
            if target_file != current_file:
                target_path = os.path.join(output_dir, target_file)
                if not os.path.exists(target_path):
                    os.replace(current_path, target_path)
                    previous.add(current_file)
                elif _read_export_file_identity(target_path).get("username") == username:
                    previous.add(current_file)
                current_file = target_file
        else:
            current_file = ""

    if not current_file:
        current_file = _choose_export_filename(output_dir, desired_filename, username)

    previous = {
        f for f in previous
        if _safe_index_filename(f) and f != current_file
    }
    chats[username] = {
        "username": username,
        "is_group": bool(is_group),
        "current_chat_name": display_name,
        "current_file": current_file,
        "previous_files": sorted(previous),
        "last_exported_at": (entry or {}).get("last_exported_at", ""),
        "date_first_msg": (entry or {}).get("date_first_msg", ""),
        "date_last_msg": (entry or {}).get("date_last_msg", ""),
    }
    return os.path.join(output_dir, current_file), index


def _update_export_index(output_dir, index, username, display_name, is_group,
                         out_path, output):
    filename = os.path.basename(out_path)
    chats = index.setdefault("chats", {})
    entry = chats.get(username, {})
    previous = []
    for item in entry.get("previous_files") or []:
        safe = _safe_index_filename(item)
        if safe and safe != filename and safe not in previous:
            previous.append(safe)

    chats[username] = {
        "username": username,
        "is_group": bool(is_group),
        "current_chat_name": display_name,
        "current_file": filename,
        "previous_files": previous,
        "last_exported_at": output.get("exported_at", ""),
        "date_first_msg": output.get("date_first_msg", ""),
        "date_last_msg": output.get("date_last_msg", ""),
    }
    _write_export_index(output_dir, index)


def _load_session_usernames(session_db):
    """读取 SessionTable 中的会话 username，保持数据库原始顺序。"""
    with closing(sqlite3.connect(session_db)) as conn:
        return [
            u for u, _ in conn.execute(
                "SELECT username, type FROM SessionTable"
            )
        ]


def _build_chat_rows(sessions, names, contact_full=None):
    """构建可展示、可选择的会话行。"""
    contact_meta = {
        item.get("username"): item
        for item in (contact_full or [])
        if item.get("username")
    }
    rows = []
    for index, username in enumerate(sessions, 1):
        display_name = names.get(username, username)
        kind = "group" if str(username).endswith("@chatroom") else "single"
        meta = contact_meta.get(username, {})
        rows.append({
            "index": index,
            "username": username,
            "display_name": display_name,
            "kind": kind,
            "remark": meta.get("remark", ""),
            "nick_name": meta.get("nick_name", ""),
        })
    return rows


def _print_chat_rows(rows):
    for row in rows:
        print(
            f"[{row['index']}] {row['kind']:<6} "
            f"{row['display_name']} ({row['username']})"
        )


def _select_chat_usernames(rows, selection):
    """按编号、编号范围或显示名/username 模糊匹配选择会话。"""
    if isinstance(selection, (list, tuple)):
        selection = ",".join(str(part) for part in selection)
    tokens = [part.strip() for part in str(selection).split(",") if part.strip()]
    if not tokens:
        return []

    by_index = {row["index"]: row for row in rows}
    selected = []
    seen = set()

    def add(row):
        username = row["username"]
        if username not in seen:
            selected.append(username)
            seen.add(username)

    for token in tokens:
        range_match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", token)
        if range_match:
            start, end = (int(range_match.group(1)), int(range_match.group(2)))
            if start > end:
                raise ValueError(f"无效范围: {token}")
            missing = [idx for idx in range(start, end + 1) if idx not in by_index]
            if missing:
                raise ValueError(f"会话编号不存在: {missing[0]}")
            for idx in range(start, end + 1):
                add(by_index[idx])
            continue

        if token.isdigit():
            index = int(token)
            if index not in by_index:
                raise ValueError(f"会话编号不存在: {index}")
            add(by_index[index])
            continue

        needle = token.casefold()
        matches = [
            row for row in rows
            if needle in str(row["display_name"]).casefold()
            or needle in str(row["username"]).casefold()
        ]
        if not matches:
            raise ValueError(f"没有匹配的会话: {token}")
        for row in matches:
            add(row)

    return selected


def _format_plan_time(ts):
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")


def _date_from_message_ts(ts):
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")


def _contact_metadata_for_export(username, is_group=False):
    if is_group:
        return {}
    contact = {}
    for item in mcp_server.get_contact_full():
        if item.get("username") == username:
            contact = item
            break
    try:
        tag_map = mcp_server.get_contact_tag_names_by_username()
    except Exception:
        tag_map = {}
    return {
        "contact_remark": contact.get("remark", ""),
        "contact_nick_name": contact.get("nick_name", ""),
        "contact_phone": contact.get("phone", ""),
        "contact_tags": tag_map.get(username, []),
        "contact_memo": contact.get("description", ""),
    }


def _where_for_time_range(start_ts=None, end_ts=None, column="create_time"):
    clauses = []
    params = []
    if start_ts is not None:
        clauses.append(f"{column} >= ?")
        params.append(start_ts)
    if end_ts is not None:
        clauses.append(f"{column} <= ?")
        params.append(end_ts)
    where_sql = "WHERE " + " AND ".join(clauses) if clauses else ""
    return where_sql, params


def _query_message_table_plan_stats(db_path, table_name, start_ts=None, end_ts=None):
    if not mcp_server._is_safe_msg_table_name(table_name):
        raise ValueError(f"非法消息表名: {table_name}")
    where_sql, params = _where_for_time_range(start_ts, end_ts)
    sql = f"""
        SELECT COUNT(*), MIN(create_time), MAX(create_time),
               COALESCE(SUM(
                   COALESCE(length(message_content), 0)
                   + COALESCE(length(compress_content), 0)
                   + COALESCE(length(packed_info_data), 0)
               ), 0)
        FROM [{table_name}]
        {where_sql}
    """
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute(sql, params).fetchone()


def _get_message_resource_db_path():
    try:
        path = mcp_server._cache.get("message/message_resource.db")
    except Exception:
        path = None
    candidates = [
        path,
        os.path.join(mcp_server.DECRYPTED_DIR, "message", "message_resource.db"),
        os.path.join(
            mcp_server.DECRYPTED_DIR,
            "_monitor_cache",
            "message_message_resource.db",
        ),
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def _query_resource_estimated_bytes(username, start_ts=None, end_ts=None):
    resource_db = _get_message_resource_db_path()
    if not resource_db:
        return 0, "resource_missing"
    with closing(sqlite3.connect(resource_db)) as conn:
        chat_row = conn.execute(
            "SELECT rowid FROM ChatName2Id WHERE user_name = ?",
            (username,),
        ).fetchone()
        if not chat_row:
            return 0, None
        clauses = ["i.chat_id = ?"]
        params = [chat_row[0]]
        if start_ts is not None:
            clauses.append("i.message_create_time >= ?")
            params.append(start_ts)
        if end_ts is not None:
            clauses.append("i.message_create_time <= ?")
            params.append(end_ts)
        where_sql = " AND ".join(clauses)
        row = conn.execute(f"""
            SELECT COALESCE(SUM(COALESCE(d.size, 0)), 0)
            FROM MessageResourceInfo i
            LEFT JOIN MessageResourceDetail d ON d.message_id = i.message_id
            WHERE {where_sql}
        """, params).fetchone()
    return int(row[0] or 0), None


def _query_voice_estimated_bytes(username, start_ts=None, end_ts=None):
    try:
        media_paths = list(mcp_server._iter_media_db_paths())
    except Exception:
        return 0, "media_error"
    if not media_paths:
        return 0, "media_missing"

    total = 0
    for media_db in media_paths:
        try:
            with closing(sqlite3.connect(media_db)) as conn:
                chat_name_id = mcp_server._get_chat_name_id(conn, username)
                if chat_name_id is None:
                    continue
                clauses = ["chat_name_id = ?"]
                params = [chat_name_id]
                if start_ts is not None:
                    clauses.append("create_time >= ?")
                    params.append(start_ts)
                if end_ts is not None:
                    clauses.append("create_time <= ?")
                    params.append(end_ts)
                row = conn.execute(f"""
                    SELECT COALESCE(SUM(COALESCE(length(voice_data), 0)), 0)
                    FROM VoiceInfo
                    WHERE {" AND ".join(clauses)}
                """, params).fetchone()
                total += int(row[0] or 0)
        except sqlite3.Error:
            return total, "media_error"
    return total, None


def _scan_dir_bytes(path):
    total = 0
    if not os.path.isdir(path):
        return 0
    for root, _, files in os.walk(path):
        for name in files:
            full = os.path.join(root, name)
            try:
                total += os.path.getsize(full)
            except OSError:
                pass
    return total


def _scan_local_attachment_bytes(username):
    base = getattr(mcp_server, "WECHAT_BASE_DIR", "")
    if not base:
        return 0, "scan_base_missing"

    username_hash = hashlib.md5(username.encode()).hexdigest()
    msg_dir = os.path.join(base, "msg")
    roots = [
        os.path.join(msg_dir, "attach", username_hash),
        os.path.join(msg_dir, "file", username_hash),
        os.path.join(msg_dir, "video", username_hash),
    ]
    total = sum(_scan_dir_bytes(path) for path in roots)

    global_roots = [
        os.path.join(msg_dir, "file"),
        os.path.join(msg_dir, "video"),
    ]
    has_unattributed_global_dirs = any(
        os.path.isdir(path) and not os.path.isdir(os.path.join(path, username_hash))
        for path in global_roots
    )
    if has_unattributed_global_dirs:
        return total, "scan_limited"
    return total, None


def _collect_chat_plan_stats(username, message_tables, start_ts=None, end_ts=None,
                             size_mode="estimate"):
    status = []
    message_count = 0
    message_body_bytes = 0
    first_ts = None
    last_ts = None

    if not message_tables:
        status.append("no_message_table")

    for table in message_tables:
        try:
            count, min_ts, max_ts, body_bytes = _query_message_table_plan_stats(
                table["db_path"],
                table["table_name"],
                start_ts=start_ts,
                end_ts=end_ts,
            )
        except (sqlite3.Error, ValueError):
            status.append("message_error")
            continue
        message_count += int(count or 0)
        message_body_bytes += int(body_bytes or 0)
        if min_ts:
            first_ts = min_ts if first_ts is None else min(first_ts, min_ts)
        if max_ts:
            last_ts = max_ts if last_ts is None else max(last_ts, max_ts)

    try:
        resource_bytes, resource_status = _query_resource_estimated_bytes(
            username, start_ts=start_ts, end_ts=end_ts
        )
    except sqlite3.Error:
        resource_bytes, resource_status = 0, "resource_error"
    if resource_status:
        status.append(resource_status)

    voice_bytes, voice_status = _query_voice_estimated_bytes(
        username, start_ts=start_ts, end_ts=end_ts
    )
    if voice_status:
        status.append(voice_status)

    scanned_bytes = ""
    if size_mode == "scan":
        scanned_bytes, scan_status = _scan_local_attachment_bytes(username)
        if scan_status:
            status.append(scan_status)

    attachment_estimated = int(resource_bytes or 0) + int(voice_bytes or 0)
    size_status = "ok" if not status else "partial:" + ",".join(sorted(set(status)))
    return {
        "message_count": message_count,
        "message_body_bytes": message_body_bytes,
        "first_time": _format_plan_time(first_ts),
        "last_time": _format_plan_time(last_ts),
        "attachment_estimated_bytes": attachment_estimated,
        "attachment_scanned_bytes": scanned_bytes,
        "total_estimated_bytes": message_body_bytes + attachment_estimated,
        "size_status": size_status,
    }


def _new_plan_accumulator():
    return {
        "message_count": 0,
        "message_body_bytes": 0,
        "first_ts": None,
        "last_ts": None,
        "attachment_estimated_bytes": 0,
        "attachment_scanned_bytes": "",
        "statuses": set(),
    }


def _message_table_name_for_username(username):
    table_hash = hashlib.md5(username.encode()).hexdigest()
    return f"Msg_{table_hash}"


def _iter_message_db_paths():
    for rel_key in getattr(mcp_server, "MSG_DB_KEYS", []):
        try:
            path = mcp_server._cache.get(rel_key)
        except Exception:
            path = None
        if path:
            yield path


def _fetch_existing_message_tables(conn, table_names):
    if not table_names:
        return set()
    existing = set()
    batch_size = 500
    table_names = list(table_names)
    for i in range(0, len(table_names), batch_size):
        batch = table_names[i:i + batch_size]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            f"WHERE type='table' AND name IN ({placeholders})",
            batch,
        ).fetchall()
        existing.update(row[0] for row in rows)
    return existing


def _query_message_table_plan_stats_conn(conn, table_name, start_ts=None, end_ts=None):
    if not mcp_server._is_safe_msg_table_name(table_name):
        raise ValueError(f"非法消息表名: {table_name}")
    where_sql, params = _where_for_time_range(start_ts, end_ts)
    sql = f"""
        SELECT COUNT(*), MIN(create_time), MAX(create_time),
               COALESCE(SUM(
                   COALESCE(length(message_content), 0)
                   + COALESCE(length(compress_content), 0)
                   + COALESCE(length(packed_info_data), 0)
               ), 0)
        FROM [{table_name}]
        {where_sql}
    """
    return conn.execute(sql, params).fetchone()


def _collect_message_stats_batch(usernames, start_ts=None, end_ts=None):
    table_to_username = {
        _message_table_name_for_username(username): username
        for username in usernames
    }
    stats = {username: _new_plan_accumulator() for username in usernames}
    found = set()
    db_paths = list(_iter_message_db_paths())
    if not db_paths:
        for username in usernames:
            stats[username]["statuses"].add("message_db_missing")
        return stats

    for db_path in db_paths:
        try:
            with closing(sqlite3.connect(db_path)) as conn:
                existing = _fetch_existing_message_tables(conn, table_to_username)
                for table_name in existing:
                    username = table_to_username[table_name]
                    try:
                        count, min_ts, max_ts, body_bytes = (
                            _query_message_table_plan_stats_conn(
                                conn,
                                table_name,
                                start_ts=start_ts,
                                end_ts=end_ts,
                            )
                        )
                    except (sqlite3.Error, ValueError):
                        stats[username]["statuses"].add("message_error")
                        continue
                    found.add(username)
                    stats[username]["message_count"] += int(count or 0)
                    stats[username]["message_body_bytes"] += int(body_bytes or 0)
                    if min_ts:
                        current = stats[username]["first_ts"]
                        stats[username]["first_ts"] = (
                            min_ts if current is None else min(current, min_ts)
                        )
                    if max_ts:
                        current = stats[username]["last_ts"]
                        stats[username]["last_ts"] = (
                            max_ts if current is None else max(current, max_ts)
                        )
        except sqlite3.Error:
            for username in usernames:
                stats[username]["statuses"].add("message_error")

    for username in usernames:
        if username not in found:
            stats[username]["statuses"].add("no_message_table")
    return stats


def _collect_resource_estimates_batch(usernames, start_ts=None, end_ts=None):
    values = {username: 0 for username in usernames}
    statuses = {username: set() for username in usernames}
    resource_db = _get_message_resource_db_path()
    if not resource_db:
        for username in usernames:
            statuses[username].add("resource_missing")
        return values, statuses

    try:
        with closing(sqlite3.connect(resource_db)) as conn:
            usernames = list(usernames)
            batch_size = 500
            for i in range(0, len(usernames), batch_size):
                batch = usernames[i:i + batch_size]
                placeholders = ",".join("?" for _ in batch)
                params = list(batch)
                clauses = [f"c.user_name IN ({placeholders})"]
                if start_ts is not None:
                    clauses.append("i.message_create_time >= ?")
                    params.append(start_ts)
                if end_ts is not None:
                    clauses.append("i.message_create_time <= ?")
                    params.append(end_ts)
                where_sql = " AND ".join(clauses)
                rows = conn.execute(f"""
                    SELECT c.user_name, COALESCE(SUM(COALESCE(d.size, 0)), 0)
                    FROM ChatName2Id c
                    JOIN MessageResourceInfo i ON i.chat_id = c.rowid
                    LEFT JOIN MessageResourceDetail d ON d.message_id = i.message_id
                    WHERE {where_sql}
                    GROUP BY c.user_name
                """, params).fetchall()
                for username, size in rows:
                    values[username] = int(size or 0)
    except sqlite3.Error:
        for username in usernames:
            statuses[username].add("resource_error")
    return values, statuses


def _collect_voice_estimates_batch(usernames, start_ts=None, end_ts=None):
    values = {username: 0 for username in usernames}
    statuses = {username: set() for username in usernames}
    try:
        media_paths = list(mcp_server._iter_media_db_paths())
    except Exception:
        for username in usernames:
            statuses[username].add("media_error")
        return values, statuses

    if not media_paths:
        for username in usernames:
            statuses[username].add("media_missing")
        return values, statuses

    usernames = list(usernames)
    try:
        for media_db in media_paths:
            with closing(sqlite3.connect(media_db)) as conn:
                batch_size = 500
                for i in range(0, len(usernames), batch_size):
                    batch = usernames[i:i + batch_size]
                    placeholders = ",".join("?" for _ in batch)
                    params = list(batch)
                    clauses = [f"n.user_name IN ({placeholders})"]
                    if start_ts is not None:
                        clauses.append("v.create_time >= ?")
                        params.append(start_ts)
                    if end_ts is not None:
                        clauses.append("v.create_time <= ?")
                        params.append(end_ts)
                    where_sql = " AND ".join(clauses)
                    rows = conn.execute(f"""
                        SELECT n.user_name,
                               COALESCE(SUM(COALESCE(length(v.voice_data), 0)), 0)
                        FROM Name2Id n
                        JOIN VoiceInfo v ON v.chat_name_id = n.rowid
                        WHERE {where_sql}
                        GROUP BY n.user_name
                    """, params).fetchall()
                    for username, size in rows:
                        values[username] += int(size or 0)
    except sqlite3.Error:
        for username in usernames:
            statuses[username].add("media_error")
    return values, statuses


def _finalize_plan_stats(acc):
    status = sorted(acc["statuses"])
    size_status = "ok" if not status else "partial:" + ",".join(status)
    return {
        "message_count": acc["message_count"],
        "message_body_bytes": acc["message_body_bytes"],
        "first_time": _format_plan_time(acc["first_ts"]),
        "last_time": _format_plan_time(acc["last_ts"]),
        "attachment_estimated_bytes": acc["attachment_estimated_bytes"],
        "attachment_scanned_bytes": acc["attachment_scanned_bytes"],
        "total_estimated_bytes": (
            acc["message_body_bytes"] + acc["attachment_estimated_bytes"]
        ),
        "size_status": size_status,
    }


def _collect_all_plan_stats(chat_rows, start_ts=None, end_ts=None,
                            size_mode="estimate"):
    usernames = [row["username"] for row in chat_rows]
    print("[*] 批量统计消息表...", flush=True)
    stats = _collect_message_stats_batch(
        usernames,
        start_ts=start_ts,
        end_ts=end_ts,
    )

    print("[*] 批量统计资源附件...", flush=True)
    resource_values, resource_statuses = _collect_resource_estimates_batch(
        usernames,
        start_ts=start_ts,
        end_ts=end_ts,
    )
    for username in usernames:
        stats[username]["attachment_estimated_bytes"] += resource_values[username]
        stats[username]["statuses"].update(resource_statuses[username])

    print("[*] 批量统计语音数据...", flush=True)
    voice_values, voice_statuses = _collect_voice_estimates_batch(
        usernames,
        start_ts=start_ts,
        end_ts=end_ts,
    )
    for username in usernames:
        stats[username]["attachment_estimated_bytes"] += voice_values[username]
        stats[username]["statuses"].update(voice_statuses[username])

    if size_mode == "scan":
        print("[*] 扫描本地附件目录...", flush=True)
        iterable = _tqdm(usernames, desc="扫描附件") if _tqdm else usernames
        for username in iterable:
            scanned_bytes, scan_status = _scan_local_attachment_bytes(username)
            stats[username]["attachment_scanned_bytes"] = scanned_bytes
            if scan_status:
                stats[username]["statuses"].add(scan_status)

    return {
        username: _finalize_plan_stats(stats[username])
        for username in usernames
    }


def _build_plan_csv_rows(chat_rows, start_ts=None, end_ts=None, size_mode="estimate"):
    stats_by_username = _collect_all_plan_stats(
        chat_rows,
        start_ts=start_ts,
        end_ts=end_ts,
        size_mode=size_mode,
    )
    rows = []
    iterable = _tqdm(chat_rows, desc="统计会话") if _tqdm else chat_rows
    for row in iterable:
        username = row["username"]
        stats = stats_by_username[username]
        rows.append({
            "export": "0",
            "index": row["index"],
            "username": username,
            "chat_name": row["display_name"],
            "chat_type": row["kind"],
            "message_count": stats["message_count"],
            "first_time": stats["first_time"],
            "last_time": stats["last_time"],
            "attachment_estimated_bytes": stats["attachment_estimated_bytes"],
            "attachment_scanned_bytes": stats["attachment_scanned_bytes"],
            "total_estimated_bytes": stats["total_estimated_bytes"],
            "size_status": stats["size_status"],
        })
    return rows


def _validate_plan_mode(plan_mode):
    if plan_mode not in (PLAN_MODE_BLACKLIST, PLAN_MODE_WHITELIST):
        raise ValueError(f"未知导出计划模式: {plan_mode}")
    return plan_mode


def _write_plan_csv(path, rows, plan_mode=PLAN_MODE_BLACKLIST):
    plan_mode = _validate_plan_mode(plan_mode)
    out_dir = os.path.dirname(os.path.abspath(path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=PLAN_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            full = {field: "" for field in PLAN_CSV_FIELDS}
            full.update(row)
            if full["export"] == "":
                full["export"] = (
                    "0" if plan_mode == PLAN_MODE_WHITELIST else "1"
                )
            writer.writerow(full)


def _load_selected_usernames_from_plan_csv(
    path, valid_usernames, plan_mode=PLAN_MODE_BLACKLIST
):
    plan_mode = _validate_plan_mode(plan_mode)
    selected = []
    seen = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "username" not in reader.fieldnames:
            raise ValueError("CSV 缺少 username 列")
        for line_no, row in enumerate(reader, 2):
            username = (row.get("username") or "").strip()
            if not username:
                raise ValueError(f"第 {line_no} 行缺少 username")
            if username in seen:
                raise ValueError(
                    f"CSV 中 username 重复: {username} "
                    f"(第 {seen[username]} 行和第 {line_no} 行)"
                )
            seen[username] = line_no

            flag = (row.get("export") or "").strip()
            if plan_mode == PLAN_MODE_WHITELIST:
                should_export = flag == "1"
            else:
                should_export = flag != "0"
            if not should_export:
                continue
            if username not in valid_usernames:
                raise ValueError(f"第 {line_no} 行 username 当前不存在: {username}")
            selected.append(username)
    return selected


def export_one(username, output_dir, names, transcribe=False,
               start_ts=None, end_ts=None, incremental=False):
    """
    导出单个会话。

    参数:
        start_ts: 消息起始时间戳（None = 全部）
        end_ts: 消息结束时间戳（None = 全部）
        incremental: 增量模式（追加到已有消息，跳过重复）

    返回: (成功标志, 总消息数, 新增消息数, 错误信息)
    """
    ctx = mcp_server._resolve_chat_context(username)
    if ctx is None:
        return False, 0, 0, f"Cannot resolve: {username}"

    display_name = ctx["display_name"]
    message_tables = ctx["message_tables"]

    if not message_tables:
        return False, 0, 0, "no tables"

    # 输出文件名可随备注变化；索引用 username 维持稳定匹配。
    try:
        out_path, export_index = _resolve_indexed_export_path(
            output_dir, username, display_name, ctx["is_group"]
        )
    except Exception as e:
        return False, 0, 0, f"export index error: {e}"

    # 增量模式：读取已有消息和最后时间戳
    existing_msgs = []
    last_ts = 0
    if incremental and os.path.isfile(out_path):
        existing_msgs = _get_existing_messages(out_path)
        last_ts = _get_last_message_ts(out_path)
        if last_ts and (start_ts is None or start_ts < last_ts):
            start_ts = last_ts

    # 如果提供了 start_ts/end_ts 但没有增量数据，仍需查询
    if start_ts is not None and incremental and not existing_msgs:
        # 无增量目标文件，退化为普通导出
        incremental = False

    new_rows = []
    for table_info in message_tables:
        db_path = table_info["db_path"]
        table_name = table_info["table_name"]
        try:
            with closing(sqlite3.connect(db_path)) as conn:
                id_to_username = mcp_server._load_name2id_maps(conn)

                # 增量模式：只查 start_ts 之后的消息
                if start_ts is not None or end_ts is not None:
                    rows = mcp_server._query_messages(
                        conn, table_name,
                        start_ts=start_ts, end_ts=end_ts,
                        limit=None, oldest_first=True,
                    )
                else:
                    rows = mcp_server._query_messages(
                        conn, table_name, limit=None, oldest_first=True
                    )

                for row in rows:
                    new_rows.append((row, id_to_username))
        except Exception as e:
            return False, 0, 0, f"DB query error: {e}"

    new_rows.sort(key=lambda pair: pair[0][2] or 0)

    local_ids_existing = {m.get("local_id") for m in existing_msgs}

    # 构建已有消息的 local_id → message 映射（用于合并时保留 transcription）
    existing_by_lid = {m.get("local_id"): m for m in existing_msgs}

    new_messages = []
    for row, id_to_username in new_rows:
        local_id, local_type, create_time, real_sender_id, content, ct = row

        # 增量模式：跳过已存在的消息
        if incremental and local_id in local_ids_existing:
            continue

        sender = _resolve_sender(row, ctx, names, id_to_username)
        type_str = _msg_type_str(local_type)
        rendered, extras = _extract_content(
            local_id, local_type, content, ct, username, display_name
        )

        msg = {"local_id": local_id, "timestamp": create_time, "sender": sender}
        effective_type = (extras or {}).get("type") or type_str
        if effective_type != "text":
            msg["type"] = effective_type
        if rendered is not None:
            msg["content"] = rendered
        if extras:
            for k, v in extras.items():
                if k == "type":
                    continue
                msg[k] = v
        new_messages.append(msg)

    # 合并消息
    messages = existing_msgs + new_messages
    new_count = len(new_messages)

    if not messages:
        return False, 0, 0, "empty"

    # ── 语音转录 ──────────────────────────────────────────────
    if transcribe:
        # 只需转录新消息中的语音
        voices_to_transcribe = new_messages if incremental else [
            m for m in messages
            if m.get("type") == "voice" and not m.get("transcription")
        ]
        transcribed = 0
        failed = 0
        for msg in voices_to_transcribe:
            if msg.get("type") != "voice":
                continue
            lid = msg["local_id"]
            try:
                row = mcp_server._fetch_voice_row(username, lid)
                if row is None:
                    continue
                voice_data, create_time = row
                wav_path, _ = mcp_server._silk_to_wav(
                    voice_data, create_time, username, lid
                )
                backend = _resolve_backend()
                result = mcp_server._transcribe(wav_path, backend)
                if result and result.get("text"):
                    msg["transcription"] = result["text"]
                    transcribed += 1
                os.unlink(wav_path)
            except Exception:
                failed += 1
        if transcribed or failed:
            display = names.get(username, username)
            voice_total = len(voices_to_transcribe)
            print(
                f"   转录: {transcribed}/{voice_total} 条语音"
                + (f" ({failed} 失败)" if failed else "")
            )

    # ── 写文件 ────────────────────────────────────────────────
    output = {
        "chat": display_name,
        "username": username,
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date_first_msg": _date_from_message_ts(messages[0].get("timestamp")),
        "date_last_msg": _date_from_message_ts(messages[-1].get("timestamp")),
        "messages": messages,
    }
    if ctx["is_group"]:
        output["is_group"] = True
    output.update(_contact_metadata_for_export(username, ctx["is_group"]))

    os.makedirs(os.path.dirname(out_path) if os.path.dirname(out_path) else ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    _update_export_index(
        output_dir, export_index, username, display_name, ctx["is_group"],
        out_path, output
    )

    return True, len(messages), new_count, None


_BACKEND_CACHE = None


def _resolve_backend():
    """解析转录 backend，结果缓存以避免重复检测。"""
    global _BACKEND_CACHE
    if _BACKEND_CACHE is None:
        try:
            _BACKEND_CACHE = mcp_server._resolve_active_backend()
        except Exception:
            _BACKEND_CACHE = "local"
    return _BACKEND_CACHE


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="批量导出所有微信聊天记录为 JSON 文件，可选附带语音转录",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    python3 export_all_chats.py                           全量导出所有会话
    python3 export_all_chats.py --list-chats              只列出所有会话，不写文件
    python3 export_all_chats.py --select                  列表后交互选择会话
    python3 export_all_chats.py --chats "1,3-5,张三"       按编号/范围/关键词导出
    python3 export_all_chats.py --write-plan-csv export_plan.csv
    python3 export_all_chats.py --write-plan-csv export_plan.csv --plan-mode whitelist
    python3 export_all_chats.py --write-plan-csv export_plan.csv --size-mode scan
    python3 export_all_chats.py output_dir --from-plan-csv export_plan.csv
    python3 export_all_chats.py output_dir --from-plan-csv export_plan.csv --plan-mode whitelist
    python3 export_all_chats.py -t                        全量导出 + 转录语音
    python3 export_all_chats.py -i                        增量（追加新消息）
    python3 export_all_chats.py --start 2025-01-01        按日期范围导出
    python3 export_all_chats.py --end 2025-01-31          按日期范围导出
    python3 export_all_chats.py --start 2025-01-01 --end 2025-01-31 -t
""",
    )
    parser.add_argument(
        "output_dir",
        nargs="?",
        default=None,
        help="输出目录路径 (默认: ./exported_chats)",
    )
    parser.add_argument(
        "-t",
        "--with-transcriptions",
        action="store_true",
        help="导出时一并转录语音消息（依赖 config.json 配置的 backend）",
    )
    parser.add_argument(
        "--list-chats",
        action="store_true",
        help="只列出所有会话编号、类型、名称和 username，不导出",
    )
    parser.add_argument(
        "--select",
        action="store_true",
        help="先列出会话，再交互输入编号/范围/关键词选择导出",
    )
    parser.add_argument(
        "--chats",
        default=None,
        help="只导出匹配的会话，支持编号、范围、显示名或 username，逗号分隔",
    )
    parser.add_argument(
        "--write-plan-csv",
        default=None,
        help="生成可人工编辑的导出计划 CSV，不导出聊天",
    )
    parser.add_argument(
        "--from-plan-csv",
        default=None,
        help="读取导出计划 CSV，按 --plan-mode 判断哪些 username 需要导出",
    )
    parser.add_argument(
        "--plan-mode",
        choices=(PLAN_MODE_BLACKLIST, PLAN_MODE_WHITELIST),
        default=PLAN_MODE_BLACKLIST,
        help=(
            "导出计划模式：blacklist=只有 export=0 跳过；"
            "whitelist=只有 export=1 导出"
        ),
    )
    parser.add_argument(
        "--size-mode",
        choices=("estimate", "scan"),
        default="estimate",
        help="生成计划 CSV 时的大小统计方式：estimate=快估，scan=尝试扫描本地附件",
    )
    parser.add_argument(
        "-i",
        "--incremental",
        action="store_true",
        help="增量导出：只追加新消息到已有 JSON 文件",
    )
    parser.add_argument(
        "--start",
        default=None,
        help="起始日期 (如 2025-01-01 或 Unix 时间戳)",
    )
    parser.add_argument(
        "--end",
        default=None,
        help="结束日期 (如 2025-01-31 或 Unix 时间戳)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="预览模式：显示将导出的会话，不实际写入",
    )
    parser.add_argument(
        "--users",
        default=None,
        help="只导出指定 username 的会话, 逗号分隔 (如 wxid_xxx,12345@chatroom). "
             "为空时导出全部 (旧行为). 也可用 env WECHAT_EXPORT_USERS",
    )
    args = parser.parse_args(argv)

    if args.select and args.chats:
        parser.error("--select 和 --chats 只能使用一个")
    if args.write_plan_csv and args.from_plan_csv:
        parser.error("--write-plan-csv 和 --from-plan-csv 只能使用一个")
    if args.from_plan_csv and (args.select or args.chats):
        parser.error("--from-plan-csv 不能与 --select/--chats 同时使用")
    if args.write_plan_csv and (args.select or args.chats or args.list_chats):
        parser.error("--write-plan-csv 不能与 --list-chats/--select/--chats 同时使用")

    script_dir = os.path.dirname(os.path.abspath(__file__))
    output_dir = args.output_dir or os.path.join(script_dir, "exported_chats")

    start_ts = _parse_timestamp(args.start) if args.start else None
    end_ts = _parse_timestamp(args.end) if args.end else None
    if args.start and start_ts is None:
        print(f"错误: 无法解析起始时间: {args.start}", file=sys.stderr)
        print("支持格式: 2025-01-01, 2025-01-01 14:30, 2025-01-01T14:30:00", file=sys.stderr)
        sys.exit(1)
    if args.end and end_ts is None:
        print(f"错误: 无法解析结束时间: {args.end}", file=sys.stderr)
        print("支持格式: 2025-01-01, 2025-01-01 14:30, 2025-01-01T14:30:00", file=sys.stderr)
        sys.exit(1)

    if args.with_transcriptions:
        try:
            backend = _resolve_backend()
            print(f"语音转录: 启用 (backend={backend})")
        except Exception as e:
            print(f"语音转录: backend 解析失败: {e}", file=sys.stderr)
            args.with_transcriptions = False

    if not os.path.exists(mcp_server.DECRYPTED_DIR):
        print(f"错误: 解密目录不存在: {mcp_server.DECRYPTED_DIR}", file=sys.stderr)
        sys.exit(1)

    session_db = os.path.join(mcp_server.DECRYPTED_DIR, "session", "session.db")
    try:
        sessions = _load_session_usernames(session_db)
    except sqlite3.Error as e:
        print(f"会话数据库查询失败: {e}", file=sys.stderr)
        sys.exit(1)

    # username 白名单过滤 (--users 参数 / WECHAT_EXPORT_USERS 环境变量)
    users_filter_raw = args.users or os.environ.get("WECHAT_EXPORT_USERS", "")
    if users_filter_raw.strip():
        wanted = {u.strip() for u in users_filter_raw.split(",") if u.strip()}
        before = len(sessions)
        sessions = [u for u in sessions if u in wanted]
        print(f"按 --users 过滤: {before} → {len(sessions)} 会话")
        if not sessions:
            print(f"[!] 指定的 username 列表跟会话表没交集 (wanted={list(wanted)[:5]}...)",
                  file=sys.stderr)
            sys.exit(1)

    names = mcp_server.get_contact_names()
    contact_full = mcp_server.get_contact_full()
    chat_rows = _build_chat_rows(sessions, names, contact_full)

    # 显示模式信息
    mode = ""
    if args.incremental:
        mode = "增量模式"
    if start_ts:
        start_dt = datetime.fromtimestamp(start_ts).strftime("%Y-%m-%d %H:%M")
        mode += f" 起始={start_dt}"
    if end_ts:
        end_dt = datetime.fromtimestamp(end_ts).strftime("%Y-%m-%d %H:%M")
        mode += f" 结束={end_dt}"
    if not mode:
        mode = "全量模式"
    if args.dry_run:
        mode += " (预览)"

    print(f"会话总数: {len(sessions)}")
    print(f"联系人映射: {len(names)}")
    print(f"输出目录: {output_dir}")
    print(f"模式: {mode}")
    print("=" * 60)

    if args.list_chats:
        _print_chat_rows(chat_rows)
        return

    if args.write_plan_csv:
        rows = _build_plan_csv_rows(
            chat_rows,
            start_ts=start_ts,
            end_ts=end_ts,
            size_mode=args.size_mode,
        )
        try:
            _write_plan_csv(args.write_plan_csv, rows, plan_mode=args.plan_mode)
        except OSError as e:
            print(
                f"写入导出计划 CSV 失败: {e}\n"
                "请确认目标文件没有被 Excel/WPS 打开，或换一个输出文件名。",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"已生成导出计划 CSV: {args.write_plan_csv}")
        if args.plan_mode == PLAN_MODE_WHITELIST:
            print("白名单模式：请在 export 列填写 1 后，使用 --from-plan-csv 导出。")
        else:
            print("黑名单模式：请将不导出的行改为 export=0，再使用 --from-plan-csv 导出。")
        return

    if args.select:
        _print_chat_rows(chat_rows)
        print("=" * 60)
        selection = input("请输入要导出的编号/范围/关键词（逗号分隔，直接回车取消）: ")
        if not selection.strip():
            print("未选择任何会话，已取消。")
            return
        try:
            sessions = _select_chat_usernames(chat_rows, selection)
        except ValueError as e:
            print(f"选择失败: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.chats:
        try:
            sessions = _select_chat_usernames(chat_rows, args.chats)
        except ValueError as e:
            print(f"选择失败: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.from_plan_csv:
        try:
            sessions = _load_selected_usernames_from_plan_csv(
                args.from_plan_csv,
                {row["username"] for row in chat_rows},
                plan_mode=args.plan_mode,
            )
        except (OSError, ValueError) as e:
            print(f"读取导出计划 CSV 失败: {e}", file=sys.stderr)
            sys.exit(1)
        if not sessions:
            print("未选择任何会话，已取消。")
            return

    if args.chats or args.select or args.from_plan_csv:
        row_by_username = {row["username"]: row for row in chat_rows}
        selected_rows = [row_by_username[u] for u in sessions if u in row_by_username]
        print(f"本次选择: {len(sessions)} 个会话")
        _print_chat_rows(selected_rows)
        print("=" * 60)

    if args.dry_run:
        print("预览模式：未写入任何导出文件。")
        return

    os.makedirs(output_dir, exist_ok=True)

    t0 = time.time()
    ok, skip, err, total = 0, 0, 0, 0
    total_new = 0

    total_sessions = len(sessions)
    for i, username in enumerate(sessions, 1):
        display = names.get(username, username)
        chat_t0 = time.time()
        print(f"[{i}/{total_sessions}] 开始导出: {display} ({username})", flush=True)
        success, total_msgs, new_msgs, reason = export_one(
            username, output_dir, names,
            transcribe=args.with_transcriptions,
            start_ts=start_ts,
            end_ts=end_ts,
            incremental=args.incremental,
        )
        if success:
            ok += 1
            total += total_msgs
            total_new += new_msgs
            if new_msgs > 0 or args.incremental:
                label = f"+{new_msgs} new" if args.incremental else f"{total_msgs} msgs"
            else:
                label = f"{total_msgs} msgs"
            elapsed = time.time() - t0
            chat_elapsed = time.time() - chat_t0
            eta = (elapsed / i) * (total_sessions - i) if i > 0 else 0
            print(
                f"[{i}/{total_sessions}] 完成导出: {display} - {label} "
                f"(本会话 {chat_elapsed:.1f}s, ETA {eta/60:.1f}分)",
                flush=True,
            )
        else:
            if "no tables" in str(reason) or "empty" in str(reason):
                skip += 1
                elapsed = time.time() - t0
                chat_elapsed = time.time() - chat_t0
                eta = (elapsed / i) * (total_sessions - i) if i > 0 else 0
                print(
                    f"[{i}/{total_sessions}] 跳过: {display} ({reason}) "
                    f"(本会话 {chat_elapsed:.1f}s, ETA {eta/60:.1f}分)",
                    flush=True,
                )
            else:
                err += 1
                elapsed = time.time() - t0
                chat_elapsed = time.time() - chat_t0
                eta = (elapsed / i) * (total_sessions - i) if i > 0 else 0
                print(
                    f"[{i}/{total_sessions}] 失败: {display} - {reason} "
                    f"(本会话 {chat_elapsed:.1f}s, ETA {eta/60:.1f}分)",
                    flush=True,
                )

    elapsed = time.time() - t0
    print()
    print("=" * 60)
    extra = f" (新增 {total_new} 条)" if args.incremental and total_new > 0 else ""
    print(
        f"完成! 成功={ok} 跳过={skip} 失败={err} "
        f"总消息={total}{extra} 耗时={elapsed/60:.1f}分"
    )


if __name__ == "__main__":
    main()
