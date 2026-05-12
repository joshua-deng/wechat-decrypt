# WeChat 4.x Database Decryptor

微信 4.0 (Windows、MacOS、Linux) 本地数据库解密工具。从运行中的微信进程内存提取加密密钥，解密所有 SQLCipher 4 加密数据库，并提供实时消息监听。

## 更新日志

## 防失联tg: https://t.me/wechat_decrypt

### 2025-03-03 — 富媒体内容 & 组合消息修复

- **表情包内联显示**: 自动从 emoticon.db 构建 MD5→CDN 映射，支持自定义表情（NonStore）和商店表情（Store），CDN 下载后本地缓存
- **富媒体内容解析**: 链接卡片（type 49）、文件、视频号、小程序、引用回复、位置分享等在 Web UI 中完整渲染
- **文字+图片组合消息不再丢失**: 修复同时发送文字和图片时只显示最后一条的问题（前端去重 key 增加消息类型）
- **隐藏消息检测**: 新增 `_check_hidden_messages` 机制，session.db 只保存最后一条消息摘要，现在会异步查 message DB 找回同一秒内的其他消息
- **MonitorDBCache 线程安全**: 引入 per-key 锁，防止多线程并发解密同一数据库导致文件损坏
- **Web UI 改进**: 消息气泡样式优化、群聊发送者显示、图片缩略图点击放大

## 原理

微信 4.0 使用 SQLCipher 4 加密本地数据库：
- **加密算法**: AES-256-CBC + HMAC-SHA512
- **KDF**: PBKDF2-HMAC-SHA512, 256,000 iterations
- **页面大小**: 4096 bytes, reserve = 80 (IV 16 + HMAC 64)
- **每个数据库有独立的 salt 和 enc_key**

WCDB (微信的 SQLCipher 封装) 会在进程内存中缓存派生后的 raw key，格式为 `x'<64hex_enc_key><32hex_salt>'`。三个平台（Windows / Linux / macOS）均可通过扫描进程内存匹配此模式，再通过 HMAC 校验 page 1 确认密钥正确性。

## 使用方法

### 环境要求

- Python 3.10+
- 微信 4.x
- `pip install -r requirements.txt`

Windows：

- Windows 10/11
- 微信正在运行
- 需要管理员权限（读取进程内存）

Linux：

- 64-bit Linux
- 需要 root 权限或 `CAP_SYS_PTRACE`（读取 `/proc/<pid>/mem`）
- `db_dir` 默认类似 `~/Documents/xwechat_files/<wxid>/db_storage`

macOS：

- macOS 10.15+（Apple Silicon / Intel 均可）
- 微信 4.x（macOS 版）
- Xcode Command Line Tools：`xcode-select --install`
- 需要对 `/Applications/WeChat.app` 做 ad-hoc 重签名（允许进程内存读取），重签名前须先退出微信
- 需要 root 权限运行扫描器
- `db_dir` 默认类似 `~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<wxid>/db_storage`

### 安装依赖

```bash
pip install -r requirements.txt
```

<details>
<summary>⚠️ 安装失败？点击展开常见问题</summary>

**问题：`error: externally-managed-environment` (PEP 668)**

Homebrew Python (3.12+) 和部分 Linux 发行版禁止 `pip install` 直接写入系统 Python 环境，会报此错误。

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
make decrypt  # 等价于 .venv/bin/python3 main.py decrypt
make web      # 等价于 .venv/bin/python3 main.py
```

---

**Windows 权限不足或全局环境不可写**，可以改用：

```bash
py -m pip install --user -r requirements.txt
```

如果需要读取受保护的进程或把依赖安装到系统 Python，也可能需要以管理员身份打开终端。

</details>

### 快速开始

Windows：

```bash
python main.py
python main.py decrypt
```

Linux：

```bash
python3 main.py decrypt
```

macOS（密钥扫描用 C 版本，见下文 [macOS 数据库密钥扫描](#macos-数据库密钥扫描-wechat-4x) 章节）：

```bash
# 1. 退出微信，重新签名（首次及微信升级后各一次）
killall WeChat
sudo codesign --force --deep --sign - /Applications/WeChat.app

# 2. 重新打开微信并登录，然后编译并运行扫描器
cc -O2 -o find_all_keys_macos find_all_keys_macos.c -framework Foundation
sudo ./find_all_keys_macos

