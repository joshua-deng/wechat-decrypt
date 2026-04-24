# 聊天导出 JSON 数据格式

`export_chat.py` 与 `transcribe_chat.py` 生成的 JSON 文件采用紧凑格式：
默认值与空值会被省略。本文档说明如何加载和解读这类文件。

## 生成文件

```bash
.venv/bin/python3 export_chat.py <chat_name> [output.json]
.venv/bin/python3 transcribe_chat.py <input.json> [output.json]
```

`export_chat.py` 负责原始导出；`transcribe_chat.py` 使用 Whisper（CPU）
为语音消息填充转录文本。`transcribe_chat.py` 可重复运行 —— 已转录的
消息会被跳过。

## 顶层结构

```json
{
  "chat": "<display name>",
  "username": "<wxid 或 @chatroom>",
  "exported_at": "YYYY-MM-DD HH:MM:SS",
  "is_group": true,
  "messages": [ ... ]
}
```

- `chat` —— 聊天的显示名（联系人名或群名）。
- `username` —— 稳定的 WeChat 用户名（1-on-1 聊天为 `wxid_*`，群聊为 `*@chatroom`）。
  `transcribe_chat.py` 会优先读取本字段而非基于 `chat` 再次模糊匹配，避免同名联系人漂移。
- `exported_at` —— 本地时间字符串，仅作溯源用途。
- `is_group` —— **仅**群聊出现且为 `true`；1-on-1 聊天时省略。
- `messages` —— 消息数组，跨所有 DB 分片按时间由旧到新排序。

消息条数 = `len(messages)`，没有 `total` 字段。

## 消息对象

每条消息必有三个字段：`local_id`、`timestamp`、`sender`。
其余字段均为**可选**，当值为默认值或 null 时会被省略。

| 字段            | 类型   | 必填 | 含义 / 缺失时的默认值                                                                                                                                         |
| --------------- | ------ | ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `local_id`      | int    | 是   | WeChat 内该聊天的稳定行 ID。用于重跑转录或对比导出时的消息匹配。                                                                                              |
| `timestamp`     | int    | 是   | Unix 时间戳（秒级，本地时间已换算为秒）。通过 `datetime.fromtimestamp(ts)` 转换。                                                                             |
| `sender`        | string | 是   | `"me"` 代表当前登录用户；否则为发送者的显示名 —— 1-on-1 聊天中是联系人名，群聊中是群成员名。对于无法归属的消息（如系统通知）为 `""`。                         |
| `type`          | string | 否   | 消息类型。**缺失时视为 `"text"`**。已知取值：`text`、`image`、`voice`、`sticker`、`video`、`link_or_file`、`call`、`system`、`recall`、`contact_card`、`location`。 |
| `content`       | string | 否   | 消息的渲染文本。当没有可提取内容时省略（例如部分图片 / 通话 / 系统事件）。                                                                                    |
| `transcription` | string | 否   | **仅**在 `type: "voice"` 且已完成转录的消息上出现。若 Whisper 未产出文本可能为空串 `""`。                                                                     |

## 加载示例

带默认值的遍历：

```python
import json
from datetime import datetime

with open("chat_export_transcribed.json") as f:
    data = json.load(f)

is_group = data.get("is_group", False)

for m in data["messages"]:
    mtype = m.get("type", "text")
    when = datetime.fromtimestamp(m["timestamp"])
    sender = m["sender"]  # "me" | 联系人/群成员名 | ""
    text = m.get("content", "")
    if mtype == "voice":
        text = m.get("transcription") or "[voice, untranscribed]"
    print(f"[{when:%Y-%m-%d %H:%M}] {sender or '(system)'}: {text}")
```

判断消息是否由自己发出：

```python
from_me = m["sender"] == "me"
```

筛选仍需转录的语音消息：

```python
pending = [m for m in data["messages"]
           if m.get("type") == "voice" and not m.get("transcription")]
```

## 解读注意事项

- **系统消息**（`type: "system"`）的 `sender` 为 `""` —— 不属于任何人。
  常见内容：撤回通知（"X 撤回了一条消息"）、添加好友事件等。
- **空转录**（`transcription: ""`）表示 Whisper 已经运行但未产出文本，
  通常是极短或静音片段。这与"尚未转录"（字段缺失）是不同的状态。
- **非文本消息的 `content`** 是渲染摘要：`[视频] 12秒`、`[表情] 哈哈`、
  `[图片]` 等。原始媒体仍在 WeChat DB 中，可用 `mcp_server.py` 中的
  辅助函数（`decode_image`、`decode_voice`）取出。
- **群聊**中的 `sender` 是群成员解析后的显示名；当前登录用户仍为 `"me"`。
