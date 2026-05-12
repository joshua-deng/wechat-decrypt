"""
为聊天导出 JSON 中的语音消息补齐转录文本。

用法:
    .venv/bin/python3 transcribe_chat.py <input.json> [output.json]

参数:
    <input.json>   由 export_chat.py 产出的 JSON。
    [output.json]  可选输出路径，默认 "<input>_transcribed.json"。

完整流程示例:
    .venv/bin/python3 export_chat.py <chat_name> /tmp/chat.json
    .venv/bin/python3 transcribe_chat.py /tmp/chat.json /tmp/chat_transcribed.json

行为说明:
    - 后端由 config.json 中 transcription_backend 字段控制 (local/openai/whisper_cpp)，
      与 MCP transcribe_voice 工具共享配置。详见 README "语音转录隐私" 章节。
    - 默认 local: 使用本地 Whisper (CPU，单线程)，首次运行下载 ~145 MB 权重。
    - 切到 openai: 语音上传至 OpenAI 服务器转录 (~$0.006/分钟)。
    - 切到 whisper_cpp: 使用 whisper-cpp CLI (Metal GPU 加速，仅 macOS)。
    - 幂等: 已有 "transcription" 字段的消息会被跳过，因此崩溃/中断后可安全重跑。
    - 崩溃安全: 每处理完一条即整体重写输出 JSON，进程中断最多丢失当前一条。

需要 WeChat DB 仍然在线/已解密 —— 语音 blob 是从 DB 现场按 local_id 读取的，
不从 JSON 读。
"""
import json
import os
import sys
from datetime import datetime

import mcp_server


def _transcribe_local_id(username, local_id, backend):
    row = mcp_server._fetch_voice_row(username, local_id)
    if row is None:
        return "[not found]"

    voice_data, create_time = row
    try:
        wav_path, _ = mcp_server._silk_to_wav(voice_data, create_time, username, local_id)
    except Exception as e:
        return f"[decode error: {e}]"

    try:
        result = mcp_server._transcribe(wav_path, backend)
        return result["text"]
    except Exception as e:
        return f"[transcribe error: {e}]"


def transcribe_export(input_path, output_path):
    with open(input_path, encoding="utf-8") as f:
        data = json.load(f)

    # 优先使用导出 JSON 中已记录的 username，避免重新模糊匹配导致同名联系人漂移。
    username = data.get("username")
    chat_name = data.get("chat", "")
    if not username:
        username = mcp_server.resolve_username(chat_name)
    if not username:
        print(f"Could not resolve username for: {chat_name}")
        sys.exit(1)

    messages = data["messages"]
    # Compact format: type is absent for text; transcription is only present when filled.
    pending = [m for m in messages if m.get("type") == "voice" and not m.get("transcription")]
    total = len(pending)

    if total == 0:
        print("No voice messages to transcribe.")
        return

    backend = mcp_server._resolve_active_backend()
    print(f"Found {total} voice messages to transcribe.")
    print(f"Backend: {backend}")
    if backend == "local":
        print("Loading Whisper model (first run downloads ~145MB)...")
        mcp_server._get_whisper_model()
        print("Model ready.\n")
    elif backend == "whisper_cpp":
        print("Using whisper-cpp with Metal GPU acceleration\n")
    else:
        print("")

    for i, msg in enumerate(pending, 1):
        local_id = msg["local_id"]
        ts = msg["timestamp"]
        ts_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if isinstance(ts, (int, float)) else ts
        print(f"[{i}/{total}] local_id={local_id} ({ts_str}) ... ", end="", flush=True)
        result = _transcribe_local_id(username, local_id, backend)
        msg["transcription"] = result
        print(repr(result[:60]) if result else '""')

        # Save after each transcription so progress isn't lost on crash
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"\nDone. Written to {output_path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 transcribe_chat.py <input.json> [output.json]")
        sys.exit(1)

    inp = sys.argv[1]
    base, ext = os.path.splitext(inp)
    out = sys.argv[2] if len(sys.argv) > 2 else f"{base}_transcribed{ext}"
    transcribe_export(inp, out)
