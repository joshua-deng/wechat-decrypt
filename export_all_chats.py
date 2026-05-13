#!/usr/bin/env python3
"""批量导出所有微信聊天记录为 JSON 文件，可选附带语音转录。

此脚本将导出所有会话的聊天记录，输出格式与 export_chat.py 完全一致。
支持导出到指定目录，默认输出到 ./exported_chats 目录。

语音转录通过 mcp_server 的 backend 配置驱动（config.json 中设置
transcription_backend 为 whisper_cpp / openai / local）。未启用 backend
或缺少依赖时仅导出文本消息，不报错。

用法:
    python3 export_all_chats.py                         # 全量导出所有会话
    python3 export_all_chats.py --with-transcriptions   # 全量导出 + 转录语音
    python3 export_all_chats.py -i                      # 增量（只导出最新消息）
    python3 export_all_chats.py --start 2025-01-01      # 按日期范围
    python3 export_all_chats.py --end 2025-01-31
    python3 export_all_chats.py --start 2025-01-01 --end 2025-01-31 -t
"""

import argparse
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

    # 构造输出路径
    prefix = "group" if ctx["is_group"] else "single"
    safe = re.sub(r'[\\/:*?"<>|]', "_", f"{prefix}_{display_name}")
    out_path = os.path.join(output_dir, f"{safe}.json")

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
        "messages": messages,
    }
    if ctx["is_group"]:
        output["is_group"] = True

    os.makedirs(os.path.dirname(out_path) if os.path.dirname(out_path) else ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

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


def main():
    parser = argparse.ArgumentParser(
        description="批量导出所有微信聊天记录为 JSON 文件，可选附带语音转录",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    python3 export_all_chats.py                           全量导出所有会话
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
        help="预览模式：显示将导出的会话数和新消息数，不实际写入",
    )
    args = parser.parse_args()

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
    os.makedirs(output_dir, exist_ok=True)

    session_db = os.path.join(mcp_server.DECRYPTED_DIR, "session", "session.db")
    try:
        with closing(sqlite3.connect(session_db)) as conn:
            sessions = [u for u, _ in conn.execute(
                "SELECT username, type FROM SessionTable"
            )]
    except sqlite3.Error as e:
        print(f"会话数据库查询失败: {e}", file=sys.stderr)
        sys.exit(1)

    names = mcp_server.get_contact_names()

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

    t0 = time.time()
    ok, skip, err, total = 0, 0, 0, 0
    total_new = 0

    iterable = _tqdm(sessions, desc="导出进度") if _tqdm else sessions
    for i, username in enumerate(iterable, 1):
        display = names.get(username, username)
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
            if not _tqdm:
                if i <= 10 or i % 100 == 0 or new_msgs > 0:
                    elapsed = time.time() - t0
                    eta = (elapsed / i) * (len(sessions) - i) if i > 0 else 0
                    print(
                        f"[{i}/{len(sessions)}] {display} - {label}"
                        + (f"  ETA {eta/60:.0f}分" if i > 1 else "")
                    )
        else:
            if "no tables" in str(reason) or "empty" in str(reason):
                skip += 1
                if not _tqdm:
                    if i <= 10 or i % 50 == 0:
                        print(f"[{i}/{len(sessions)}] {display} - 跳过({reason})")
            else:
                err += 1
                if not _tqdm:
                    print(f"[{i}/{len(sessions)}] {display} - 失败: {reason}")
                elif _tqdm:
                    _tqdm.write(f"失败: {display} - {reason}")

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
