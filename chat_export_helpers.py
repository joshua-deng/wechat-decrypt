"""聊天导出共享工具函数。

本模块包含 export_chat.py 和 export_all_chats.py 共用的消息格式化函数。
统一维护，避免两处代码漂移。
"""

import base64

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
