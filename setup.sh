#!/usr/bin/env bash
# setup.sh — 一键安装所有依赖 + 编译 + 初始配置
# 幂等（可重复运行）。适用 macOS / Linux / Windows (Git Bash / WSL)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "========================================================"
echo "  WeChat Decrypt — 环境配置"
echo "========================================================"

# ── 检测平台 ──────────────────────────────────────────────────
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
case "$OS" in
    darwin)  PLATFORM="macos"  ;;
    linux)   PLATFORM="linux"  ;;
    mingw*|msys*|cygwin*) PLATFORM="windows" ;;
    *)       echo "未识别的平台: $OS"; exit 1 ;;
esac
echo "[平台] $PLATFORM"

# ── Python / venv ────────────────────────────────────────────
PYTHON="python3"
if command -v python3 &>/dev/null; then
    PYTHON="python3"
elif command -v python &>/dev/null; then
    PYTHON="python"
else
    echo "[错误] 未找到 Python 3。请安装: https://python.org"
    exit 1
fi

if [ ! -d .venv ]; then
    echo "[venv] 创建虚拟环境..."
    "$PYTHON" -m venv .venv
else
    echo "[venv] 虚拟环境已存在"
fi

# 激活 venv（跨平台兼容）
if [ "$PLATFORM" = "windows" ]; then
    VENV_PY=".venv/Scripts/python.exe"
else
    VENV_PY=".venv/bin/python3"
fi

if [ ! -f "$VENV_PY" ]; then
    echo "[错误] venv Python 未找到: $VENV_PY"
    exit 1
fi

echo "[pip] 安装 Python 依赖..."
"$VENV_PY" -m pip install --upgrade pip -q
"$VENV_PY" -m pip install -r requirements.txt -q
echo "[pip] 完成"

# ── macOS 特有 ────────────────────────────────────────────────
if [ "$PLATFORM" = "macos" ]; then
    echo ""
    echo "[macOS] ---"

    # Xcode CLT
    if ! xcode-select -p &>/dev/null; then
        echo "[xcode] 安装 Command Line Tools..."
        xcode-select --install || true
        echo "[xcode] 安装完成后请重新运行 setup.sh"
        exit 0
    else
        echo "[xcode] ✓"
    fi

    # whisper-cpp
    if command -v brew &>/dev/null; then
        if ! command -v whisper-cpp &>/dev/null; then
            echo "[whisper-cpp] 通过 Homebrew 安装..."
            brew install whisper-cpp
        else
            echo "[whisper-cpp] ✓ 已安装"
        fi
    else
        echo "[brew] 未安装 Homebrew，跳过 whisper-cpp 自动安装"
        echo "       手动安装: brew install whisper-cpp"
    fi

    # 编译 C 扫描器
    if [ ! -f find_all_keys_macos ]; then
        echo "[编译] find_all_keys_macos..."
        cc -O2 -o find_all_keys_macos find_all_keys_macos.c -framework Foundation 2>/dev/null && \
            codesign -s - find_all_keys_macos 2>/dev/null && \
            echo "[编译] ✓" || echo "[编译] 跳过（c 源文件不存在？）"
    else
        echo "[编译] find_all_keys_macos 已存在（重新编译: make build）"
    fi

    # 微信重签名提示
    echo ""
    echo "[注意] 首次使用需要重签名微信:"
    echo "  killall WeChat"
    echo "  sudo codesign --force --deep --sign - /Applications/WeChat.app"
fi

# ── Linux 特有 ────────────────────────────────────────────────
if [ "$PLATFORM" = "linux" ]; then
    echo ""
    echo "[Linux] 需要 root 或 CAP_SYS_PTRACE 来扫描微信进程内存。"
    echo "        运行密钥提取时使用: sudo python3 find_all_keys.py"
fi

# ── config.json ───────────────────────────────────────────────
if [ ! -f config.json ]; then
    echo ""
    echo "[config] 生成 config.json 模板..."
    cat > config.json << 'CONFIG_EOF'
{
    "db_dir": "/path/to/your/wxid/db_storage",
    "keys_file": "all_keys.json",
    "decrypted_dir": "decrypted",
    "wechat_process": "WeChat",
    "__comment_db_dir": "各平台默认路径见 README.md"
}
CONFIG_EOF
    echo "[config] 已生成，请编辑 config.json 中的 db_dir 路径"
else
    echo "[config] 已存在（跳过）"
fi

# ── 完成 ──────────────────────────────────────────────────────
echo ""
echo "========================================================"
echo "  配置完成！下一步："
echo ""
echo "  1. 编辑 config.json 确认 db_dir 路径"
echo "  2. 提取密钥并解密："
echo "     macOS:   sudo ./find_all_keys_macos  &&  $VENV_PY decrypt_db.py"
echo "     Linux:   sudo $VENV_PY find_all_keys.py  &&  $VENV_PY decrypt_db.py"
echo "     Windows: python find_all_keys.py  &&  python decrypt_db.py"
echo ""
echo "  3. 批量导出聊天记录："
echo "     $VENV_PY export_all_chats.py"
echo ""
echo "  或使用 Makefile:  make decrypt / make all"
echo "========================================================"