# 3. 解密
python3 decrypt_db.py
```

程序会自动完成：配置检测 → 内存扫描提取密钥 → 解密。首次运行会自动检测微信数据目录并生成 `config.json`。微信只要在运行中即可，无需重启或重新登录。

如果自动检测失败（例如微信安装在非默认位置），手动创建 `config.json`：
```json
{
    "db_dir": "D:\\xwechat_files\\你的微信ID\\db_storage",
    "keys_file": "all_keys.json",
    "decrypted_dir": "decrypted",
    "wechat_process": "Weixin.exe"
}
```

Linux 版 `config.json` 示例：

```json
{
    "db_dir": "/home/yourname/Documents/xwechat_files/your_wxid/db_storage",
    "keys_file": "all_keys.json",
    "decrypted_dir": "decrypted",
    "wechat_process": "wechat"
}
```

macOS 版 `config.json` 示例：

```json
{
    "db_dir": "/Users/yourname/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/your_wxid/db_storage",
    "keys_file": "all_keys.json",
    "decrypted_dir": "decrypted",
    "wechat_process": "WeChat"
}
```

`db_dir` 路径：Windows 可在微信设置 → 文件管理中找到；Linux 默认在 `~/Documents/xwechat_files/<wxid>/db_storage`；macOS 在 `~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<wxid>/db_storage`（程序已支持自动检测）。

### Web UI 说明

`python main.py` 启动后打开 http://localhost:5678 查看实时消息流。

- 30ms 轮询 WAL 文件变化 (mtime)
- 检测到变化后全量解密 + WAL patch (~70ms)
- SSE 实时推送到浏览器
- 总延迟约 100ms
- **图片消息内联预览**（支持旧 XOR / V1 / V2 三种 .dat 加密格式）

#### HTTP API

| 端点 | 说明 |
|------|------|
| `GET /api/history` | 最近消息列表 (JSON) |
| `GET /api/history?chat=群名` | 按群名/用户名过滤消息 |
| `GET /api/history?since=1712000000` | 增量拉取（返回该时间戳之后的消息） |
| `GET /api/history?chat=群名&since=ts&limit=100` | 参数可组合使用 |
| `GET /api/tags` | 所有联系人标签及成员 (JSON) |
| `GET /api/tags?name=同事` | 按标签名过滤 |
| `GET /stream` | SSE 实时消息推送 |

将特定群消息存到自己的数据库：监听 `/stream` 或轮询 `/api/history?chat=群名&since=上次时间戳`，写入即可。

### MCP Server (Claude AI 集成)

将微信数据查询能力接入 [Claude Code](https://claude.ai/claude-code)，让 AI 直接读取你的微信消息。

```bash
pip install -r requirements.txt
```

注册到 Claude Code：

```bash
claude mcp add wechat -- python C:\Users\你的用户名\wechat-decrypt\mcp_server.py
```

或手动编辑 `~/.claude.json`：

```json
{
  "mcpServers": {
    "wechat": {
      "type": "stdio",
      "command": "python",
      "args": ["C:\\Users\\你的用户名\\wechat-decrypt\\mcp_server.py"]
    }
  }
}
```

注册后在 Claude Code 中即可使用以下工具：

| Tool | 功能 |
|------|------|
| `get_recent_sessions(limit)` | 最近会话列表（含消息摘要、未读数） |
| `get_chat_history(chat_name, limit, offset, start_time, end_time)` | 指定聊天的消息记录，支持时间范围和分页 |
| `search_messages(keyword, chat_name, start_time, end_time, limit, offset)` | 统一搜索消息；支持全库、单个聊天对象、多个聊天对象、时间范围和分页 |
| `get_contacts(query, limit)` | 搜索/列出联系人 |
| `get_contact_tags()` | 列出所有联系人标签及成员数量 |
| `get_tag_members(tag_name)` | 获取指定标签下的所有联系人，支持模糊匹配 |
| `get_new_messages()` | 获取自上次调用以来的新消息 |
| `get_voice_messages(chat_name)` | 列出某会话所有语音消息（local_id、时长、时间戳） |
| `decode_voice(chat_name, local_id)` | 解码 SILK 语音为本地 WAV 文件 |
| `transcribe_voice(chat_name, local_id)` | 转录语音为文字（自动检测语言） |

前置条件：需要先运行 `python main.py` 或 `python find_all_keys.py` 完成密钥提取。

说明：`search_messages` 的 `limit` 最大为 `500`；`get_chat_history` 支持更大的 `limit`，但消息很多时仍建议配合 `offset` 分页读取。

#### ⚠️ 语音转录隐私

`transcribe_voice` 默认使用本地 Whisper（CPU），数据全程留在本机。`transcribe_chat.py` 批量 CLI 共享同一份配置。

如需切换到 OpenAI Whisper API（更快、Mandarin 精度更高），在 `config.json` 中：

```json
{
    "transcription_backend": "openai",
    "openai_api_key": "sk-..."
}
```

启用后**语音文件会上传至 OpenAI 服务器**进行转录。需 `pip install openai`。

- 成本：约 $0.006 / 分钟（OpenAI 计价）
- 文件 > 25MB 在上传前被拒绝（OpenAI 上限）

如需切换到 whisper.cpp 后端（macOS Metal GPU 加速，3-5x 更快），在 `config.json` 中：

```json
{
    "transcription_backend": "whisper_cpp"
}
```

数据全程留在本机，不上传。需要 `brew install whisper-cpp` 并下载模型（自动检测常见路径，或通过 `whisper_cpp_binary` / `whisper_cpp_model` 指定）。

所有后端共用以下行为：
- 首次启用 openai 或 whisper_cpp 后端时 stderr 会打一行警告
- openai: `openai_api_key` 缺失时静默回退 local
- whisper_cpp: 二进制文件未找到时静默回退 local
- 切换后端后，旧缓存条目（backend 不匹配）自动重新转录

**[查看使用案例 →](USAGE.md)**

### 图片解密 (V2 格式)

微信 4.0 (2025-08+) 的 .dat 图片文件使用 AES-128-ECB + XOR 混合加密 (V2 格式)。AES 密钥的获取方式因平台而异：

**Windows / Linux**（从进程内存扫描）：

```bash
# 1. 在微信中打开查看 2-3 张图片（点击看大图）
# 2. 立即运行密钥提取（持续监控版）：
python find_image_key_monitor.py

