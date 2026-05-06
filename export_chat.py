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


MSG_TYPE_MAP = {
    1: "text",
    3: "image",
    34: "voice",
    42: "contact_card",
    43: "video",
    47: "sticker",
    48: "location",
    49: "link_or_file",
    50: "call",
    10000: "system",
    10002: "recall",
}


def _msg_type_str(local_type):
    base, _ = mcp_server._split_msg_type(local_type)
    return MSG_TYPE_MAP.get(base, f"type_{local_type}")


def _resolve_sender(row, ctx, names, id_to_username):
    """Resolve the sender of a message.

    Returns "me" for the logged-in user, or the sender's display name otherwise
    (the contact's name in 1-on-1 chats, the member's name in groups). Empty
    string for unattributable messages (e.g. system notifications).
    """
    local_id, local_type, create_time, real_sender_id, content, ct = row
    decoded = mcp_server._decompress_content(content, ct)
    sender_from_content, _ = mcp_server._format_message_text(
        local_id, local_type, decoded, ctx["is_group"], ctx["username"], ctx["display_name"], names
    )
    label = mcp_server._resolve_sender_label(
        real_sender_id,
        sender_from_content,
        ctx["is_group"],
        ctx["username"],
        ctx["display_name"],
        names,
        id_to_username,
    )
    return label or ""


def _decode_sticker_desc(b64_desc):
    """WeChat encodes sticker labels as base64 protobuf: repeated (lang, text) pairs.
    Returns the 'default' language label (usually Chinese), or None.

    Limitation: treats the length byte as a single octet rather than a real protobuf
    varint — labels >127 bytes would be misread. In practice sticker descriptions are
    short (<30 chars), so this is adequate. Also sensitive to the bytes b"default"
    appearing inside a preceding value; no such cases observed.
    """
    import base64
    try:
        raw = base64.b64decode(b64_desc)
    except Exception:
        return None
    # Find the 'default' marker; text follows as: \x12 <varint len> <utf-8>
    i = raw.find(b"default")
    if i < 0 or i + 7 >= len(raw) or raw[i + 7] != 0x12:
        return None
    try:
        text_len = raw[i + 8]
        text_bytes = raw[i + 9 : i + 9 + text_len]
        return text_bytes.decode("utf-8") or None
    except (IndexError, UnicodeDecodeError):
        return None


def _format_sticker_message(content):
    root = mcp_server._parse_xml_root(content) if content else None
    if root is None:
        return "[表情]"
    emoji = root.find(".//emoji")
    if emoji is None:
        return "[表情]"
    desc = emoji.get("desc") or ""
    label = _decode_sticker_desc(desc) if desc else None
    return f"[表情] {label}" if label else "[表情]"


def _format_system_message(content):
    if not content:
        return "[系统消息]"
    if "<sysmsg" not in content:
        return content
    root = mcp_server._parse_xml_root(content)
    if root is None:
        return content
    inner = root.findtext(".//content")
    return inner.strip() if inner else content


def _format_video_message(content):
    root = mcp_server._parse_xml_root(content) if content else None
    if root is None:
        return "[视频]"
    video = root.find(".//videomsg")
    if video is None:
        return "[视频]"
    playlength = video.get("playlength")
    return f"[视频] {playlength}秒" if playlength else "[视频]"


def _extract_transfer_extras(content):
    """Detect appmsg type=2000 and return structured transfer fields, else None.

    Reuses mcp_server._extract_transfer_info so the schema/version-quirks logic
    lives in one place. Empty values are dropped to keep the export compact.
    Numeric timestamps are returned as ints (consistent with the top-level
    `timestamp` field), not iso strings — downstream consumers can format.
    """
    if not content or '<appmsg' not in content:
        return None
    root = mcp_server._parse_app_message_outer(content)
    if root is None:
        return None
    appmsg = root.find('.//appmsg')
    if appmsg is None:
        return None
    app_type = mcp_server._parse_int(
        mcp_server._collapse_text(appmsg.findtext('type') or ''), 0
    )
    if app_type != 2000:
        return None

    info = mcp_server._extract_transfer_info(appmsg)
    if not info:
        return None

    out = {}
    if info['paysubtype_label']:
        out['direction'] = info['paysubtype_label']
    for k in ('paysubtype', 'fee_desc', 'pay_memo',
              'payer_username', 'receiver_username',
              'transfer_id', 'transcation_id', 'pay_msg_id'):
        v = info.get(k)
        if v:
            out[k] = v
    for k in ('begin_transfer_time', 'invalid_time'):
        v = mcp_server._parse_int(info.get(k) or '', 0)
        if v:
            out[k] = v
    return out or None


def _extract_content(local_id, local_type, content, ct, chat_username, chat_display_name):
    """Return (rendered_text, extras_dict). Either may be None.

    extras carries structured fields for non-text message types where caller
    wants more than the human-readable string (currently: transfer). Future
    additions (视频号 metadata, merged-forward expansion, …) can flow through
    the same channel without changing the caller signature.
    """
    content = mcp_server._decompress_content(content, ct)
    if content is None:
        return None, None

    base, _ = mcp_server._split_msg_type(local_type)
    if base == 1:
        return (content or ""), None
    if base == 43:
        return _format_video_message(content), None
    if base == 47:
        return _format_sticker_message(content), None
    if base == 49:
        rendered = mcp_server._format_app_message_text(
            content, local_type, False, chat_username, chat_display_name, {}
        )
        transfer = _extract_transfer_extras(content)
        extras = {'type': 'transfer', 'transfer': transfer} if transfer else None
        return rendered, extras
    if base == 50:
        return mcp_server._format_voip_message_text(content), None
    if base == 10000:
        return _format_system_message(content), None
    if base == 10002:
        return "[撤回消息]", None
    return None, None


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
