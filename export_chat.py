"""
将单个聊天的全部消息导出为 JSON。

用法:
    .venv/bin/python3 export_chat.py <chat_name> [output.json]

参数:
    <chat_name>    联系人显示名、备注名、群名或 wxid。
    [output.json]  可选输出路径，默认 "<chat_name>_export.json"。

示例:
    .venv/bin/python3 export_chat.py <contact_name>
    .venv/bin/python3 export_chat.py <group_name> /tmp/out.json

输出 JSON 的紧凑结构:
    {
      "chat": "<display name>",
      "username": "<wxid 或 @chatroom>",
      "exported_at": "YYYY-MM-DD HH:MM:SS",
      "is_group": true,          // 仅群聊出现
      "messages": [
        {"local_id": 1, "timestamp": 1713..., "sender": "me", "content": "..."},
        {"local_id": 2, "timestamp": 1713..., "sender": "<name>", "type": "voice"}
      ]
    }

默认值/空值会被省略: text 消息省略 "type"，无可提取内容时省略 "content"，
1-on-1 聊天省略 "is_group"。

语音消息以 type "voice" 导出且不带 transcription 字段；运行
transcribe_chat.py 可用 Whisper 补齐转录。

需先完成 WeChat DB 解密（详见 README）。

完整 schema、字段语义与加载示例: docs/chat_export_format.md
"""
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime

import mcp_server
from chat_export_helpers import (
    _extract_content,
    _msg_type_str,
    _resolve_sender,
)


def export_chat(chat_name, output_path):
    ctx = mcp_server._resolve_chat_context(chat_name)
    if ctx is None:
        print(f"Could not resolve chat: {chat_name}")
        sys.exit(1)

    username = ctx["username"]
    display_name = ctx["display_name"]
    # resolve_username 对模糊匹配会静默选第一个命中，打印一下便于用户核对。
    print(f"Resolved to: {display_name} ({username})")

    if not ctx["message_tables"]:
        print(f"No message tables found for {username}")
        sys.exit(1)

    names = mcp_server.get_contact_names()

    # Each shard has its own Name2Id table, so we must pair rows with the
    # id_to_username map from their source DB.
    all_rows = []
    for table_info in ctx["message_tables"]:
        db_path = table_info["db_path"]
        table_name = table_info["table_name"]
        with closing(sqlite3.connect(db_path)) as conn:
            id_to_username = mcp_server._load_name2id_maps(conn)
            rows = mcp_server._query_messages(conn, table_name, limit=None, oldest_first=True)
            for row in rows:
                all_rows.append((row, id_to_username))

    # Sort across shards by create_time (defensive "or 0" in case a row has NULL).
    all_rows.sort(key=lambda pair: pair[0][2] or 0)

    messages = []
    for row, id_to_username in all_rows:
        local_id, local_type, create_time, real_sender_id, content, ct = row
        sender = _resolve_sender(row, ctx, names, id_to_username)
        type_str = _msg_type_str(local_type)
        rendered, extras = _extract_content(
            local_id, local_type, content, ct, username, display_name
        )

        # Compact format: omit defaults/nulls. type defaults to "text", transcription
        # is added later by transcribe_chat.py only for voice messages. See CLAUDE.md.
        msg = {
            "local_id": local_id,
            "timestamp": create_time,
            "sender": sender,
        }
        # extras may override type with a more specific value (e.g. "transfer"
        # narrower than the generic "link_or_file" base=49 maps to).
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
        messages.append(msg)

    output = {
        "chat": display_name,
        "username": username,
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "messages": messages,
    }
    if ctx["is_group"]:
        output["is_group"] = True

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Exported {len(messages)} messages to {output_path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 export_chat.py <chat_name> [output.json]")
        sys.exit(1)
    chat = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else f"{chat}_export.json"
    export_chat(chat, out)
