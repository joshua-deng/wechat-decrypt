"""导出企业微信消息记录到 CSV / HTML / JSON。

输入目录默认来自 wxwork_decrypted_dir，输出到 wxwork_export_dir。
可用环境变量:
  WXWORK_EXPORT_CONVERSATIONS=conversation_id1,conversation_id2
  WXWORK_EXPORT_FORMATS=csv,html,json
"""
import argparse
import csv
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from html import escape


if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


MSG_TYPES = {
    0: "文本/混合",
    2: "文本",
    4: "图片",
    7: "语音",
    15: "图片/文件",
    38: "应用消息",
    40: "通话/音视频",
    503: "状态",
    1011: "会议通知",
}

_MESSAGE_TABLES = ("message_table", "message_small_table", "kf_message_tableV1")


def _app_paths():
    from config import _app_base_dir, _config_file_path

    return _app_base_dir(), _config_file_path()


def _load_config():
    base, config_file = _app_paths()
    cfg = {}
    if os.path.exists(config_file):
        with open(config_file, encoding="utf-8") as f:
            cfg = json.load(f)

    decrypted_dir = cfg.get("wxwork_decrypted_dir", "wxwork_decrypted")
    if not os.path.isabs(decrypted_dir):
        decrypted_dir = os.path.join(base, decrypted_dir)

    output_dir = cfg.get("wxwork_export_dir", "wxwork_export")
    if not os.path.isabs(output_dir):
        output_dir = os.path.join(base, output_dir)

    db_dir = cfg.get("wxwork_db_dir", "")
    return {
        "base": base,
        "decrypted_dir": decrypted_dir,
        "output_dir": output_dir,
        "self_id": _infer_self_id(db_dir),
    }


def _infer_self_id(db_dir):
    if not db_dir:
        return None
    parts = os.path.normpath(db_dir).split(os.sep)
    for part in reversed(parts):
        if part.isdigit() and len(part) >= 10:
            return int(part)
    return None


def _safe_dirname(name):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(name))
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name or "unknown")[:120]


def _table_exists(conn, table):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _open_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _load_user_map(decrypted_dir):
    user_db = os.path.join(decrypted_dir, "user.db")
    users = {}
    if not os.path.exists(user_db):
        return users

    conn = _open_db(user_db)
    try:
        if _table_exists(conn, "user_table"):
            for row in conn.execute(
                "SELECT id, name, real_name, account, external_corp_name, external_job "
                "FROM user_table"
            ):
                name = row["real_name"] or row["name"] or row["account"] or ""
                if row["external_corp_name"] and row["external_corp_name"] not in name:
                    name = f"{name} ({row['external_corp_name']})" if name else row["external_corp_name"]
                if name:
                    users[int(row["id"])] = name

        if _table_exists(conn, "external_user_relation_v3"):
            for row in conn.execute(
                "SELECT user_id, remarks, real_remarks, corp_remark FROM external_user_relation_v3"
            ):
                name = row["real_remarks"] or row["remarks"] or row["corp_remark"] or ""
                if name:
                    users[int(row["user_id"])] = name
    finally:
        conn.close()
    return users


def _load_group_member_names(decrypted_dir):
    session_db = os.path.join(decrypted_dir, "session.db")
    members = defaultdict(dict)
    if not os.path.exists(session_db):
        return members

    conn = _open_db(session_db)
    try:
        if _table_exists(conn, "conversation_user_table"):
            for row in conn.execute(
                "SELECT conversation_id, user_id, nick_name FROM conversation_user_table"
            ):
                if row["nick_name"]:
                    members[row["conversation_id"]][int(row["user_id"])] = row["nick_name"]

        if _table_exists(conn, "conversation_member_nickname_table"):
            # 该表使用 room_id，需要用 conversation_table.con_numeric_id 转成会话 ID。
            room_map = {}
            if _table_exists(conn, "conversation_table"):
                for row in conn.execute("SELECT con_numeric_id, id FROM conversation_table"):
                    room_map[int(row["con_numeric_id"])] = row["id"]
            for row in conn.execute(
                "SELECT room_id, userid, nickname FROM conversation_member_nickname_table"
            ):
                cid = room_map.get(int(row["room_id"]))
                if cid and row["nickname"]:
                    members[cid][int(row["userid"])] = row["nickname"]
    finally:
        conn.close()
    return members


