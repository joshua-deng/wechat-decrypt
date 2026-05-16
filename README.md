# WeChat 4.x Database Decryptor

微信 4.0 (Windows / macOS / Linux) 本地数据库解密工具。从运行中的微信进程内存提取加密密钥，解密所有 SQLCipher 4 加密数据库，并提供实时消息监听、MCP Server、批量导出和语音转录。

---

## ⭐ 快速开始

<details open>
<summary>macOS — 最小路径（展开查看）</summary>

```bash
# 1. 安装依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
brew install whisper-cpp           # 语音转录加速（可选，推荐）

# 2. 密钥提取（退出微信后先重签名）
killall WeChat
sudo codesign --force --deep --sign - /Applications/WeChat.app
cc -O2 -o find_all_keys_macos find_all_keys_macos.c -framework Foundation
sudo ./find_all_keys_macos         # 扫描内存提取密钥

# 3. 解密 + 导出 + 转录
python3 decrypt_db.py              # 解密所有数据库
python3 export_all_chats.py -t     # 导出全部聊天并转录语音

# 或一条命令从零到完成：
make all
```

</details>

<details>
<summary>Windows — 最小路径</summary>

```bash
# 1. 以管理员身份打开终端
# 2. 安装依赖
py -m pip install -r requirements.txt

# 3. 提取密钥 + 解密
python main.py decrypt

# 4. 批量导出
python export_all_chats.py
```

</details>

<details>
<summary>Linux — 最小路径</summary>

```bash
# 1. 安装依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. 提取密钥（需要 root 或 CAP_SYS_PTRACE）
sudo python3 main.py decrypt

# 3. 批量导出
python3 export_all_chats.py
```

</details>

---

## 📖 详细指南

### 环境要求

- Python 3.10+
- 微信 4.x 正在运行

**macOS**:
- Xcode Command Line Tools: `xcode-select --install`
- 需要对 `/Applications/WeChat.app` 做 ad-hoc 重签名（允许进程内存读取）
- 需要 root 权限运行扫描器

**Windows**:
- 管理员权限（读取进程内存）
- 微信正在运行

**Linux**:
- root 权限或 `CAP_SYS_PTRACE`
- 微信正在运行

### 安装依赖

```bash
pip install -r requirements.txt
```

<details>
<summary>⚠️ 安装失败？ 点击展开</summary>

**问题：`error: externally-managed-environment` (PEP 668)**

Homebrew Python (3.12+) 和部分 Linux 发行版禁止 `pip install` 直接写入系统 Python 环境。

**解决：使用虚拟环境**

```bash
python3 -m venv .venv
source .venv/bin/activate   # 激活虚拟环境
pip install -r requirements.txt

# 后续运行脚本时使用 .venv 中的 Python
.venv/bin/python3 main.py
.venv/bin/python3 decrypt_db.py
```

或使用 Makefile（已配置 `.venv/bin/python3`）：

```bash
make setup   # 一键安装所有依赖 + 编译扫描器
make decrypt # 等价于 .venv/bin/python3 main.py decrypt
make all     # 从密钥提取到导出全部完成
```

Windows 可改用：

```bash
py -m pip install --user -r requirements.txt
```

</details>

### 配置

程序会自动检测微信数据目录并生成 `config.json`。如果自动检测失败，手动创建：

```json
{
    "db_dir": "/path/to/your/wxid/db_storage",
    "keys_file": "all_keys.json",
    "decrypted_dir": "decrypted",
    "wechat_process": "WeChat"
}
```

各平台默认路径：
- macOS: `~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<wxid>/db_storage`
- Windows: 微信设置 → 文件管理中查看
- Linux: `~/Documents/xwechat_files/<wxid>/db_storage`

### 常用命令

| 用途 | 命令 |
|------|------|
| 提取密钥（macOS） | `sudo ./find_all_keys_macos` |
| 提取密钥（Windows/Linux） | `python find_all_keys.py` |
| 解密全部数据库 | `python decrypt_db.py` |
| 启动 Web UI（实时消息） | `python main.py` |
| 批量导出聊天记录 | `python export_all_chats.py` |
| 批量导出 + 语音转录 | `python export_all_chats.py --with-transcriptions` |
| 转录单个文件语音 | `python transcribe_chat.py input.json [output.json]` |
| 注册 MCP Server（Claude） | `claude mcp add wechat -- python /path/to/mcp_server.py` |

### Web UI

`python main.py` 启动后打开 http://localhost:5678 查看实时消息流。

- 30ms 轮询 WAL 文件变化
- SSE 实时推送到浏览器
- 图片消息内联预览

#### HTTP API

| 端点 | 说明 |
|------|------|
| `GET /api/history` | 最近消息列表 |
| `GET /api/history?chat=群名` | 按会话过滤 |
| `GET /api/history?since=1712000000` | 增量拉取 |
| `GET /api/tags` | 联系人标签 |
| `GET /stream` | SSE 实时消息推送 |

