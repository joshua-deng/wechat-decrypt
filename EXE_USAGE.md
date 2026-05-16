# WeChat Decrypt 工具箱 使用说明

## 快速开始

1. **启动微信**并登录账号；如果要解密企业微信，也请先启动企业微信
2. 双击 `WeChatDecrypt.exe` 打开工具箱
3. 根据需要点击按钮：
   - **① 微信解密** → 从微信进程提取密钥并解密数据库到 `decrypted/` 目录
   - **② 图片密钥** → 从微信进程提取新版图片 AES 密钥
   - **③ 导出数据** → 将聊天记录导出为 CSV / HTML / JSON 到 `export/` 目录
   - **④ 朋友圈图片** → 解密朋友圈缓存图片
   - **⑤ 企业微信解密** → 从企业微信进程提取密钥并解密数据库到 `wxwork_decrypted/` 目录
   - **⑥ 企业微信导出** → 选择某个人或群，导出 CSV / HTML / JSON 到 `wxwork_export/` 目录

## 前置要求

- Windows 10 / 11
- 微信 PC 版已登录（解密微信时需要微信进程运行）
- 企业微信 PC 版已登录（解密企业微信时需要企业微信进程运行）
- [FFmpeg](https://ffmpeg.org/download.html) 已安装并加入 PATH（转换音频需要）

### 检查 FFmpeg

打开命令提示符，输入：
```
ffmpeg -version
```
如果提示"不是内部或外部命令"，需要先安装 FFmpeg。

## 输出目录说明

运行后在 exe 所在目录下生成以下文件夹：

```
WeChatDecrypt.exe
config.json          ← 首次运行自动生成的配置文件
decrypted/           ← ① 解密后的数据库文件
wxwork_decrypted/    ← ⑤ 解密后的企业微信数据库文件
wxwork_export/       ← ⑥ 导出的企业微信聊天记录
  群名_R_123/
    .info
    messages.csv
    messages.html
    messages.json
export/              ← ③ 导出的聊天记录
  张三/
    .info            ← 联系人信息（username/alias/remark/nick_name）
    message_0.db.csv ← CSV 格式（Excel 可直接打开）
    message_0.db.html← HTML 格式（浏览器打开，微信气泡样式）
    message_0.db.json← JSON 格式（程序处理用）
  李四/
    ...
data/                ← 导出时选择“同时转换语音为 MP3”后的输出
  张三/
    .info
    20250101_120000_1.mp3
    ...
```

## 导出格式说明

### CSV
- 编码：UTF-8 with BOM，Excel 双击即可正确显示中文
- 字段：时间、发送者、消息类型、内容、server_id

### HTML
- 浏览器打开，模拟微信聊天界面
- 左侧气泡为接收消息，右侧为发送消息
- 按日期自动分组

### JSON
- 完整结构化数据，包含所有元信息
- 适合程序二次处理或 AI 训练

## 配置文件

首次运行会自动检测微信数据目录并生成 `config.json`：

```json
{
    "db_dir": "D:\\xwechat_files\\wxid_xxx\\db_storage",
    "keys_file": "all_keys.json",
    "decrypted_dir": "decrypted",
    "wechat_process": "Weixin.exe",
    "wxwork_db_dir": "C:\\Users\\<用户>\\Documents\\WXWork\\<account_id>\\Data",
    "wxwork_keys_file": "wxwork_keys.json",
    "wxwork_decrypted_dir": "wxwork_decrypted",
    "wxwork_export_dir": "wxwork_export"
}
```

如果自动检测失败，请手动修改 `db_dir` 为你的微信数据目录。
路径可在：微信设置 → 文件管理 中找到。

## 常见问题

**Q: 点击"解密数据库"提示未检测到微信进程**
A: 请确保微信 PC 版已启动并登录，然后重试。

**Q: 解密失败 / 密钥提取失败**
A: 检查 `config.json` 中的 `db_dir` 是否与当前登录的微信账号匹配。切换账号后需要删除 `all_keys.json` 重新提取。

**Q: 企业微信解密失败 / 找不到企业微信数据目录**
A: 确认企业微信 PC 版已启动并登录。若自动检测失败，请在 `config.json` 中设置 `wxwork_db_dir`，路径通常类似 `C:\Users\<用户>\Documents\WXWork\<account_id>\Data`。切换企业微信账号后删除 `wxwork_keys.json` 重新提取。

**Q: 企业微信导出为空 / 找不到会话**
A: 先执行"⑤ 企业微信解密"，确认 `wxwork_decrypted/message.db` 和 `wxwork_decrypted/session.db` 存在，然后再执行"⑥ 企业微信导出"。

**Q: 转换音频没有输出**
A: 确认已安装 FFmpeg 并加入系统 PATH。确认已先执行"① 解密数据库"。

**Q: 导出消息为空**
A: 确认已先执行"① 解密数据库"，且 `decrypted/message/` 下有 `.db` 文件。

**Q: 目录名是 wxid_xxx 而不是昵称**
A: 该联系人不在通讯录中（contact.db 无记录），会使用原始 username。

## 自行打包

安装依赖后双击 `build.bat` 即可重新打包：

```
pip install pyinstaller pycryptodome zstandard pilk
build.bat
```

输出文件：`dist\WeChatDecrypt.exe`
