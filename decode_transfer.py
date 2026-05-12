"""
读取微信转账消息（appmsg type=2000）的结构化字段。

用法:
    python3 decode_transfer.py <chat_name> <local_id> [<ts>]

参数:
    <chat_name>  联系人显示名、备注名或 wxid（仅 1v1 聊天有转账消息）。
    <local_id>   转账消息的 local_id（从 export_chat 输出 / monitor_web 等地方获取）。
    [<ts>]       可选 unix 时间戳。当 local_id 在多个分片冲突时用它唯一定位。

输出: 多行可读文本，含方向（发起/收款/退还）、金额、备注、付款/收款 wxid、
      交易号、发起/失效时间。

需先完成 WeChat DB 解密（详见 README）。本 CLI 是 mcp_server.decode_transfer
工具的命令行包装，输出格式与 MCP 工具一致。
"""
import argparse
import sys

import mcp_server


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python3 decode_transfer.py",
        description="读取微信转账消息的结构化字段",
    )
    parser.add_argument("chat_name", help="联系人名/备注/wxid")
    parser.add_argument("local_id", type=int, help="转账消息的 local_id")
    parser.add_argument(
        "ts",
        nargs="?",
        type=int,
        default=0,
        help="消息的 unix 时间戳（跨分片唯一定位时需要，可省略）",
    )
    args = parser.parse_args()

    result = mcp_server.decode_transfer(args.chat_name, args.local_id, args.ts)
    print(result)
    # 如果工具返回错误文案，退出码非 0 便于 shell 脚本判断
    if result.startswith(("错误:", "找不到", "不是转账消息", "无法解析", "消息中没有", "消息 content")):
        return 1
    if "无法唯一定位" in result:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