### MCP Server（Claude AI 集成）

将微信数据查询能力接入 Claude Code，让 AI 直接读取你的微信消息。

**注册：**

```bash
claude mcp add wechat -- python /path/to/mcp_server.py
```

**可用工具：**

| 工具 | 功能 |
|------|------|
| `get_recent_sessions(limit)` | 最近会话列表 |
| `get_chat_history(chat_name, limit, offset, start_time, end_time)` | 聊天记录 |
| `search_messages(keyword, chat_name, limit, offset, ...)` | 搜索消息 |
| `get_contacts(query, limit)` | 联系人搜索 |
| `get_contact_tags()` | 联系人标签 |
| `get_voice_messages(chat_name)` | 语音消息列表 |
| `decode_voice(chat_name, local_id)` | 解码语音为 WAV |
| `transcribe_voice(chat_name, local_id)` | 转录语音为文字 |

### ⚠️ 语音转录

`export_all_chats.py -t`、`transcribe_chat.py` 和 `transcribe_voice` MCP 工具共享同一套转录配置。

**后端对比：**

| 后端 | 速度 | 隐私 | 依赖 | 配置 |
|------|------|------|------|------|
| `local`（默认） | CPU，较慢 | 数据不出本机 | `pip install -r requirements.txt` | 无需配置 |
| `openai` | API，最快 | 语音上传至 OpenAI | `pip install openai` | 需 `openai_api_key` |
| `whisper_cpp` | Metal GPU，3-5x | 数据不出本机 | `brew install whisper-cpp` + 模型 | 自动检测 |

**配置方式（config.json）：**

```json
{
    "transcription_backend": "whisper_cpp"
}
```

启用 whisper_cpp 前需安装：

```bash
brew install whisper-cpp
# 模型自动检测常见路径，或手动下载：
# curl -L -o ~/whisper-models/ggml-base.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin
```

**注意事项：**
- 首次启用 openai 或 whisper_cpp 时会打印一行提示
- openai 缺 key 时静默回退 local
- whisper_cpp 二进制未找到时静默回退 local
- 切换后端后旧缓存自动失效并重新转录

### 图片解密

微信 4.0 的 .dat 图片文件使用三种加密格式之一：

| 格式 | 时期 | 加密方式 |
|------|------|---------|
| 旧 XOR | ~2025-07 | 单字节 XOR |
| V1 | 过渡期 | AES-ECB + XOR |
| V2 | 2025-08+ | AES-128-ECB + XOR |

macOS 图片密钥从磁盘 kvcomm 缓存派生，无需扫描进程内存：

```bash
python find_image_key_macos.py
```

密钥自动保存到 `config.json`，之后 Web UI 自动显示图片预览。

### Makefile 命令

```bash
make setup      # 全自动：venv → pip install → brew install → 编译扫描器 → 配置
make build      # 编译 macOS 密钥扫描器
make keys       # 提取密钥（需要 root）
make decrypt    # 解密全部数据库
make web        # 启动 Web UI
make all        # 从零到完成：setup → keys → decrypt → export
make status     # 显示当前数据状态
make clean      # 交互式清理：选择删除 decrypted / exported_chats / 临时文件
make help       # 列出所有命令
```

---

## 文件说明

| 文件 | 说明 |
|------|------|
| `main.py` | **一键启动入口** — 自动配置、提取密钥、启动 Web UI |
| `decrypt_db.py` | 全量解密所有数据库 |
| `export_all_chats.py` | 批量导出所有聊天为 JSON（支持 `-t` 附带语音转录） |
| `export_chat.py` | 单会话导出（供 export_all_chats.py 内部调用） |
| `chat_export_helpers.py` | 导出格式化共享函数（两脚本共用，避免代码漂移） |
| `transcribe_chat.py` | 语音消息转录（共享 config.json 配置的 backend） |
| `app_gui.py` | **GUI 工具箱** — tkinter 界面，整合解密/导出/音频转换 |
| `export_messages.py` | 聊天记录导出（CSV / HTML / JSON） |
| `voice_to_mp3.py` | 语音消息 SILK 转 MP3 |
| `build.bat` | 一键打包为单 exe（PyInstaller） |
| `config.py` | 配置加载器（自动检测微信数据目录） |
| `find_all_keys.py` | 平台分发入口（Windows / Linux） |
| `find_all_keys_windows.py` | Windows 版内存扫描提 key |
| `find_all_keys_linux.py` | Linux 版内存扫描提 key |
| `find_wxwork_keys.py` | 企业微信 Windows 版内存扫描提 key |
| `decrypt_wxwork_db.py` | 企业微信 wxSQLite3 AES-128 数据库解密 |
| `export_wxwork_messages.py` | 企业微信聊天记录导出（按个人/群筛选，CSV / HTML / JSON） |
| `mcp_server.py` | MCP Server，让 Claude AI 查询微信数据 |
| `monitor_web.py` | 实时消息监听 (Web UI + SSE) |
| `monitor.py` | 实时消息监听 (命令行) |
| `find_all_keys_macos.c` | macOS 版内存密钥扫描器 (C, Mach VM API) |
| `find_image_key.py` | 从进程内存提取图片 AES 密钥（Windows / Linux） |
| `find_image_key_macos.py` | macOS 版图片密钥派生（从磁盘 kvcomm 缓存推算） |
| `decode_image.py` | 图片 .dat 文件解密模块 (XOR / V1 / V2) |
| `config.json` | 配置文件（自动生成，手动编辑） |
| `setup.sh` | 一键安装脚本 |

