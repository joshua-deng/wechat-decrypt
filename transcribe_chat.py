"""
Fill in transcriptions for voice messages in a chat export JSON.

Usage:
    .venv/bin/python3 transcribe_chat.py <input.json> [output.json]

Arguments:
    <input.json>   JSON file produced by export_chat.py.
    [output.json]  Optional output path. Defaults to "<input>_transcribed.json".

Example (full workflow):
    .venv/bin/python3 export_chat.py <chat_name> /tmp/chat.json
    .venv/bin/python3 transcribe_chat.py /tmp/chat.json /tmp/chat_transcribed.json

Behavior:
    - Transcribes each voice message via OpenAI Whisper (CPU, single-threaded).
    - Idempotent: messages that already have a "transcription" field are skipped,
      so re-running after a crash or on a partially-transcribed file is safe.
    - Crash-safe: the output JSON is rewritten after every message, so progress
      is preserved if the process is interrupted.
    - First run downloads the Whisper model (~145 MB) and caches it.

Requires the WeChat DBs to still be present/decrypted — the voice blobs are
re-read from the DB (not from the export JSON).
"""
import io
import json
import os
import sys
import wave

import mcp_server


def _transcribe_local_id(username, local_id):
    row = mcp_server._fetch_voice_row(username, local_id)
    if row is None:
        return "[not found]"

    voice_data, create_time = row
    try:
        wav_path, _ = mcp_server._silk_to_wav(voice_data, create_time, username, local_id)
    except Exception as e:
        return f"[decode error: {e}]"

    try:
        model = mcp_server._get_whisper_model()
        result = model.transcribe(wav_path)
        return result.get("text", "").strip()
    except Exception as e:
        return f"[transcribe error: {e}]"


def transcribe_export(input_path, output_path):
    with open(input_path, encoding="utf-8") as f:
        data = json.load(f)

    chat_name = data["chat"]
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

    print(f"Found {total} voice messages to transcribe.")
    print("Loading Whisper model (first run downloads ~145MB)...")
    mcp_server._get_whisper_model()
    print("Model ready.\n")

    for i, msg in enumerate(pending, 1):
        local_id = msg["local_id"]
        import datetime as _dt
        ts = msg["timestamp"]
        ts_str = _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if isinstance(ts, (int, float)) else ts
        print(f"[{i}/{total}] local_id={local_id} ({ts_str}) ... ", end="", flush=True)
        result = _transcribe_local_id(username, local_id)
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
