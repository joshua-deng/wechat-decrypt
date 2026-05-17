# WeChat 4.x Database Decryptor

> 💬 **交流群 / 防失联**: [Telegram - t.me/wechat_decrypt](https://t.me/wechat_decrypt)

**微信 / 企业微信本地数据库解密 + 数据工具集** (Windows / macOS / Linux)

从运行中的进程内存提取加密密钥,解密 SQLite 加密数据库,衍生出一整套实用工具:

| 能力 | 支持范围 |
|---|---|
| 🔓 **数据库解密** | 个人微信 4.0 (SQLCipher 4) + 企业微信 5.x (wxSQLite3 AES-128) |
| 📡 **实时消息监听** | Web UI (SSE) / 命令行 / MCP Server (Claude AI 集成) |
| 📦 **批量导出** | 全部聊天 → JSON / CSV / HTML, 含增量 + 日期范围 + dry-run |
| 🖼️ **图片解密** | V1 / V2 / wxgf 三种 .dat 格式 + 朋友圈缓存 |
| 🎙️ **语音转录** | SILK → WAV → 文本 (local Whisper / OpenAI / whisper.cpp 三种 backend) |
| 🪧 **朋友圈** | SnsTimeLine 解析 + 缓存图片解密 + HTML 时间线 |
| 🖱️ **Windows GUI** | tkinter 界面整合所有功能 + PyInstaller 单 exe 打包 |

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
<summary>Windows — 最小路径 (CLI)</summary>

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
<summary>Windows — Web UI (推荐, 跨平台 + 实时监听)</summary>

```bash
python monitor_web.py        # → 浏览器自动开 http://localhost:5678
```

工具箱 3 个 tab (📱 个人微信 / 🏢 企业微信 / 🔧 工具), 覆盖解密 / 导出 /
朋友圈 / 语音转 MP3 全部场景, 导出带模态框筛选 (不会一点就跑全量)。
实时消息监听跟工具箱在同一页面。

</details>

<details>
<summary>Windows — 桌面 GUI / EXE (tkinter, 适合不开浏览器的场景)</summary>

```bash
# 直接跑桌面 GUI
python app_gui.py

# 或打包成单 exe 分发给别人 (默认入口 Web UI)
build.bat                    # 输出 dist/WeChatDecrypt.exe
```

跟 Web UI **共存**, 两者覆盖功能基本一致。tkinter 优点是不依赖浏览器
(全离线 / 公司机器禁浏览器场景), 缺点是中文渲染糊 + 不跨平台 +
没远程访问。 详见 [EXE_USAGE.md](./EXE_USAGE.md)。

</details>

<details>
<summary>Windows — 企业微信 (实验)</summary>

```bash
# 1. 启动企业微信并登录
# 2. 提取企微 keys + 解密
python find_wxwork_keys.py    # 自动检测 Documents\WXWork\<id>\Data
python decrypt_wxwork_db.py   # 解密到 wxwork_decrypted/

# 3. (可选) 导出聊天记录
python export_wxwork_messages.py
```

仅 Windows 5.x 实测可用。详见技术细节里的「企业微信数据库解密」章节。

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
| 生成导出计划 CSV（黑名单，默认） | `python export_all_chats.py --write-plan-csv export_plan.csv` |
| 生成导出计划 CSV（白名单） | `python export_all_chats.py --write-plan-csv export_plan.csv --plan-mode whitelist` |
| 按计划 CSV 导出（黑名单，默认） | `python export_all_chats.py output_dir --from-plan-csv export_plan.csv` |
| 按计划 CSV 导出（白名单） | `python export_all_chats.py output_dir --from-plan-csv export_plan.csv --plan-mode whitelist` |
| 批量导出 + 语音转录 | `python export_all_chats.py --with-transcriptions` |
| 转录单个文件语音 | `python transcribe_chat.py input.json [output.json]` |
| 注册 MCP Server（Claude） | `claude mcp add wechat -- python /path/to/mcp_server.py` |

批量导出会在输出目录自动维护 `_export_index.json`，用稳定的 `username`
追踪当前 JSON 文件。再次导出时如果联系人备注或群名变化，会先把旧文件
重命名为新的可读文件名；如果同名文件属于另一个 `username`，会追加
`__<username>` 后缀避免覆盖。

导出计划 CSV 支持两种模式：默认 `blacklist` 模式下只有 `export=0`
的行会被跳过，空值、`1` 或没有 `export` 列都会导出；`whitelist`
模式下只有明确 `export=1` 的行会导出。

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

文件按功能分组,方便定位。

<details open>
<summary><b>① 入口 / 一键脚本</b></summary>

| 文件 | 说明 |
|---|---|
| `main.py` | **CLI 总入口** — 子命令 `decrypt` / `export` / `all` / `status` / `decode-images` / `help` |
| `monitor_web.py` | **Web UI 总入口 (推荐)** — 浏览器界面: 实时监听 + 8 个工具按钮 + 导出筛选模态框, 也是 PyInstaller 单 exe 的入口 |
| `app_gui.py` | **桌面 GUI (备用)** — tkinter 界面, 跟 Web UI 功能基本对齐, 适合不开浏览器场景 |
| `setup.sh` | 一键安装依赖 (macOS / Linux / Windows Git Bash) |
| `setup.py` | 交互式配置向导 (`python setup.py --check` 仅检查环境) |
| `cleanup.py` | 磁盘清理工具 (`status` 查看用量 / `--dry-run` 预览) |

</details>

<details>
<summary><b>② 密钥提取 (从进程内存)</b></summary>

| 文件 | 说明 |
|---|---|
| `find_all_keys.py` | 平台分发入口 (Windows / Linux) |
| `find_all_keys_windows.py` | Windows: 扫 Weixin.exe 内存找 SQLCipher raw key |
| `find_all_keys_linux.py` | Linux: 同上, 走 /proc/<pid>/mem |
| `find_all_keys_macos.c` | macOS: C + Mach VM API (需 codesign + 重签 WeChat.app) |
| `find_image_key.py` | 从进程内存提取图片 AES 密钥 (Windows / Linux) |
| `find_image_key_macos.py` | macOS: 从磁盘 kvcomm 缓存推算 (无需进程在线) |
| `find_image_key_monitor.py` | 持续监控模式 (Windows) |
| `find_wxwork_keys.py` | **企业微信 5.x** wxSQLite3 raw key 提取 (cipher 结构体扫描) |
| `key_scan_common.py` / `key_utils.py` | 扫描器共用工具 |

</details>

<details>
<summary><b>③ 数据库 / 图片 / 语音解密</b></summary>

| 文件 | 说明 |
|---|---|
| `decrypt_db.py` | 全量解密 SQLCipher 4 数据库 (支持 `-i` 增量) |
| `decode_image.py` | 图片 `.dat` 解密 (V1 / V2 / 旧 XOR), 含 `aligned_aes_block_size` 公共 helper |
| `decrypt_wxwork_db.py` | **企业微信** wxSQLite3 AES-128 数据库解密 |
| `decrypt_sns.py` | **朋友圈缓存图片**解密 (V1 / V2 / XOR) |
| `wxwork_crypto.py` | 企业微信 wxSQLite3 per-page 加密原语 (MD5 派生 + AES-128-CBC) |
| `batch_decrypt_images.py` | CLI: 任意目录递归批量解 .dat |

</details>

<details>
<summary><b>④ 导出 (JSON / CSV / HTML)</b></summary>

| 文件 | 说明 |
|---|---|
| `export_all_chats.py` | 批量导出全部聊天为 JSON (含 CSV 计划选择、`-t` 转录、`-i` 增量、日期范围、`--dry-run`) |
| `export_chat.py` | 单会话 JSON 导出 (供 `export_all_chats` 调用) |
| `chat_export_helpers.py` | JSON 导出共享格式化函数 (避免漂移) |
| `export_messages.py` | CSV / HTML / JSON 三种格式导出, 图片可内联 (PR #107) |
| `export_wxwork_messages.py` | 企业微信版导出 (CSV / HTML / JSON) |
| `export_sns.py` | 朋友圈 SnsTimeLine 导出 (JSON + HTML 时间线) |

</details>

<details>
<summary><b>⑤ 实时监听 / 服务</b></summary>

| 文件 | 说明 |
|---|---|
| `monitor_web.py` | (见 ① 入口) — 同一个文件既是 Web UI 总入口也是实时消息监听 |
| `monitor.py` | 命令行实时监听 |
| `mcp_server.py` | **MCP Server** — Claude AI 查询微信数据 (含 `get_chat_history` / `decode_voice` / `decode_refer` 等 20+ 工具) |
| `decode_transfer.py` | CLI: 查单条转账消息 (mcp_server `decode_transfer` 工具的命令行包装) |

</details>

<details>
<summary><b>⑥ 语音 / 音频</b></summary>

| 文件 | 说明 |
|---|---|
| `transcribe_chat.py` | 语音转文本 (local Whisper / OpenAI / whisper.cpp 三 backend) |
| `voice_to_mp3.py` | SILK_V3 → MP3 (需要 ffmpeg in PATH) |

</details>

<details>
<summary><b>⑦ 打包 / 配置 / 文档</b></summary>

| 文件 | 说明 |
|---|---|
| `config.py` | 配置加载器 (自动检测微信数据目录, 支持打包后 exe 路径) |
| `config.json` | 配置文件 (首次运行自动生成) |
| `WeChatDecrypt.spec` | PyInstaller 打包描述 |
| `build.bat` | Windows 一键打包为单 exe |
| `requirements.txt` | Python 依赖 |
| `EXE_USAGE.md` | GUI / EXE 使用说明 |
| `Makefile` | 常用命令快捷方式 (`make all` / `make clean` / `make status`) |

</details>

<details>
<summary><b>⑧ 测试 / 文档</b></summary>

| 路径 | 说明 |
|---|---|
| `tests/` | 单元测试 (185+ 用例, 含 wxsqlite3 / image v2 / msg types / pagination 等) |
| `docs/` | 部署 / 排错指南 + 字段研究报告 |
| `latency_test.py` | 开发工具: 测量消息从 WeChat 写入到我们检测的延迟 |

</details>

---

## 🔧 技术细节

### 原理

微信 4.0 使用 SQLCipher 4 加密本地数据库：
- **加密算法**: AES-256-CBC + HMAC-SHA512
- **KDF**: PBKDF2-HMAC-SHA512, 256,000 iterations
- **每个数据库有独立的 salt 和 enc_key**

WCDB (微信的 SQLCipher 封装) 会在进程内存中缓存派生后的 raw key，格式为 `x'<64hex_enc_key><32hex_salt>'`。三个平台均可通过扫描进程内存匹配此模式，再通过 HMAC 校验 page 1 确认密钥正确性。

#### 安全提示

- `all_keys.json` / `wxwork_keys.json` 包含明文 raw key,落盘时已 `chmod 0600`(Unix)或保留默认 ACL(Windows)。**勿提交到 git 或与人共享**——拿到 key 等于拿到全部聊天解密能力。
- 解密后的 `.db` 文件是明文 SQLite,内容包括所有联系人、群、消息,**同样需要小心备份和分享**。

### 朋友圈解密的 XML 安全

`export_sns.py` 解析 SnsTimeLine 的 XML 时已加 **XXE 防护**(拒绝 `<!DOCTYPE>` / `<!ENTITY>` + 200KB 大小上限),避免恶意朋友圈 XML 通过 entity expansion 或外部实体引用执行 SSRF / 读取本地文件。`mcp_server.py` 解析其他类型 appmsg XML 同样有这层保护。

### GUI 工具箱 (Web UI 推荐 + tkinter 备用, 共存)

项目提供 **两套 UI 共存**, 用户按场景选:

#### Web UI (`monitor_web.py`) — 推荐

```bash
python monitor_web.py        # → 浏览器自动开 http://localhost:5678
```

右上角 🛠️ 工具 按钮展开 3 个 tab:

- **📱 个人微信**: Step 1 解密 / 图片密钥 → Step 2 导出聊天 / 批量解图片 / 朋友圈
- **🏢 企业微信**: Step 1 解密 → Step 2 导出聊天 (CSV/HTML/JSON)
- **🔧 工具**: 语音转 MP3 等

特点:
- **导出筛选模态框**: 不会一点就跑全量, 弹框选会话 (含搜索/全选/选最近 30 天)
- **任务终止**: 按钮变红 🛑, 一点立刻 SIGTERM 子进程
- **跟实时监听共存**: 同一页面下方就是消息流, 互不影响
- **跨平台 + 远程可访问**: 浏览器渲染清晰, 默认 bind 0.0.0.0 同局域网可用

#### 桌面 GUI (`app_gui.py`) — 备用

```bash
python app_gui.py            # 直接弹 tkinter 窗口, 不开浏览器
```

适合: 公司机器禁浏览器 / 全离线场景 / 喜欢传统桌面应用的用户。
功能跟 Web UI 基本对齐 (8 个按钮覆盖解密/导出/朋友圈/企微/语音),
但中文字体下渲染较糊, Windows-only。

#### 打包为单 exe

```bash
pip install pyinstaller
build.bat                    # → dist\WeChatDecrypt.exe (~20MB)
```

**单 exe 默认入口是 Web UI** (双击 → 自动开浏览器), 因为 Web UI 体验更好。
同一个 exe 也支持 CLI 子命令:

```powershell
.\WeChatDecrypt.exe --help
.\WeChatDecrypt.exe status
.\WeChatDecrypt.exe decrypt
.\WeChatDecrypt.exe export --from-plan-csv export_plan.csv
.\WeChatDecrypt.exe export-all --write-plan-csv export_plan.csv
```

要打包成 tkinter 入口的话改 `WeChatDecrypt.spec` 里的
`Analysis(['wechat_decrypt_launcher.py'])` 为 `Analysis(['app_gui.py'])`
再 `build.bat`。

> 语音转 MP3 需要系统安装 [FFmpeg](https://ffmpeg.org/download.html) 并加入 PATH。

详细说明见 [EXE_USAGE.md](EXE_USAGE.md)。

### WAL 处理

微信使用 SQLite WAL 模式，WAL 文件是**预分配固定大小** (4MB)。检测变化时：
- 不能用文件大小（永远不变）
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

<details>
<summary>点击展开</summary>

#### 2025-03-03 — 富媒体内容 & 组合消息修复
- 表情包内联显示
- 富媒体内容解析（链接卡片、文件、视频号、小程序等）
- 文字+图片组合消息不再丢失
- 隐藏消息检测机制
- Web UI 改进

</details>

### 免责声明

本工具仅用于学习和研究目的，用于解密**自己的**微信数据。请遵守相关法律法规，不要用于未经授权的数据访问。

> 💬 交流 / 防失联: [t.me/wechat_decrypt](https://t.me/wechat_decrypt) (顶部也有)