---

## 🔧 技术细节

### 原理

微信 4.0 使用 SQLCipher 4 加密本地数据库：
- **加密算法**: AES-256-CBC + HMAC-SHA512
- **KDF**: PBKDF2-HMAC-SHA512, 256,000 iterations
- **每个数据库有独立的 salt 和 enc_key**

WCDB (微信的 SQLCipher 封装) 会在进程内存中缓存派生后的 raw key，格式为 `x'<64hex_enc_key><32hex_salt>'`。三个平台均可通过扫描进程内存匹配此模式，再通过 HMAC 校验 page 1 确认密钥正确性。

### GUI 工具箱 & 单 exe 打包

提供 tkinter 图形界面 (`app_gui.py`)，集成核心功能：

1. **解密数据库** — 调用 `main.py decrypt`
2. **导出消息** — 调用 `export_messages.py`，输出 CSV / HTML / JSON
3. **转换音频** — 调用 `voice_to_mp3.py`，SILK_V3 → MP3
4. **企业微信解密** — 调用 `find_wxwork_keys.py` + `decrypt_wxwork_db.py`
5. **企业微信导出** — 调用 `export_wxwork_messages.py`，按个人/群导出 CSV / HTML / JSON

#### 直接运行

```bash
python app_gui.py
```

#### 打包为单 exe

```bash
pip install pyinstaller
build.bat
```

输出 `dist\WeChatDecrypt.exe`（约 18MB），双击即可使用，无需安装 Python。

> 转换音频需要系统安装 [FFmpeg](https://ffmpeg.org/download.html) 并加入 PATH。

详细说明见 [EXE_USAGE.md](EXE_USAGE.md)。

### WAL 处理

微信使用 SQLite WAL 模式，WAL 文件是**预分配固定大小** (4MB)。检测变化时：
- 不能用文件大小（永远不变）
- 使用 mtime 检测写入
- 解密 WAL frame 时需校验 salt 值，跳过旧周期遗留的 frame

### 更新日志

<details>
<summary>点击展开</summary>

#### 2025-03-03 — 富媒体内容 & 组合消息修复
- 表情包内联显示
- 富媒体内容解析（链接卡片、文件、视频号、小程序等）
- 文字+图片组合消息不再丢失
- 隐藏消息检测机制
- Web UI 改进

</details>

### 企业微信数据库解密 (实验)

企业微信 Windows 5.x 的本地数据库不是普通微信 SQLCipher 4 格式，而是 wxSQLite3 AES-128-CBC：

- 16 字节 raw key
- 每页按 page index + `sAlT` 派生 AES key
- 每页 IV 由 page index 派生
- 无 SQLCipher HMAC / reserve 区

提取并解密：

```bash
python find_wxwork_keys.py
python decrypt_wxwork_db.py
python export_wxwork_messages.py
```

如果自动提取失败但你已有 raw key，也可以直接传入 32 位 hex key：

```bash
python decrypt_wxwork_db.py --key 00112233445566778899aabbccddeeff
```

配置项：

```json
{
    "wxwork_db_dir": "C:\\Users\\<用户>\\Documents\\WXWork\\<account_id>\\Data",
    "wxwork_keys_file": "wxwork_keys.json",
    "wxwork_decrypted_dir": "wxwork_decrypted",
    "wxwork_export_dir": "wxwork_export"
}
```

### 数据库结构

解密后包含约 26 个数据库：
- `session/session.db` - 会话列表 (最新消息摘要)
- `message/message_*.db` - 聊天记录
- `contact/contact.db` - 联系人
- `media_*/media_*.db` - 媒体文件索引
- 其他: head_image, favorite, sns, emoticon 等

## 免责声明

本工具仅用于学习和研究目的，用于解密**自己的**微信数据。请遵守相关法律法规，不要用于未经授权的数据访问。

防失联 TG: https://t.me/wechat_decrypt