def _conversation_kind(conversation_id):
    if conversation_id.startswith("R:"):
        return "群聊"
    if conversation_id.startswith("S:"):
        return "单聊"
    if conversation_id.startswith("M:"):
        return "微信联系人"
    if conversation_id.startswith("O:"):
        return "应用/公众号"
    if conversation_id.startswith("Y:"):
        return "系统会话"
    return "其他"


def _name_from_conversation_id(conversation_id, user_map, self_id):
    if conversation_id.startswith("S:"):
        ids = []
        for value in conversation_id[2:].split("_"):
            if value.isdigit():
                ids.append(int(value))
        other_ids = [uid for uid in ids if self_id is None or uid != self_id]
        for uid in other_ids or ids:
            if uid in user_map:
                return user_map[uid]

    if ":" in conversation_id:
        tail = conversation_id.split(":", 1)[1]
        if tail.isdigit() and int(tail) in user_map:
            return user_map[int(tail)]

    return conversation_id


def _load_message_counts(decrypted_dir):
    msg_db = os.path.join(decrypted_dir, "message.db")
    counts = defaultdict(int)
    last_times = defaultdict(int)
    if not os.path.exists(msg_db):
        return counts, last_times

    conn = _open_db(msg_db)
    try:
        for table in _MESSAGE_TABLES:
            if not _table_exists(conn, table):
                continue
            for row in conn.execute(
                f'SELECT conversation_id, COUNT(*) AS c, MAX(send_time) AS t '
                f'FROM "{table}" GROUP BY conversation_id'
            ):
                cid = row["conversation_id"]
                if not cid:
                    continue
                counts[cid] += int(row["c"] or 0)
                last_times[cid] = max(last_times[cid], int(row["t"] or 0))
    finally:
        conn.close()
    return counts, last_times


def discover_conversations(decrypted_dir=None):
    cfg = _load_config()
    if decrypted_dir is None:
        decrypted_dir = cfg["decrypted_dir"]
    if not os.path.isdir(decrypted_dir):
        raise FileNotFoundError(f"企业微信解密目录不存在: {decrypted_dir}")

    user_map = _load_user_map(decrypted_dir)
    counts, message_last_times = _load_message_counts(decrypted_dir)
    session_db = os.path.join(decrypted_dir, "session.db")
    conversations = {}

    if os.path.exists(session_db):
        conn = _open_db(session_db)
        try:
            if _table_exists(conn, "conversation_table"):
                for row in conn.execute(
                    "SELECT id, name, roomname_remark, last_message_time, last_message_id "
                    "FROM conversation_table"
                ):
                    cid = row["id"]
                    if not cid:
                        continue
                    raw_name = row["roomname_remark"] or row["name"] or ""
                    display = raw_name or _name_from_conversation_id(
                        cid, user_map, cfg["self_id"]
                    )
                    last_time = max(
                        int(row["last_message_time"] or 0),
                        message_last_times.get(cid, 0),
                    )
                    conversations[cid] = {
                        "conversation_id": cid,
                        "display_name": display,
                        "kind": _conversation_kind(cid),
                        "message_count": counts.get(cid, 0),
                        "last_time": last_time,
                        "last_message_id": int(row["last_message_id"] or 0),
                    }
        finally:
            conn.close()

    for cid, count in counts.items():
        if cid in conversations:
            conversations[cid]["message_count"] = count
            conversations[cid]["last_time"] = max(
                conversations[cid]["last_time"], message_last_times.get(cid, 0)
            )
            continue
        conversations[cid] = {
            "conversation_id": cid,
            "display_name": _name_from_conversation_id(cid, user_map, cfg["self_id"]),
            "kind": _conversation_kind(cid),
            "message_count": count,
            "last_time": message_last_times.get(cid, 0),
            "last_message_id": 0,
        }

    result = [c for c in conversations.values() if c["message_count"] > 0]
    result.sort(key=lambda c: (c["last_time"], c["message_count"]), reverse=True)
    return result


