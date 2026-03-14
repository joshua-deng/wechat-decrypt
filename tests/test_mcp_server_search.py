import hashlib
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import mcp_server


class _FakeCache:
    # 用最小缓存桩替代真实解密缓存，避免单元测试依赖本地微信环境。
    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, rel_key):
        return self._mapping.get(rel_key)


def _msg_table_name(username):
    # 生产代码使用 username 的 md5 作为消息表名，测试里保持一致。
    return f"Msg_{hashlib.md5(username.encode()).hexdigest()}"


def _create_message_db(path, chats):
    # 构造最小可用消息库，只包含搜索/历史查询依赖的字段。
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
        for username, messages in chats.items():
            conn.execute("INSERT INTO Name2Id(user_name) VALUES (?)", (username,))
            table_name = _msg_table_name(username)
            conn.execute(
                f"""
                CREATE TABLE [{table_name}] (
                    local_id INTEGER,
                    local_type INTEGER,
                    create_time INTEGER,
                    real_sender_id INTEGER,
                    message_content TEXT,
                    WCDB_CT_message_content INTEGER
                )
                """
            )
            for local_id, create_time, content in messages:
                conn.execute(
                    f"""
                    INSERT INTO [{table_name}] (
                        local_id, local_type, create_time, real_sender_id,
                        message_content, WCDB_CT_message_content
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (local_id, 1, create_time, 0, content, 0),
                )
        conn.commit()
    finally:
        conn.close()


class SearchMessagesTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def create_db(self, filename, chats):
        path = os.path.join(self.temp_dir.name, filename)
        _create_message_db(path, chats)
        return path

    def test_validate_pagination_rejects_large_limit(self):
        # 防止单次查询过大，保证 limit 上限校验存在。
        with self.assertRaisesRegex(ValueError, "limit 不能大于 500"):
            mcp_server._validate_pagination(501, 0)

    def test_page_search_entries_returns_chronological_results_with_offset(self):
        # 结果应先按最新时间分页，再把当前页恢复成时间正序输出。
        entries = [(1, "a"), (5, "e"), (3, "c"), (4, "d"), (2, "b")]

        paged = mcp_server._page_search_entries(entries, limit=2, offset=1)

        self.assertEqual(paged, [(3, "c"), (4, "d")])

    def test_search_messages_single_chat_uses_offset_and_returns_page(self):
        # 单聊分页应只返回当前页，并按聊天阅读顺序展示。
        db_path = self.create_db(
            "single.db",
            {
                "alice": [
                    (1, 100, "foo newest"),
                    (2, 90, "foo middle"),
                    (3, 80, "foo oldest"),
                ]
            },
        )
        ctx = {
            "query": "Alice",
            "username": "alice",
            "display_name": "Alice",
            "db_path": db_path,
            "table_name": _msg_table_name("alice"),
            "message_tables": [{"db_path": db_path, "table_name": _msg_table_name("alice")}],
            "is_group": False,
        }

        with patch.object(mcp_server, "get_contact_names", return_value={"alice": "Alice"}), patch.object(
            mcp_server, "_resolve_chat_context", return_value=ctx
        ):
            result = mcp_server.search_messages("foo", chat_name="Alice", limit=2, offset=1)

        self.assertIn('在 Alice 中搜索 "foo" 找到 2 条结果（offset=1, limit=2）', result)
        self.assertLess(result.index("foo oldest"), result.index("foo middle"))
        self.assertNotIn("foo newest", result)

    def test_search_messages_multiple_chats_applies_global_pagination(self):
        # 多个聊天联合搜索时，分页必须基于合并后的全局结果。
        db_path = self.create_db(
            "multi.db",
            {
                "alice": [
                    (1, 110, "foo a1"),
                    (2, 90, "foo a2"),
                ],
                "bob": [
                    (1, 100, "foo b1"),
                    (2, 80, "foo b2"),
                ],
            },
        )
        contexts = [
            {
                "query": "Alice",
                "username": "alice",
                "display_name": "Alice",
                "db_path": db_path,
                "table_name": _msg_table_name("alice"),
                "message_tables": [{"db_path": db_path, "table_name": _msg_table_name("alice")}],
                "is_group": False,
            },
            {
                "query": "Bob",
                "username": "bob",
                "display_name": "Bob",
                "db_path": db_path,
                "table_name": _msg_table_name("bob"),
                "message_tables": [{"db_path": db_path, "table_name": _msg_table_name("bob")}],
                "is_group": False,
            },
        ]

        with patch.object(mcp_server, "get_contact_names", return_value={"alice": "Alice", "bob": "Bob"}), patch.object(
            mcp_server, "_resolve_chat_contexts", return_value=(contexts, [], [])
        ):
            result = mcp_server.search_messages("foo", chat_name=["Alice", "Bob"], limit=2, offset=1)

        self.assertIn('在 2 个聊天对象中搜索 "foo" 找到 2 条结果（offset=1, limit=2）', result)
        self.assertLess(result.index("foo a2"), result.index("foo b1"))
        self.assertNotIn("foo a1", result)
        self.assertNotIn("foo b2", result)

    def test_search_messages_all_messages_scans_all_dbs_before_paging(self):
        # 全库搜索要先扫完整个库，再分页，不能被旧分片提前截断。
        older_db = self.create_db(
            "older.db",
            {"older_user": [(1, 10, "foo older 1"), (2, 9, "foo older 2"), (3, 8, "foo older 3")]},
        )
        newer_db = self.create_db(
            "newer.db",
            {"newer_user": [(1, 30, "foo newer 1"), (2, 20, "foo newer 2")]},
        )
        fake_cache = _FakeCache({"older": older_db, "newer": newer_db})

        with patch.object(mcp_server, "MSG_DB_KEYS", ["older", "newer"]), patch.object(
            mcp_server, "_cache", fake_cache
        ), patch.object(
            mcp_server,
            "get_contact_names",
            return_value={"older_user": "Older", "newer_user": "Newer"},
        ):
            result = mcp_server.search_messages("foo", limit=2, offset=0)

        self.assertIn('搜索 "foo" 找到 2 条结果（offset=0, limit=2）', result)
        self.assertLess(result.index("foo newer 2"), result.index("foo newer 1"))
        self.assertNotIn("foo older 1", result)

    def test_search_messages_single_chat_respects_time_range(self):
        # 单聊搜索的开始/结束时间都必须严格生效。
        db_path = self.create_db(
            "single_time.db",
            {
                "alice": [
                    (1, 300, "foo in range"),
                    (2, 200, "foo too early"),
                    (3, 400, "foo too late"),
                ]
            },
        )
        ctx = {
            "query": "Alice",
            "username": "alice",
            "display_name": "Alice",
            "db_path": db_path,
            "table_name": _msg_table_name("alice"),
            "message_tables": [{"db_path": db_path, "table_name": _msg_table_name("alice")}],
            "is_group": False,
        }

        with patch.object(mcp_server, "get_contact_names", return_value={"alice": "Alice"}), patch.object(
            mcp_server, "_resolve_chat_context", return_value=ctx
        ), patch.object(
            mcp_server, "_parse_time_range", return_value=(250, 350)
        ):
            result = mcp_server.search_messages(
                "foo",
                chat_name="Alice",
                start_time="custom-start",
                end_time="custom-end",
                limit=20,
                offset=0,
            )

        self.assertIn("时间范围: custom-start ~ custom-end", result)
        self.assertIn("foo in range", result)
        self.assertNotIn("foo too early", result)
        self.assertNotIn("foo too late", result)

    def test_search_messages_multiple_chats_respects_time_range(self):
        # 多聊联合搜索时，每个聊天对象都要套用同一时间范围。
        db_path = self.create_db(
            "multi_time.db",
            {
                "alice": [(1, 300, "foo alice in range"), (2, 150, "foo alice too early")],
                "bob": [(1, 320, "foo bob in range"), (2, 500, "foo bob too late")],
            },
        )
        contexts = [
            {
                "query": "Alice",
                "username": "alice",
                "display_name": "Alice",
                "db_path": db_path,
                "table_name": _msg_table_name("alice"),
                "message_tables": [{"db_path": db_path, "table_name": _msg_table_name("alice")}],
                "is_group": False,
            },
            {
                "query": "Bob",
                "username": "bob",
                "display_name": "Bob",
                "db_path": db_path,
                "table_name": _msg_table_name("bob"),
                "message_tables": [{"db_path": db_path, "table_name": _msg_table_name("bob")}],
                "is_group": False,
            },
        ]

        with patch.object(mcp_server, "get_contact_names", return_value={"alice": "Alice", "bob": "Bob"}), patch.object(
            mcp_server, "_resolve_chat_contexts", return_value=(contexts, [], [])
        ), patch.object(
            mcp_server, "_parse_time_range", return_value=(250, 400)
        ):
            result = mcp_server.search_messages(
                "foo",
                chat_name=["Alice", "Bob"],
                start_time="range-start",
                end_time="range-end",
                limit=20,
                offset=0,
            )

        self.assertIn("时间范围: range-start ~ range-end", result)
        self.assertIn("foo alice in range", result)
        self.assertIn("foo bob in range", result)
        self.assertNotIn("foo alice too early", result)
        self.assertNotIn("foo bob too late", result)

    def test_search_messages_all_messages_respects_time_range(self):
        # 全库搜索也不能返回时间范围外的消息。
        db_path = self.create_db(
            "all_time.db",
            {
                "alice": [
                    (1, 100, "foo too early"),
                    (2, 300, "foo in range"),
                    (3, 500, "foo too late"),
                ]
            },
        )
        fake_cache = _FakeCache({"all": db_path})

        with patch.object(mcp_server, "MSG_DB_KEYS", ["all"]), patch.object(
            mcp_server, "_cache", fake_cache
        ), patch.object(
            mcp_server,
            "get_contact_names",
            return_value={"alice": "Alice"},
        ), patch.object(
            mcp_server, "_parse_time_range", return_value=(250, 350)
        ):
            result = mcp_server.search_messages(
                "foo",
                start_time="range-start",
                end_time="range-end",
                limit=20,
                offset=0,
            )

        self.assertIn("时间范围: range-start ~ range-end", result)
        self.assertIn("foo in range", result)
        self.assertNotIn("foo too early", result)
        self.assertNotIn("foo too late", result)

    def test_get_chat_history_merges_sharded_message_tables(self):
        # 同一联系人跨多个 message_N.db 分片时，历史查询要先合并再分页。
        older_db = self.create_db("history_older.db", {"alice": [(1, 100, "old message")]})
        newer_db = self.create_db(
            "history_newer.db",
            {"alice": [(1, 300, "new message"), (2, 250, "middle message")]},
        )
        ctx = {
            "query": "Alice",
            "username": "alice",
            "display_name": "Alice",
            "db_path": newer_db,
            "table_name": _msg_table_name("alice"),
            "message_tables": [
                {"db_path": older_db, "table_name": _msg_table_name("alice")},
                {"db_path": newer_db, "table_name": _msg_table_name("alice")},
            ],
            "is_group": False,
        }

        with patch.object(mcp_server, "get_contact_names", return_value={"alice": "Alice"}), patch.object(
            mcp_server, "_resolve_chat_context", return_value=ctx
        ):
            result = mcp_server.get_chat_history("Alice", limit=2, offset=0)

        self.assertIn("Alice 的消息记录（返回 2 条，offset=0, limit=2）", result)
        self.assertIn("middle message", result)
        self.assertIn("new message", result)
        self.assertNotIn("old message", result)

    def test_search_messages_single_chat_merges_sharded_message_tables(self):
        # 单聊搜索也要跨分片合并，否则最近消息可能查不到。
        older_db = self.create_db("search_older.db", {"alice": [(1, 100, "foo old")]})
        newer_db = self.create_db(
            "search_newer.db",
            {"alice": [(1, 300, "foo new"), (2, 200, "foo middle")]},
        )
        ctx = {
            "query": "Alice",
            "username": "alice",
            "display_name": "Alice",
            "db_path": newer_db,
            "table_name": _msg_table_name("alice"),
            "message_tables": [
                {"db_path": older_db, "table_name": _msg_table_name("alice")},
                {"db_path": newer_db, "table_name": _msg_table_name("alice")},
            ],
            "is_group": False,
        }

        with patch.object(mcp_server, "get_contact_names", return_value={"alice": "Alice"}), patch.object(
            mcp_server, "_resolve_chat_context", return_value=ctx
        ), patch.object(
            mcp_server, "_parse_time_range", return_value=(150, 350)
        ):
            result = mcp_server.search_messages(
                "foo",
                chat_name="Alice",
                start_time="range-start",
                end_time="range-end",
                limit=20,
                offset=0,
            )

        self.assertIn("foo middle", result)
        self.assertIn("foo new", result)
        self.assertNotIn("foo old", result)


if __name__ == "__main__":
    unittest.main()
