# WeChatDecrypt 使用说明 (Web UI + CLI 双模式)

## 快速开始

1. **启动微信** (个人微信 / 企业微信, 哪个想解密就启动哪个)
2. **双击 `WeChatDecrypt.exe`**
3. 浏览器**自动打开** `http://localhost:5678` (没自动开就手动复制粘贴)
4. 右上角点 **🛠️ 工具** 展开工具箱, 按 tab 切到你要的板块

## CLI 用法

同一个 `WeChatDecrypt.exe` 带参数运行时会作为命令行入口:

```powershell
.\WeChatDecrypt.exe --help
.\WeChatDecrypt.exe status
.\WeChatDecrypt.exe decrypt
.\WeChatDecrypt.exe export --from-plan-csv export_plan.csv
.\WeChatDecrypt.exe export-all --write-plan-csv export_plan.csv
```

无参数或 `web` 子命令仍启动 Web UI:

```powershell
.\WeChatDecrypt.exe
.\WeChatDecrypt.exe web
```

## 工具箱 3 个 tab

### 📱 个人微信

| 步骤 | 操作 |
|---|---|
| Step 1 — 解密 | ① 提取密钥 + 解密数据库 / ② 提取图片密钥 |
| Step 2 — 导出/解码 | ③ 导出聊天 (弹模态框选会话+格式) / ④ 批量解密图片 / ⑤ 朋友圈解密+导出 |

**前置**: 微信 PC 版正在运行且已登录

### 🏢 企业微信

| 步骤 | 操作 |
|---|---|
| Step 1 — 解密 | ① 提取密钥 + 解密数据库 |
| Step 2 — 导出 | ② 导出聊天 (弹模态框选会话+CSV/HTML/JSON) |

**前置**: 企业微信 PC 版正在运行且已登录 (独立于个人微信)

### 🔧 工具

跟微信/企微进程无关, 只读已解密产物:
- 语音转 MP3 (需 [ffmpeg](https://ffmpeg.org/) 在 PATH)

## 实时消息监听

- 工具箱下方就是消息流, 按时间降序排列 (最新在顶)
- SSE 推送, 毫秒级延迟
- 图片自动解密预览, 表情/链接/转账等富媒体内联渲染
- 右上角 **⚙️** 配置消息通知规则 (按群名/发送人匹配, 桌面通知 + 声音)

## 导出筛选 (重要)

点 ③ 导出聊天 / ② 企微导出 后, **会弹模态框**:

- 🔍 搜索框按会话名/wxid 过滤
- 复选框选要导的会话 (3142 个个人微信会话 / 14 个企微会话按时间降序)
- 一键 [全选] / [清空] / [选最近 30 天活跃]
- 选格式 (CSV / HTML / JSON, 企微支持; 个人微信目前只输出 JSON)
- 点 [确认导出 →] 才真正跑

**不会再"一点就跑全量"**。

## 任务终止

任何任务跑起来后, 触发的按钮会变成红色 **🛑 终止**。再点一下立刻 SIGTERM/kill 子进程。

## 前置要求

- Windows 10 / 11
- 微信 / 企业微信 PC 版已登录 (跑解密前需要进程在运行)
- [FFmpeg](https://ffmpeg.org/download.html) 已安装并加入 PATH (仅"语音转 MP3"需要)

## 输出目录

在 exe 所在目录下生成:

```
WeChatDecrypt.exe
config.json                  ← 首次运行自动生成
decrypted/                   ← 个人微信解密后的 SQLite 数据库
wxwork_decrypted/            ← 企业微信解密后的 SQLite 数据库
wxwork_keys.json             ← 企微 keys (含明文 raw key, 已 chmod 0600)
all_keys.json                ← 个人微信 keys (同上)
wechat_files/<wxid>/         ← 导出的聊天记录 (按 wxid + 联系人分子目录)
  张三/
    messages.csv             ← (用 export_messages.py 时)
    messages.html
    messages.json
  朋友圈图片/                  ← 朋友圈缓存图片解密后
  data/                      ← 语音转 MP3 输出 (有的话)
exported_chats/              ← 用 ③ 导出全部聊天 (JSON) 时的输出 (export_all_chats.py)
wxwork_export/               ← 企微聊天导出
```

## 远程访问 / 多设备

monitor_web 默认 bind `0.0.0.0`, 同局域网其他设备可以访问
`http://<你的本机 IP>:5678`。如需要只允许本机访问, 修改源码 `PORT` 那一行附近的 bind 地址改成 `127.0.0.1`。

## 还有个 tkinter 桌面 GUI (备用, 共存)

如果你 **不想开浏览器** (公司机器禁浏览器 / 全离线场景), 可以用桌面 GUI:

```bash
python app_gui.py        # → 弹 tkinter 窗口
```

跟 Web UI 功能基本对齐 (8 个按钮 / 导出对话框), 缺点:
- 中文字体下渲染较糊
- Windows-only (不跨平台)
- 没法远程访问

单 exe **默认入口是 Web UI + CLI 分发器**。要打 tkinter 版的 exe, 改
`WeChatDecrypt.spec` 里 `Analysis(['wechat_decrypt_launcher.py'])` 为
`Analysis(['app_gui.py'])` 再 `build.bat`。