# 或单次扫描版：
python find_image_key.py
```

> AES 密钥仅在微信查看图片时临时加载到内存中。如果扫描未找到密钥，请先在微信中查看几张图片，然后立即重新运行脚本。

**macOS**（从磁盘 kvcomm 缓存派生，**无需扫描进程内存**）：

```bash
python find_image_key_macos.py
```

无需提前在微信中查看图片，无需 root 权限，无需重签名。脚本会扫描 `~/Library/Containers/com.tencent.xinWeChat/.../app_data/net/kvcomm/key_*.statistic` 文件名提取派生码 `code`，配合 `db_dir` 路径里的 wxid，按 `aes_key = MD5(str(code) + cleaned_wxid)[:16]` / `xor_key = code & 0xFF` 的规则推算密钥，并用一张 V2 `_t.dat` 缩略图做 AES 模板验证。解决 [issue #23](https://github.com/ylytdeng/wechat-decrypt/issues/23)（macOS 内存扫描器 197K 候选全部失败）。

派生算法的发现归功于 [@hicccc77](https://github.com/hicccc77) 在 issue #23 的[评论](https://github.com/ylytdeng/wechat-decrypt/issues/23)，参考实现见其 [WeFlow 项目](https://github.com/hicccc77/WeFlow/blob/dev/electron/services/keyServiceMac.ts)（CC BY-NC-SA 4.0）。本仓库的 `find_image_key_macos.py` 是基于该算法的独立 Python clean-room 实现。

密钥会自动保存到 `config.json` 的 `image_aes_key` / `image_xor_key` 字段。之后 `monitor_web.py` 启动时会自动加载，图片消息将显示内联预览。

## 文件说明

| 文件 | 说明 |
|------|------|
| `main.py` | **一键启动入口** — 自动配置、提取密钥、启动服务 |
| `config.py` | 配置加载器（自动检测微信数据目录） |
| `find_all_keys.py` | 平台分发入口（Windows / Linux） |
| `find_all_keys_windows.py` | Windows 版内存扫描提 key |
| `find_all_keys_linux.py` | Linux 版内存扫描提 key |
| `decrypt_db.py` | 全量解密所有数据库 |
| `mcp_server.py` | MCP Server，让 Claude AI 查询微信数据 |
| `monitor_web.py` | 实时消息监听 (Web UI + SSE + 图片预览) |
| `monitor.py` | 实时消息监听 (命令行) |
| `decode_image.py` | 图片 .dat 文件解密模块 (XOR / V1 / V2) |
| `find_image_key.py` | 从微信进程内存提取图片 AES 密钥（Windows / Linux） |
| `find_image_key_monitor.py` | 持续监控版密钥提取（Windows / Linux，推荐） |
| `find_image_key_macos.py` | macOS 版图片密钥派生（从磁盘 kvcomm 缓存推算，无需扫描内存） |
| `latency_test.py` | 延迟测量诊断工具 |
| `find_all_keys_macos.c` | macOS 版内存密钥扫描器 (C, Mach VM API) |

## 技术细节

### WAL 处理

微信使用 SQLite WAL 模式，WAL 文件是**预分配固定大小** (4MB)。检测变化时：
- 不能用文件大小 (永远不变)
- 使用 mtime 检测写入
- 解密 WAL frame 时需校验 salt 值，跳过旧周期遗留的 frame

### 图片 .dat 加密格式

微信本地图片 (.dat) 有三种加密格式：

| 格式 | 时期 | Magic | 加密方式 | 密钥来源 |
|------|------|-------|---------|---------|
| 旧 XOR | ~2025-07 | 无 | 单字节 XOR | 自动检测 (对比 magic bytes) |
| V1 | 过渡期 | `07 08 V1 08 07` | AES-ECB + XOR | 固定 key: `cfcd208495d565ef` |
| V2 | 2025-08+ | `07 08 V2 08 07` | AES-128-ECB + XOR | 从进程内存提取 |

V2 文件结构: `[6B signature] [4B aes_size LE] [4B xor_size LE] [1B padding]` + `[AES-ECB encrypted] [raw unencrypted] [XOR encrypted]`

### 数据库结构

解密后包含约 26 个数据库：
- `session/session.db` - 会话列表 (最新消息摘要)
- `message/message_*.db` - 聊天记录
- `contact/contact.db` - 联系人
- `media_*/media_*.db` - 媒体文件索引
- 其他: head_image, favorite, sns, emoticon 等

## macOS 数据库密钥扫描 (WeChat 4.x)

macOS 版微信 4.x 使用 SQLCipher 4 加密本地数据库，密钥格式为 `x'<64hex_key><32hex_salt>'`。C 版扫描器通过 Mach VM API 扫描微信进程内存提取密钥。

### 前置条件

- macOS (Apple Silicon / Intel)
- WeChat 4.x (macOS 版)
- Xcode Command Line Tools: `xcode-select --install`
- 微信需要 ad-hoc 签名（或安装了防撤回补丁）：
  `sudo codesign --force --deep --sign - /Applications/WeChat.app`

### 编译和使用

```bash
# 编译
cc -O2 -o find_all_keys_macos find_all_keys_macos.c -framework Foundation