def _read_varint(data, pos):
    value = 0
    shift = 0
    while pos < len(data) and shift < 64:
        b = data[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not (b & 0x80):
            return value, pos
        shift += 7
    raise ValueError("bad varint")


def _clean_text(text):
    text = "".join(
        ch if ch in "\n\t" or (ch.isprintable() and ch not in "\x0b\x0c") else " "
        for ch in text
    )
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _looks_like_plain_text(data, text):
    if not text:
        return False
    control = sum(1 for b in data if b < 32 and b not in (9, 10, 13))
    if control / max(len(data), 1) > 0.08:
        return False
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\n\t")
    return printable / max(len(text), 1) > 0.9


def _decode_text_segment(segment):
    if not segment or b"\x00" in segment:
        return None
    try:
        text = segment.decode("utf-8")
    except UnicodeDecodeError:
        return None
    text = _clean_text(text)
    if len(text) < 2:
        return None
    if re.fullmatch(r"[0-9a-fA-F]{32,}", text):
        return None
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\n\t")
    if printable / max(len(text), 1) < 0.9:
        return None
    return text


def _parse_protobuf_strings(data, depth=0):
    if depth > 4 or not data:
        return []
    pos = 0
    out = []
    fields = 0
    try:
        while pos < len(data):
            tag, pos = _read_varint(data, pos)
            if tag == 0:
                return []
            wire = tag & 7
            fields += 1
            if wire == 0:
                _, pos = _read_varint(data, pos)
            elif wire == 1:
                pos += 8
            elif wire == 5:
                pos += 4
            elif wire == 2:
                length, pos = _read_varint(data, pos)
                if length < 0 or pos + length > len(data):
                    return []
                segment = data[pos:pos + length]
                pos += length
                text = _decode_text_segment(segment)
                if text:
                    out.append(text)
                else:
                    out.extend(_parse_protobuf_strings(segment, depth + 1))
            else:
                return []
            if pos > len(data):
                return []
    except Exception:
        return []
    return out if fields else []


def _dedupe_texts(values):
    seen = set()
    out = []
    for value in values:
        value = _clean_text(value)
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def decode_content(raw):
    if raw is None:
        return ""
    if isinstance(raw, str):
        return _clean_text(raw)
    data = bytes(raw)
    if not data:
        return ""

    try:
        plain = data.decode("utf-8")
        if _looks_like_plain_text(data, plain):
            return _clean_text(plain)
    except UnicodeDecodeError:
        pass

    texts = _dedupe_texts(_parse_protobuf_strings(data))
    if texts:
        return "\n".join(texts[:12])

    for enc in ("utf-8", "gbk", "utf-16le"):
        try:
            text = _clean_text(data.decode(enc, errors="replace"))
            if text and "\ufffd" not in text[:20]:
                return text[:2000]
        except Exception:
            continue
    return f"[二进制内容 {len(data)} 字节]"


def _format_time(ts):
    try:
        ts = int(ts or 0)
    except (TypeError, ValueError):
        ts = 0
    if ts <= 0:
        return ""
    if ts > 20_000_000_000:
        ts = ts / 1000
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _message_type_name(content_type):
    return MSG_TYPES.get(int(content_type or 0), f"未知({content_type})")


def _display_message_content(content_type, content, extra_content, local_extra_content):
    text = content or extra_content or local_extra_content
    if text:
        return text
    return f"[{_message_type_name(content_type)}]"


def _build_message(row, conv_map, user_map, member_names, self_id):
    cid = row["conversation_id"]
    sender_id = int(row["sender_id"] or 0)
    sender = member_names.get(cid, {}).get(sender_id) or user_map.get(sender_id)
    if self_id is not None and sender_id == self_id:
        sender = "我"
    if not sender:
        sender = str(sender_id) if sender_id else "系统"

    content = decode_content(row["content"])
    extra_content = decode_content(row["extra_content"])
    local_extra_content = decode_content(row["local_extra_content"])
    content_type = int(row["content_type"] or 0)
    conv = conv_map.get(cid, {})

    return {
        "source_table": row["source_table"],
        "message_id": int(row["message_id"] or 0),
        "server_id": int(row["server_id"] or 0),
        "sequence": int(row["sequence"] or 0),
        "conversation_id": cid,
        "conversation": conv.get("display_name") or cid,
        "conversation_kind": conv.get("kind") or _conversation_kind(cid),
        "sender_id": sender_id,
        "sender": sender,
        "content_type": content_type,
        "type_name": _message_type_name(content_type),
        "send_time": int(row["send_time"] or 0),
        "time": _format_time(row["send_time"]),
        "flag": int(row["flag"] or 0),
        "content": content,
        "extra_content": extra_content,
        "local_extra_content": local_extra_content,
        "display_content": _display_message_content(
            content_type, content, extra_content, local_extra_content
        ),
        "is_sent": self_id is not None and sender_id == self_id,
    }


def _iter_message_rows(message_db, selected_ids=None):
    selected_ids = set(selected_ids or [])
    conn = _open_db(message_db)
    try:
        for table in _MESSAGE_TABLES:
            if not _table_exists(conn, table):
                continue
            where = ""
            params = []
            if selected_ids:
                placeholders = ",".join("?" for _ in selected_ids)
                where = f"WHERE conversation_id IN ({placeholders})"
                params = list(selected_ids)
            sql = (
                f'SELECT "{table}" AS source_table, message_id, server_id, sequence, '
                f"sender_id, conversation_id, content_type, send_time, flag, "
                f"content, extra_content, local_extra_content "
                f'FROM "{table}" {where} '
                f"ORDER BY send_time, sequence, message_id"
            )
            yield from conn.execute(sql, params)
    finally:
        conn.close()


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:#f4f4f2;color:#1f2328;font-family:"Microsoft YaHei UI","PingFang SC",Arial,sans-serif;font-size:14px}}
.header{{position:sticky;top:0;background:#1f6f50;color:#fff;padding:12px 18px;font-weight:700;box-shadow:0 1px 4px rgba(0,0,0,.18)}}
.meta{{font-weight:400;font-size:12px;opacity:.86;margin-top:3px}}
.chat{{max-width:880px;margin:0 auto;padding:12px 10px 24px}}
.day{{text-align:center;color:#777;font-size:12px;margin:14px 0 8px}}
.day span{{background:#deded8;border-radius:10px;padding:2px 10px}}
.msg{{display:flex;align-items:flex-start;gap:8px;margin:8px 0}}
.msg.sent{{flex-direction:row-reverse}}
.avatar{{width:36px;height:36px;border-radius:6px;background:#4977a8;color:#fff;display:flex;align-items:center;justify-content:center;font-weight:700;flex:0 0 36px}}
.msg.sent .avatar{{background:#2b8a57}}
.body{{max-width:72%}}
.sender{{font-size:12px;color:#666;margin:0 0 3px 2px}}
.msg.sent .sender{{text-align:right;margin-right:2px}}
.bubble{{white-space:pre-wrap;word-break:break-word;line-height:1.55;background:#fff;border-radius:6px;padding:8px 11px;box-shadow:0 1px 2px rgba(0,0,0,.08)}}
.msg.sent .bubble{{background:#b9ed9b}}
.type{{font-size:11px;color:#888;margin-top:2px}}
</style>
</head>
<body>
<div class="header">{title}<div class="meta">{meta}</div></div>
<div class="chat">
{body}
</div>
</body>
</html>
"""


def _write_html(path, conv, messages):
    parts = []
    last_day = None
    is_group = conv.get("kind") == "群聊"
    for msg in messages:
        day = msg["time"][:10] if msg["time"] else ""
        if day and day != last_day:
            parts.append(f'<div class="day"><span>{escape(day)}</span></div>')
            last_day = day

        side = "sent" if msg["is_sent"] else "received"
        sender_label = ""
        if is_group or not msg["is_sent"]:
            sender_label = f'<div class="sender">{escape(msg["sender"])}</div>'
        initial = escape((msg["sender"] or "?")[0].upper())
        content = escape(msg["display_content"] or "")
        type_line = escape(f'{msg["type_name"]} · {msg["time"]}')
        parts.append(
            f'<div class="msg {side}">'
            f'<div class="avatar">{initial}</div>'
            f'<div class="body">{sender_label}'
            f'<div class="bubble">{content}</div>'
            f'<div class="type">{type_line}</div>'
            f'</div></div>'
        )

    meta = f'{conv.get("kind", "")} · {len(messages)} 条消息 · {conv["conversation_id"]}'
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            HTML_TEMPLATE.format(
                title=escape(conv["display_name"]),
                meta=escape(meta),
                body="\n".join(parts),
            )
        )


def _write_csv(path, messages):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "时间", "会话", "会话ID", "发送者", "发送者ID", "消息类型",
            "内容", "message_id", "server_id", "sequence", "flag",
        ])
        for msg in messages:
            writer.writerow([
                msg["time"],
                msg["conversation"],
                msg["conversation_id"],
                msg["sender"],
                msg["sender_id"],
                msg["type_name"],
                msg["display_content"],
                msg["message_id"],
                msg["server_id"],
                msg["sequence"],
                msg["flag"],
            ])


def _write_json(path, conv, messages):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "conversation": conv,
                "message_count": len(messages),
                "messages": messages,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )


def _selected_from_env():
    raw = os.environ.get("WXWORK_EXPORT_CONVERSATIONS", "").strip()
    if not raw:
        return None
    return {item.strip() for item in raw.split(",") if item.strip()}


def _formats_from_env():
    raw = os.environ.get("WXWORK_EXPORT_FORMATS", "").strip()
    if not raw:
        return {"csv"}
    formats = {item.strip().lower() for item in raw.split(",") if item.strip()}
    valid = {"csv", "html", "json"}
    return formats & valid or {"csv"}


def export_messages(selected_ids=None, formats=None):
    cfg = _load_config()
    decrypted_dir = cfg["decrypted_dir"]
    output_dir = cfg["output_dir"]
    message_db = os.path.join(decrypted_dir, "message.db")

    if not os.path.isdir(decrypted_dir):
        raise FileNotFoundError(f"企业微信解密目录不存在: {decrypted_dir}")
    if not os.path.exists(message_db):
        raise FileNotFoundError(f"企业微信消息库不存在: {message_db}")

    formats = formats or _formats_from_env()
    selected_ids = selected_ids if selected_ids is not None else _selected_from_env()
    conversations = discover_conversations(decrypted_dir)
    conv_map = {conv["conversation_id"]: conv for conv in conversations}
    if selected_ids:
        missing = sorted(selected_ids - set(conv_map))
        if missing:
            print(f"提示: {len(missing)} 个选择的会话没有消息或不存在")

    user_map = _load_user_map(decrypted_dir)
    member_names = _load_group_member_names(decrypted_dir)
    grouped = defaultdict(list)
    seen = set()

    for row in _iter_message_rows(message_db, selected_ids):
        key = (row["conversation_id"], row["message_id"], row["server_id"], row["sequence"])
        if key in seen:
            continue
        seen.add(key)
        msg = _build_message(row, conv_map, user_map, member_names, cfg["self_id"])
        grouped[msg["conversation_id"]].append(msg)

    os.makedirs(output_dir, exist_ok=True)

    total_conversations = 0
    total_messages = 0
    for cid, messages in sorted(
        grouped.items(),
        key=lambda item: (conv_map.get(item[0], {}).get("last_time", 0), len(item[1])),
        reverse=True,
    ):
        conv = conv_map.get(cid) or {
            "conversation_id": cid,
            "display_name": cid,
            "kind": _conversation_kind(cid),
            "message_count": len(messages),
            "last_time": messages[-1]["send_time"] if messages else 0,
        }
        folder = _safe_dirname(f'{conv["display_name"]}_{cid}')
        out_dir = os.path.join(output_dir, folder)
        os.makedirs(out_dir, exist_ok=True)

        info_path = os.path.join(out_dir, ".info")
        with open(info_path, "w", encoding="utf-8") as f:
            f.write(f"conversation_id: {cid}\n")
            f.write(f"display_name: {conv['display_name']}\n")
            f.write(f"kind: {conv.get('kind', '')}\n")
            f.write(f"message_count: {len(messages)}\n")

        if "csv" in formats:
            _write_csv(os.path.join(out_dir, "messages.csv"), messages)
        if "html" in formats:
            _write_html(os.path.join(out_dir, "messages.html"), conv, messages)
        if "json" in formats:
            _write_json(os.path.join(out_dir, "messages.json"), conv, messages)

        total_conversations += 1
        total_messages += len(messages)
        print(f"  {conv['display_name']} ({cid}): {len(messages)} 条")

    print(f"\n完成: {total_conversations} 个会话, 共 {total_messages} 条消息")
    print(f"输出目录: {os.path.abspath(output_dir)}")
    return {
        "conversation_count": total_conversations,
        "message_count": total_messages,
        "output_dir": os.path.abspath(output_dir),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Export WXWork messages")
    parser.add_argument("--list", action="store_true", help="list conversations and exit")
    parser.add_argument(
        "--conversation",
        action="append",
        help="conversation ID to export; can be passed multiple times",
    )
    parser.add_argument("--formats", help="comma separated formats: csv,html,json")
    args = parser.parse_args(argv)

    if args.list:
        conversations = discover_conversations()
        print(f"发现 {len(conversations)} 个有消息的企业微信会话")
        for conv in conversations:
            last_time = _format_time(conv["last_time"])
            print(
                f"{conv['conversation_id']}\t{conv['message_count']}\t"
                f"{last_time}\t{conv['kind']}\t{conv['display_name']}"
            )
        return 0

    selected = set(args.conversation) if args.conversation else None
    formats = None
    if args.formats:
        formats = {item.strip().lower() for item in args.formats.split(",") if item.strip()}
    export_messages(selected_ids=selected, formats=formats)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"导出失败: {exc}", file=sys.stderr)
        sys.exit(1)
