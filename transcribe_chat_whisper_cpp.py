#!/usr/bin/env python3
"""为聊天导出 JSON 中的语音消息补齐转录文本（macOS whisper.cpp 版本）。

用法:
    .venv/bin/python3 transcribe_chat_whisper_cpp.py <input.json> [output.json]

参数:
    <input.json>   由 export_chat.py 产出的 JSON。
    [output.json]  可选输出路径，默认 "<input>_transcribed.json"。
    -m, --model     whisper.cpp 模型路径 (默认: 自动检测或 ~/whisper-models/ggml-base.bin)
    -l, --language  语言代码 (默认: zh)
    -t, --threads   线程数 (默认: 自动)

与 transcribe_chat.py 的区别:
    - 后端: whisper-cpp CLI (Metal/ANE 加速) 代替 openai/whisper Python 包
    - 无需 PyTorch，无 Python whisper 依赖
    - 在 Apple Silicon 上速度快 3-5x
    - 幂等: 已有 "transcription" 字段的消息会被跳过
    - 崩溃安全: 每处理完一条即整体重写输出 JSON

需要 WeChat DB 仍然在线/已解密 —— 语音 blob 是从 DB 现场按 local_id 读取的。
需要 whisper-cpp 已安装 (brew install whisper-cpp) 且模型已下载。
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

import mcp_server

# 默认模型搜索路径
_MODEL_SEARCH_PATHS = [
    os.path.expanduser("~/Library/Application Support/whisper-cpp"),
    os.path.expanduser("~/Library/Application Support/Recordly/whisper"),
    os.path.expanduser("~/whisper-models"),
    os.path.expanduser("~/models"),
    os.path.expanduser("~/Downloads"),
    "/opt/homebrew/share/whisper-cpp/models",
    "/usr/local/share/whisper-cpp/models",
]
_MODEL_SIZES = ["base", "small", "medium", "large-v3-turbo", "large-v3"]


def _find_whisper_cpp_binary():
    """查找 whisper-cpp 可执行文件"""
    paths = [
        "/opt/homebrew/bin/whisper-cpp",
        "/usr/local/bin/whisper-cpp",
        os.path.expanduser("~/.local/bin/whisper-cpp"),
    ]
    for p in paths:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _find_model(size="base"):
    """在常见位置查找 whisper.cpp 模型文件"""
    # 先查找指定 size
    patterns = [
        f"ggml-{size}.bin",
        f"ggml-{size}.en.bin",
        f"ggml-{size}-q5_0.bin",
        f"ggml-{size}.q5_0.bin",
    ]
    for search_dir in _MODEL_SEARCH_PATHS:
        if not os.path.isdir(search_dir):
            continue
        for pat in patterns:
            path = os.path.join(search_dir, pat)
            if os.path.isfile(path):
                return path

    # fallback: 任意 ggml-*.bin
    for search_dir in _MODEL_SEARCH_PATHS:
        if not os.path.isdir(search_dir):
            continue
        for f in os.listdir(search_dir):
            if f.startswith("ggml-") and f.endswith(".bin"):
                return os.path.join(search_dir, f)

    return None


def _download_model(size="base", models_dir=None):
    """使用 whisper-cpp 项目脚本下载模型"""
    if models_dir is None:
        models_dir = os.path.expanduser("~/whisper-models")
    os.makedirs(models_dir, exist_ok=True)

    url = (
        "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
        f"ggml-{size}.bin"
    )
    out_path = os.path.join(models_dir, f"ggml-{size}.bin")

    print(f"下载模型 {size} (~{_model_size_mb(size)} MB) 到 {out_path} ...")
    try:
        import urllib.request
        urllib.request.urlretrieve(url, out_path)
        print("完成。")
        return out_path
    except Exception as e:
        print(f"下载失败: {e}", file=sys.stderr)
        print(f"手动下载: curl -L '{url}' -o '{out_path}'", file=sys.stderr)
        return None


def _model_size_mb(size):
    return {
        "tiny": 78, "tiny.en": 78,
        "base": 148, "base.en": 148,
        "small": 488, "small.en": 488,
        "medium": 1530, "medium.en": 1530,
        "large-v1": 3090, "large-v2": 3090, "large-v3": 3090,
        "large-v3-turbo": 1620,
    }.get(size, "?")


def _detect_threads():
    """自动检测最优线程数 (Apple Silicon: 性能核心数)"""
    try:
        cpu_count = os.cpu_count() or 4
        return min(cpu_count, 8)
    except Exception:
        return 4


def _transcribe_local_id(username, local_id, model_path, language, threads):
    """使用 whisper-cpp 转录音频"""
    row = mcp_server._fetch_voice_row(username, local_id)
    if row is None:
        return "[not found]"

    voice_data, create_time = row
    try:
        wav_path, _ = mcp_server._silk_to_wav(voice_data, create_time, username, local_id)
    except Exception as e:
        return f"[decode error: {e}]"

    try:
        cmd = [
            _find_whisper_cpp_binary() or "whisper-cpp",
            "-m", model_path,
            "-f", wav_path,
            "-l", language,
            "-t", str(threads),
            "--no-fallback",
            "-otxt",
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        # whisper-cpp 把转录文本写入 {wav_path}.txt
        txt_path = f"{wav_path}.txt"
        if os.path.isfile(txt_path):
            with open(txt_path, encoding="utf-8") as f:
                text = f.read().strip()
            os.unlink(txt_path)
            return text or ""
        # 如果没生成文件，从 stdout 提取
        if result.stdout.strip():
            return result.stdout.strip()
        if result.stderr.strip():
            print(f"  [whisper stderr: {result.stderr.strip()[:80]}]", file=sys.stderr)
        return ""
    except subprocess.TimeoutExpired:
        return "[timeout]"
    except Exception as e:
        return f"[transcribe error: {e}]"


def transcribe_export(input_path, output_path, model_path, language, threads):
    with open(input_path, encoding="utf-8") as f:
        data = json.load(f)

    username = data.get("username")
    chat_name = data.get("chat", "")
    if not username:
        username = mcp_server.resolve_username(chat_name)
    if not username:
        print(f"Could not resolve username for: {chat_name}")
        sys.exit(1)

    messages = data["messages"]
    pending = [m for m in messages
               if m.get("type") == "voice"
               and not m.get("transcription")]

    total = len(pending)
    if total == 0:
        print("No voice messages to transcribe.")
        return

    print(f"Found {total} voice messages to transcribe.")
    print(f"Model: {model_path}")
    print(f"Language: {language}")
    print(f"Using whisper-cpp with Metal acceleration")
    print()

    for i, msg in enumerate(pending, 1):
        local_id = msg["local_id"]
        ts = msg.get("timestamp")
        if isinstance(ts, (int, float)):
            ts_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        else:
            ts_str = str(ts) if ts else "?"
        print(f"[{i}/{total}] local_id={local_id} ({ts_str}) ... ", end="", flush=True)
        result = _transcribe_local_id(username, local_id, model_path, language, threads)
        msg["transcription"] = result
        print(repr(result[:60]) if result else '""')

        # 每处理完一条即保存，崩溃安全
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"\nDone. Written to {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="为聊天导出 JSON 中的语音消息补齐转录文本 (whisper.cpp)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    python3 transcribe_chat_whisper_cpp.py /tmp/chat.json
    python3 transcribe_chat_whisper_cpp.py /tmp/chat.json -m ~/models/ggml-base.bin
    python3 transcribe_chat_whisper_cpp.py /tmp/chat.json -l en -t 4
        """,
    )
    parser.add_argument("input", help="输入 JSON 路径 (export_chat.py 产出)")
    parser.add_argument("output", nargs="?", default=None,
                        help="输出 JSON 路径 (默认: <input>_transcribed.json)")
    parser.add_argument("-m", "--model", default=None,
                        help="whisper.cpp 模型路径 (默认: 自动检测)")
    parser.add_argument("-l", "--language", default="zh",
                        help="语言代码 (默认: zh)")
    parser.add_argument("-t", "--threads", type=int, default=None,
                        help="线程数 (默认: 自动)")
    parser.add_argument("--model-size", default=None,
                        help="自动下载指定大小的模型 (base/small/medium/large-v3)")
    args = parser.parse_args()

    # 检查 whisper-cpp 是否安装
    binary = _find_whisper_cpp_binary()
    if binary is None:
        print("错误: 未找到 whisper-cpp。请运行: brew install whisper-cpp",
              file=sys.stderr)
        sys.exit(1)

    # 确定模型路径
    model_path = args.model
    if model_path is None:
        model_path = _find_model(args.model_size or "base")
    if model_path is None:
        if args.model_size:
            model_path = _download_model(args.model_size)
        else:
            print("错误: 未找到 whisper.cpp 模型。", file=sys.stderr)
            print("选项:", file=sys.stderr)
            print("  1. 指定路径: -m ~/whisper-models/ggml-base.bin", file=sys.stderr)
            print("  2. 自动下载: --model-size base", file=sys.stderr)
            sys.exit(1)

    if not os.path.isfile(model_path):
        print(f"错误: 模型文件不存在: {model_path}", file=sys.stderr)
        sys.exit(1)

    # 输入输出路径
    inp = args.input
    if not os.path.isfile(inp):
        print(f"错误: 输入文件不存在: {inp}", file=sys.stderr)
        sys.exit(1)

    base, ext = os.path.splitext(inp)
    out = args.output or f"{base}_transcribed{ext}"

    threads = args.threads or _detect_threads()

    transcribe_export(inp, out, model_path, args.language, threads)


if __name__ == "__main__":
    main()