# 运行（自动查找微信进程、扫描内存、匹配 DB salt）
sudo ./find_all_keys_macos

# 或指定 PID
sudo ./find_all_keys_macos <pid>
```

输出 `all_keys.json`，格式兼容 `decrypt_db.py`，可直接用于解密：

```bash
python3 decrypt_db.py
```

### 常见问题

#### `task_for_pid failed: 5`

以 root 运行扫描器后仍报此错，说明微信进程的 Hardened Runtime 签名未移除。

**原因**：macOS 会阻止对带有 Hardened Runtime 标志的进程进行内存读取，即使以 root 身份运行也不行。微信默认签名包含此标志。

**排查步骤**：

```bash
# 1. 检查微信当前签名（如包含 flags=0x10000(runtime) 则需要重签名）
codesign -dvvv /Applications/WeChat.app 2>&1 | grep flags

# 2. 必须先退出微信再重签名（微信在运行时重签名不会生效）
killall WeChat
sudo codesign --force --deep --sign - /Applications/WeChat.app

# 3. 验证签名已变更（应显示 flags=0x2，不再有 runtime 标志）
codesign -dvvv /Applications/WeChat.app 2>&1 | grep flags

# 4. 重新打开微信并登录，然后运行扫描器
sudo ./find_all_keys_macos
```

**注意**：
- 微信每次更新后签名会恢复原始状态，需重新执行上述步骤
- `--deep` 参数确保签名覆盖 App Bundle 内所有嵌套二进制文件
- 重签名后必须重启微信，否则进程仍使用旧的签名凭证

#### 未能自动检测微信数据目录

程序已支持 macOS 自动检测微信数据目录。如果检测失败，手动查找并配置：

```bash
# 搜索 db_storage 目录
find ~/Library/Containers/com.tencent.xinWeChat -type d -name "db_storage" 2>/dev/null
```

如有多个账号（多个 `db_storage` 目录），按修改时间判断当前活跃账号：

```bash
stat -f "%m %N" /path/to/account1/db_storage /path/to/account2/db_storage
# 数值更大 = 最近活跃
```

然后编辑 `config.json`，将找到的路径填入 `db_dir` 字段。

## 免责声明

本工具仅用于学习和研究目的，用于解密**自己的**微信数据。请遵守相关法律法规，不要用于未经授权的数据访问。
