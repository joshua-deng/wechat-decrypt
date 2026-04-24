# Chat Export JSON Format

Files produced by `export_chat.py` and `transcribe_chat.py` use a compact schema
where defaults and nulls are omitted. This document describes how to load and
interpret those files.

## Producing a file

```bash
.venv/bin/python3 export_chat.py <chat_name> [output.json]
.venv/bin/python3 transcribe_chat.py <input.json> [output.json]
```

`export_chat.py` writes the raw export; `transcribe_chat.py` fills in
transcriptions for voice messages (Whisper, CPU). Re-running `transcribe_chat.py`
is safe — already-transcribed messages are skipped.

## Top-level shape

```json
{
  "chat": "<display name>",
  "exported_at": "YYYY-MM-DD HH:MM:SS",
  "is_group": true,
  "messages": [ ... ]
}
```

- `chat` — display name of the chat (contact name or group name).
- `exported_at` — local timestamp string, for provenance only.
- `is_group` — present and `true` **only** for group chats; absent for 1-on-1.
- `messages` — array sorted oldest → newest across all DB shards.

Count is `len(messages)`; there is no `total` field.

## Message object

Every message carries three required keys: `local_id`, `timestamp`, `sender`.
All other keys are **optional** and omitted when they would carry a default or
null value.

| Key             | Type    | Required | Meaning / default when absent                                                                                                        |
| --------------- | ------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `local_id`      | int     | yes      | Stable WeChat row id within this chat. Use for matching when re-transcribing or diffing exports.                                     |
| `timestamp`     | int     | yes      | Unix epoch seconds (local wall time → seconds). Convert with `datetime.fromtimestamp(ts)`.                                           |
| `sender`        | string  | yes      | `"me"` = logged-in user. Otherwise the sender's display name — the contact's name in 1-on-1 chats, the member's name in groups. `""` for unattributable messages (e.g. system notifications). |
| `type`          | string  | no       | Message type. **Absent ⇒ `"text"`.** Known values: `text`, `image`, `voice`, `sticker`, `video`, `link_or_file`, `call`, `system`, `recall`, `contact_card`, `location`. |
| `content`       | string  | no       | Rendered text of the message. Absent when nothing extractable (e.g. some images / calls / system events).                            |
| `transcription` | string  | no       | Present **only** on `type: "voice"` messages that have been transcribed. May be an empty string `""` if Whisper produced nothing.     |

## Loading examples

Iterate with defaults applied:

```python
import json
from datetime import datetime

with open("chat_export_transcribed.json") as f:
    data = json.load(f)

is_group = data.get("is_group", False)

for m in data["messages"]:
    mtype = m.get("type", "text")
    when = datetime.fromtimestamp(m["timestamp"])
    sender = m["sender"]  # "me" | contact/member name | ""
    text = m.get("content", "")
    if mtype == "voice":
        text = m.get("transcription") or "[voice, untranscribed]"
    print(f"[{when:%Y-%m-%d %H:%M}] {sender or '(system)'}: {text}")
```

Determine "sent by me":

```python
from_me = m["sender"] == "me"
```

Filter only voice messages that still need transcription:

```python
pending = [m for m in data["messages"]
           if m.get("type") == "voice" and not m.get("transcription")]
```

## Notes on interpretation

- **System messages** (`type: "system"`) have `sender: ""` — they're not "from"
  anyone. Typical content: recall notifications ("X 撤回了一条消息"), add-friend
  events, etc.
- **Empty transcription** (`transcription: ""`) means Whisper ran but produced
  no text — usually a very short or silent clip. It's distinct from "not yet
  transcribed" (field absent).
- **`content` for non-text types** is a rendered summary: `[视频] 12秒`,
  `[表情] 哈哈`, `[图片]`, etc. The raw media is still in the WeChat DB; see
  `mcp_server.py` helpers (`decode_image`, `decode_voice`) to extract it.
- **Group chats**: `sender` is the member's resolved display name; the
  logged-in user is still `"me"`.
