#!/usr/bin/env python3
"""批量导出所有微信聊天记录为 JSON 文件。

此脚本将导出所有会话的聊天记录，输出格式与 export_chat.py 完全一致。
支持导出到指定目录，默认输出到 ./exported_chats 目录。

用法:
    python3 export_all_chats.py [output_dir]

示例:
    python3 export_all_chats.py /path/to/output
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from datetime import datetime

import mcp_server
from chat_export_helpers import _extract_content, _msg_type_str, _resolve_sender


def export_one(username, output_dir, names):
    """
    导出单个会话。

    返回: (成功标志, 消息数, 错误信息)
    """
    ctx = mcp_server._resolve_chat_context(username)
    if ctx is None:
        return False, 0, f"Cannot resolve: {username}"

    display_name = ctx["display_name"]
    message_tables = ctx["message_tables"]

    if not message_tables:
        return False, 0, "no tables"

    all_rows = []
    for table_info in message_tables:
        db_path = table_info["db_path"]
        table_name = table_info["table_name"]
        try:
            with closing(sqlite3.connect(db_path)) as conn:
                id_to_username = mcp_server._load_name2id_maps(conn)
                rows = mcp_server._query_messages(
                    conn, table_name, limit=None, oldest_first=True
                )
                for row in rows:
                    all_rows.append((row, id_to_username))
        except Exception as e:
            return False, 0, f"DB query error: {e}"

    all_rows.sort(key=lambda pair: pair[0][2] or 0)

    messages = []
    for row, id_to_username in all_rows:
        local_id, local_type, create_time, real_sender_id, content, ct = row
        sender = _resolve_sender(row, ctx, names, id_to_username)
        type_str = _msg_type_str(local_type)
        rendered = _extract_content(local_id, local_type, content, ct, username, display_name)

        msg = {"local_id": local_id, "timestamp": create_time, "sender": sender}
        if type_str != "text":
            msg["type"] = type_str
        if rendered is not None:
            msg["content"] = rendered
        messages.append(msg)

    if not messages:
        return False, 0, "empty"

    output = {
        "chat": display_name,
        "username": username,
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "messages": messages,
    }
    if ctx["is_group"]:
        output["is_group"] = True

    prefix = "group" if ctx["is_group"] else "single"
    safe = re.sub(r'[\\/:*?"<>|]', "_", f"{prefix}_{display_name}")
    out_path = os.path.join(output_dir, f"{safe}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    return True, len(messages), None


def main():
    parser = argparse.ArgumentParser(
        description="批量导出所有微信聊天记录为 JSON 文件",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    python3 export_all_chats.py /path/to/output
        """,
    )
    parser.add_argument(
        "output_dir",
        nargs="?",
        default=None,
        help="输出目录路径 (默认: ./exported_chats)",
    )
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    output_dir = args.output_dir or os.path.join(script_dir, "exported_chats")

    if not os.path.exists(mcp_server.DECRYPTED_DIR):
        print(f"错误: 解密目录不存在: {mcp_server.DECRYPTED_DIR}", file=sys.stderr)
        sys.exit(1)
    os.makedirs(output_dir, exist_ok=True)

    session_db = os.path.join(mcp_server.DECRYPTED_DIR, "session", "session.db")
    try:
        with closing(sqlite3.connect(session_db)) as conn:
            sessions = [u for u, _ in conn.execute("SELECT username, type FROM SessionTable")]
    except sqlite3.Error as e:
        print(f"会话数据库查询失败: {e}", file=sys.stderr)
        sys.exit(1)

    names = mcp_server.get_contact_names()

    print(f"会话总数: {len(sessions)}")
    print(f"联系人映射: {len(names)}")
    print(f"输出目录: {output_dir}")
    print("=" * 60)

    ok, skip, err, total = 0, 0, 0, 0
    for i, username in enumerate(sessions, 1):
        display = names.get(username, username)
        success, count, reason = export_one(username, output_dir, names)
        if success:
            ok += 1
            total += count
            if i <= 10 or i % 100 == 0:
                print(f"[{i}/{len(sessions)}] {display} - {count} 条消息")
        else:
            if "no tables" in str(reason) or "empty" in str(reason):
                skip += 1
                if i <= 10 or i % 50 == 0:
                    print(f"[{i}/{len(sessions)}] {display} - 跳过({reason})")
            else:
                err += 1
                print(f"[{i}/{len(sessions)}] {display} - 失败: {reason}")

    print()
    print("=" * 60)
    print(f"完成! 成功={ok} 跳过={skip} 失败={err} 总消息={total}")


if __name__ == "__main__":
    main()
