"""
Export all messages for a given chat to JSON.

Usage:
    .venv/bin/python3 export_chat.py <chat_name> [output.json]

Arguments:
    <chat_name>    Contact display name, remark name, group name, or wxid.
    [output.json]  Optional output path. Defaults to "<chat_name>_export.json".

Example:
    .venv/bin/python3 export_chat.py <contact_name>
    .venv/bin/python3 export_chat.py <group_name> /tmp/out.json

Output is a JSON file with shape:
    {
      "chat": "<display name>",
      "exported_at": "YYYY-MM-DD HH:MM:SS",
      "is_group": true,          // only present for groups
      "messages": [
        {"local_id": 1, "timestamp": 1713..., "sender": "me", "content": "..."},
        {"local_id": 2, "timestamp": 1713..., "sender": "<name>", "type": "voice"}
      ]
    }

Defaults/nulls are omitted: "type" is absent for text messages, "content" is
absent when nothing extractable, "is_group" is absent for 1-on-1 chats.

Voice messages are exported as type "voice" without a transcription field.
Run transcribe_chat.py on the output to fill in Whisper transcriptions.

Requires the WeChat DBs to already be decrypted (see README).

Full schema, field semantics, and loading examples: docs/chat_export_format.md
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
    Returns the 'default' language label (usually Chinese), or None."""
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


def _extract_content(local_id, local_type, content, ct, chat_username, chat_display_name):
    content = mcp_server._decompress_content(content, ct)
    if content is None:
        return None

    base, _ = mcp_server._split_msg_type(local_type)
    if base == 1:
        return content or ""
    if base == 43:
        return _format_video_message(content)
    if base == 47:
        return _format_sticker_message(content)
    if base == 49:
        return mcp_server._format_app_message_text(
            content, local_type, False, chat_username, chat_display_name, {}
        )
    if base == 50:
        return mcp_server._format_voip_message_text(content)
    if base == 10000:
        return _format_system_message(content)
    if base == 10002:
        return "[撤回消息]"
    return None


def export_chat(chat_name, output_path):
    ctx = mcp_server._resolve_chat_context(chat_name)
    names = mcp_server.get_contact_names()

    # Each shard has its own Name2Id table, so we must pair rows with the
    # id_to_username map from their source DB.
    all_rows = []
    for table_info in ctx["message_tables"]:
        db_path = table_info["db_path"]
        table_name = table_info["table_name"]
        with closing(sqlite3.connect(db_path)) as conn:
            id_to_username = mcp_server._load_name2id_maps(conn)
            rows = mcp_server._query_messages(conn, table_name, limit=999999, oldest_first=True)
            for row in rows:
                all_rows.append((row, id_to_username))

    # Sort across shards by create_time
    all_rows.sort(key=lambda pair: pair[0][2])

    username = ctx["username"]
    display_name = ctx["display_name"]

    messages = []
    for row, id_to_username in all_rows:
        local_id, local_type, create_time, real_sender_id, content, ct = row
        sender = _resolve_sender(row, ctx, names, id_to_username)
        type_str = _msg_type_str(local_type)
        rendered = _extract_content(local_id, local_type, content, ct, username, display_name)

        # Compact format: omit defaults/nulls. type defaults to "text", transcription
        # is added later by transcribe_chat.py only for voice messages. See CLAUDE.md.
        msg = {
            "local_id": local_id,
            "timestamp": create_time,
            "sender": sender,
        }
        if type_str != "text":
            msg["type"] = type_str
        if rendered is not None:
            msg["content"] = rendered
        messages.append(msg)

    output = {
        "chat": display_name,
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
