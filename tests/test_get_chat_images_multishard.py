"""Tests for `get_chat_images` multi-shard scanning.

WeChat rolls a chat's messages over to the next `message_N.db` shard once
the current one fills up, so any chat older than the current shard window
has its history split across multiple shards. The other query tools
(`get_chat_history`, `search_messages`, `decode_image`) already scan all
shards via `_find_msg_tables_for_user`; before this fix `get_chat_images`
used the single-shard `_find_msg_table_for_user`, so it silently dropped
every image that lived in a non-first shard.

These tests pin the corrected behaviour: results come from all matching
shards, are sorted by `create_time` DESC across shards, and respect the
`limit` cap.
"""
import unittest
from unittest.mock import patch

import mcp_server


class GetChatImagesMultiShardTests(unittest.TestCase):
    def setUp(self):
        # `resolve_username` / `get_contact_names` would hit real DBs; stub them.
        self._patches = [
            patch.object(mcp_server, "resolve_username",
                          side_effect=lambda x: "wxid_demo"),
            patch.object(mcp_server, "get_contact_names",
                          return_value={"wxid_demo": "Demo"}),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _run(self, shards, shard_images_map, limit=20):
        """Helper: stub the two collaborators and call the tool."""
        def fake_list(db_path, table_name, username, limit=20, start_ts=None, end_ts=None):
            return shard_images_map.get(db_path, [])

        with patch.object(mcp_server, "_find_msg_tables_for_user",
                          return_value=shards), \
             patch.object(mcp_server._image_resolver, "list_chat_images",
                          side_effect=fake_list):
            return mcp_server.get_chat_images("Demo", limit=limit)

    def test_collects_images_from_every_shard(self):
        shards = [
            {"db_path": "/m/message_1.db", "table_name": "Msg_x",
             "max_create_time": 1_800_000_000},
            {"db_path": "/m/message_2.db", "table_name": "Msg_x",
             "max_create_time": 1_700_000_000},
        ]
        shard_images = {
            "/m/message_1.db": [
                {"local_id": 11, "create_time": 1_800_000_000, "md5": "a" * 32, "size": 1024},
            ],
            "/m/message_2.db": [
                {"local_id": 22, "create_time": 1_700_000_000, "md5": "b" * 32, "size": 2048},
            ],
        }
        out = self._run(shards, shard_images)
        # Both shards' images must appear; before the fix the message_2.db
        # image was silently dropped.
        self.assertIn("local_id=11", out)
        self.assertIn("local_id=22", out)
        self.assertIn("2 张图片", out)

    def test_global_sort_by_create_time_desc(self):
        # Older shard happens to contain a NEWER image (e.g. when shards are
        # ordered by max_create_time but individual rows interleave): the
        # output must still be globally sorted, not per-shard concatenated.
        shards = [
            {"db_path": "/m/message_1.db", "table_name": "Msg_x",
             "max_create_time": 1_800_000_000},
            {"db_path": "/m/message_2.db", "table_name": "Msg_x",
             "max_create_time": 1_700_000_000},
        ]
        shard_images = {
            "/m/message_1.db": [
                {"local_id": 11, "create_time": 1_750_000_000, "md5": "a" * 32},
            ],
            "/m/message_2.db": [
                # Older shard, but this single image is newer than the one above.
                {"local_id": 22, "create_time": 1_799_000_000, "md5": "b" * 32},
            ],
        }
        out = self._run(shards, shard_images)
        pos_22 = out.find("local_id=22")
        pos_11 = out.find("local_id=11")
        self.assertGreaterEqual(pos_22, 0)
        self.assertGreaterEqual(pos_11, 0)
        self.assertLess(pos_22, pos_11)  # newer first

    def test_limit_truncates_globally_across_shards(self):
        shards = [
            {"db_path": "/m/message_1.db", "table_name": "Msg_x",
             "max_create_time": 1_800_000_000},
            {"db_path": "/m/message_2.db", "table_name": "Msg_x",
             "max_create_time": 1_700_000_000},
        ]
        shard_images = {
            "/m/message_1.db": [
                {"local_id": i, "create_time": 1_800_000_000 - i}
                for i in range(0, 5)
            ],
            "/m/message_2.db": [
                {"local_id": 100 + i, "create_time": 1_700_000_000 - i}
                for i in range(0, 5)
            ],
        }
        out = self._run(shards, shard_images, limit=3)
        # 3 newest overall = local_id=0, 1, 2 (all from shard 1, but the
        # decision is global, not "first shard wins").
        self.assertIn("3 张图片", out)
        self.assertIn("local_id=0", out)
        self.assertIn("local_id=1", out)
        self.assertIn("local_id=2", out)
        self.assertNotIn("local_id=100", out)

    def test_no_shards_returns_not_found(self):
        out = self._run(shards=[], shard_images_map={})
        self.assertIn("找不到", out)

    def test_all_shards_empty_returns_no_images(self):
        shards = [
            {"db_path": "/m/message_1.db", "table_name": "Msg_x",
             "max_create_time": 0},
        ]
        out = self._run(shards, shard_images_map={"/m/message_1.db": []})
        self.assertIn("无图片消息", out)


if __name__ == "__main__":
    unittest.main()
