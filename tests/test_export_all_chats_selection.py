import csv
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from unittest.mock import patch

import export_all_chats


class ChatSelectionTests(unittest.TestCase):
    def setUp(self):
        self.rows = export_all_chats._build_chat_rows(
            ["wxid_alice", "12345@chatroom", "wxid_bob"],
            {
                "wxid_alice": "Alice",
                "12345@chatroom": "Project Group",
                "wxid_bob": "Bob",
            },
            [
                {
                    "username": "wxid_alice",
                    "remark": "Alice Remark",
                    "nick_name": "Alice Nick",
                },
            ],
        )

    def test_selects_by_index_range_and_text_match(self):
        selected = export_all_chats._select_chat_usernames(
            self.rows, "2,1-1,bob"
        )

        self.assertEqual(
            selected,
            ["12345@chatroom", "wxid_alice", "wxid_bob"],
        )

    def test_rejects_unknown_selection_token(self):
        with self.assertRaisesRegex(ValueError, "没有匹配"):
            export_all_chats._select_chat_usernames(self.rows, "nobody")

    def test_chat_rows_include_contact_remark_and_nickname(self):
        self.assertEqual(self.rows[0]["remark"], "Alice Remark")
        self.assertEqual(self.rows[0]["nick_name"], "Alice Nick")
        self.assertEqual(self.rows[1]["remark"], "")
        self.assertEqual(self.rows[1]["nick_name"], "")


class ExportAllChatsCliSelectionTests(unittest.TestCase):
    def _run_main(self, argv):
        with patch.object(export_all_chats.mcp_server, "DECRYPTED_DIR", "decrypted"), \
             patch.object(export_all_chats.os.path, "exists", return_value=True), \
             patch.object(export_all_chats, "_load_session_usernames",
                          return_value=["wxid_alice", "12345@chatroom"]), \
             patch.object(export_all_chats.mcp_server, "get_contact_names",
                          return_value={
                              "wxid_alice": "Alice",
                              "12345@chatroom": "Project Group",
                          }), \
             patch.object(export_all_chats.mcp_server, "get_contact_full",
                          return_value=[]), \
             patch.object(export_all_chats, "export_one",
                          return_value=(True, 1, 1, None)) as export_one:
            out = io.StringIO()
            with redirect_stdout(out):
                export_all_chats.main(argv)
            return out.getvalue(), export_one

    def test_list_chats_prints_choices_without_exporting(self):
        output, export_one = self._run_main(["--list-chats"])

        self.assertIn("[1] single Alice", output)
        self.assertIn("[2] group  Project Group", output)
        export_one.assert_not_called()

    def test_dry_run_does_not_export(self):
        output, export_one = self._run_main(["--dry-run", "--chats", "Alice"])

        self.assertIn("预览", output)
        self.assertIn("Alice", output)
        export_one.assert_not_called()

    def test_chats_limits_export_scope(self):
        _, export_one = self._run_main(["--chats", "Project"])

        export_one.assert_called_once()
        self.assertEqual(export_one.call_args.args[0], "12345@chatroom")


class ExportPlanCsvTests(unittest.TestCase):
    def test_writes_utf8_sig_csv_without_contact_remark_or_nickname(self):
        row = {
            "index": 1,
            "username": "wxid_alice",
            "chat_name": '张三, "A"',
            "chat_type": "single",
            "message_count": 3,
            "first_time": "2026-05-01 00:00:00",
            "last_time": "2026-05-02 00:00:00",
            "attachment_estimated_bytes": 12,
            "attachment_scanned_bytes": "",
            "total_estimated_bytes": 20,
            "size_status": "ok",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "plan.csv")

            export_all_chats._write_plan_csv(path, [row])

            with open(path, "rb") as f:
                self.assertTrue(f.read(3).startswith(b"\xef\xbb\xbf"))
            with open(path, newline="", encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(export_all_chats.PLAN_CSV_FIELDS, list(rows[0].keys()))
            self.assertEqual(rows[0]["export"], "1")
            self.assertEqual(rows[0]["chat_name"], '张三, "A"')
            self.assertNotIn("contact_remark", rows[0])
            self.assertNotIn("contact_nick_name", rows[0])

    def test_whitelist_plan_csv_defaults_to_export_zero(self):
        row = {
            "index": 1,
            "username": "wxid_alice",
            "chat_name": "Alice",
            "chat_type": "single",
            "message_count": 0,
            "first_time": "",
            "last_time": "",
            "attachment_estimated_bytes": 0,
            "attachment_scanned_bytes": "",
            "total_estimated_bytes": 0,
            "size_status": "ok",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "plan.csv")

            export_all_chats._write_plan_csv(
                path, [row], plan_mode="whitelist"
            )

            with open(path, newline="", encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))

        self.assertEqual(rows[0]["export"], "0")

    def test_loads_all_rows_except_explicit_export_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "plan.csv")
            _write_csv(path, [
                {"export": "1", "username": "wxid_alice", "chat_name": "Alice"},
                {"export": "0", "username": "wxid_bob", "chat_name": "Bob"},
                {"export": "", "username": "12345@chatroom", "chat_name": "Group"},
                {"export": "yes", "username": "wxid_carl", "chat_name": "Carl"},
            ])

            selected = export_all_chats._load_selected_usernames_from_plan_csv(
                path, {
                    "wxid_alice", "wxid_bob", "12345@chatroom", "wxid_carl"
                }
            )

        self.assertEqual(selected, ["wxid_alice", "12345@chatroom", "wxid_carl"])

    def test_whitelist_plan_mode_loads_only_explicit_export_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "plan.csv")
            no_export = os.path.join(tmp, "no_export.csv")
            _write_csv(path, [
                {"export": "1", "username": "wxid_alice", "chat_name": "Alice"},
                {"export": "0", "username": "wxid_bob", "chat_name": "Bob"},
                {"export": "", "username": "12345@chatroom", "chat_name": "Group"},
                {"export": "yes", "username": "wxid_carl", "chat_name": "Carl"},
            ])
            _write_custom_csv(no_export, ["username", "chat_name"], [
                {"username": "wxid_alice", "chat_name": "Alice"},
            ])

            selected = export_all_chats._load_selected_usernames_from_plan_csv(
                path,
                {"wxid_alice", "wxid_bob", "12345@chatroom", "wxid_carl"},
                plan_mode="whitelist",
            )
            selected_without_export = (
                export_all_chats._load_selected_usernames_from_plan_csv(
                    no_export, {"wxid_alice"}, plan_mode="whitelist"
                )
            )

        self.assertEqual(selected, ["wxid_alice"])
        self.assertEqual(selected_without_export, [])

    def test_missing_export_column_exports_all_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "plan.csv")
            _write_custom_csv(path, ["username", "chat_name"], [
                {"username": "wxid_alice", "chat_name": "Alice"},
                {"username": "12345@chatroom", "chat_name": "Group"},
            ])

            selected = export_all_chats._load_selected_usernames_from_plan_csv(
                path, {"wxid_alice", "12345@chatroom"}
            )

        self.assertEqual(selected, ["wxid_alice", "12345@chatroom"])

    def test_rejects_duplicate_and_missing_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            duplicate = os.path.join(tmp, "duplicate.csv")
            missing = os.path.join(tmp, "missing.csv")
            _write_csv(duplicate, [
                {"export": "0", "username": "wxid_alice"},
                {"export": "1", "username": "wxid_alice"},
            ])
            _write_csv(missing, [{"export": "1", "username": "wxid_missing"}])

            with self.assertRaisesRegex(ValueError, "重复"):
                export_all_chats._load_selected_usernames_from_plan_csv(
                    duplicate, {"wxid_alice"}
                )
            with self.assertRaisesRegex(ValueError, "不存在"):
                export_all_chats._load_selected_usernames_from_plan_csv(
                    missing, {"wxid_alice"}
                )


class ExportPlanStatsTests(unittest.TestCase):
    def test_collects_message_resource_and_voice_estimates_with_date_filter(self):
        username = "wxid_alice"
        table_name = "Msg_" + hashlib.md5(username.encode()).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            msg_db = os.path.join(tmp, "message_0.db")
            resource_db = os.path.join(tmp, "message_resource.db")
            media_db = os.path.join(tmp, "media_0.db")
            _create_message_db(msg_db, table_name)
            _create_resource_db(resource_db, username)
            _create_media_db(media_db, username)

            with patch.object(export_all_chats, "_get_message_resource_db_path",
                              return_value=resource_db), \
                 patch.object(export_all_chats.mcp_server, "_iter_media_db_paths",
                              return_value=[media_db]):
                stats = export_all_chats._collect_chat_plan_stats(
                    username,
                    [{"db_path": msg_db, "table_name": table_name}],
                    start_ts=150,
                    end_ts=None,
                    size_mode="estimate",
                )

        self.assertEqual(stats["message_count"], 1)
        self.assertEqual(stats["message_body_bytes"], 6)
        self.assertEqual(stats["attachment_estimated_bytes"], 13)
        self.assertEqual(stats["attachment_scanned_bytes"], "")
        self.assertEqual(stats["total_estimated_bytes"], 19)
        self.assertEqual(stats["first_time"], "1970-01-01 08:03:20")
        self.assertEqual(stats["last_time"], "1970-01-01 08:03:20")
        self.assertEqual(stats["size_status"], "ok")

    def test_build_plan_rows_uses_batched_stats_without_per_chat_table_lookup(self):
        chat_rows = [
            {
                "index": 1,
                "username": "wxid_alice",
                "display_name": "Alice",
                "kind": "single",
                "remark": "Alice Remark",
                "nick_name": "Alice Nick",
            }
        ]
        stats = {
            "wxid_alice": {
                "message_count": 2,
                "message_body_bytes": 11,
                "first_time": "2026-01-01 00:00:00",
                "last_time": "2026-01-02 00:00:00",
                "attachment_estimated_bytes": 5,
                "attachment_scanned_bytes": "",
                "total_estimated_bytes": 16,
                "size_status": "ok",
            }
        }

        with patch.object(export_all_chats, "_collect_all_plan_stats",
                          return_value=stats), \
             patch.object(export_all_chats.mcp_server, "_find_msg_tables_for_user",
                          side_effect=AssertionError("per-chat lookup too slow")):
            rows = export_all_chats._build_plan_csv_rows(chat_rows)

        self.assertEqual(rows[0]["username"], "wxid_alice")
        self.assertNotIn("contact_remark", rows[0])
        self.assertNotIn("contact_nick_name", rows[0])
        self.assertEqual(rows[0]["message_count"], 2)
        self.assertEqual(rows[0]["total_estimated_bytes"], 16)


class ExportAllChatsCliPlanCsvTests(unittest.TestCase):
    def _base_patches(self):
        return (
            patch.object(export_all_chats.mcp_server, "DECRYPTED_DIR", "decrypted"),
            patch.object(export_all_chats.os.path, "exists", return_value=True),
            patch.object(export_all_chats, "_load_session_usernames",
                         return_value=["wxid_alice", "12345@chatroom"]),
            patch.object(export_all_chats.mcp_server, "get_contact_names",
                         return_value={
                             "wxid_alice": "Alice",
                             "12345@chatroom": "Project Group",
                         }),
            patch.object(export_all_chats.mcp_server, "get_contact_full",
                         return_value=[]),
        )

    def test_write_plan_csv_does_not_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "plan.csv")
            patches = self._base_patches()
            with patches[0], patches[1], patches[2], patches[3], \
                 patch.object(export_all_chats, "_build_plan_csv_rows",
                              return_value=[{
                                  "index": 1,
                                  "username": "wxid_alice",
                                  "chat_name": "Alice",
                                  "chat_type": "single",
                                  "message_count": 0,
                                  "first_time": "",
                                  "last_time": "",
                                  "attachment_estimated_bytes": 0,
                                  "attachment_scanned_bytes": "",
                                  "total_estimated_bytes": 0,
                                  "size_status": "ok",
                              }]), \
                 patch.object(export_all_chats, "export_one") as export_one:
                out = io.StringIO()
                with redirect_stdout(out):
                    export_all_chats.main(["--write-plan-csv", path])

            export_one.assert_not_called()
            self.assertTrue(os.path.exists(path))

    def test_write_plan_csv_passes_whitelist_plan_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "plan.csv")
            patches = self._base_patches()
            with patches[0], patches[1], patches[2], patches[3], \
                 patch.object(export_all_chats, "_build_plan_csv_rows",
                              return_value=[]), \
                 patch.object(export_all_chats, "_write_plan_csv") as write_csv:
                out = io.StringIO()
                with redirect_stdout(out):
                    export_all_chats.main([
                        "--write-plan-csv", path, "--plan-mode", "whitelist"
                    ])

            write_csv.assert_called_once()
            self.assertEqual(write_csv.call_args.kwargs["plan_mode"], "whitelist")
            self.assertIn("白名单模式", out.getvalue())

    def test_write_plan_csv_reports_write_error_without_traceback(self):
        patches = self._base_patches()
        with patches[0], patches[1], patches[2], patches[3], \
             patch.object(export_all_chats, "_build_plan_csv_rows",
                          return_value=[]), \
             patch.object(export_all_chats, "_write_plan_csv",
                          side_effect=PermissionError("locked")):
            err = io.StringIO()
            with redirect_stderr(err):
                with self.assertRaises(SystemExit) as cm:
                    export_all_chats.main(["--write-plan-csv", "plan.csv"])

        self.assertEqual(cm.exception.code, 1)
        self.assertIn("写入导出计划 CSV 失败", err.getvalue())

    def test_from_plan_csv_exports_only_selected_username(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = os.path.join(tmp, "plan.csv")
            _write_csv(plan, [
                {"export": "1", "username": "wxid_alice", "chat_name": "Alice"},
                {"export": "0", "username": "12345@chatroom", "chat_name": "Group"},
            ])
            patches = self._base_patches()
            with patches[0], patches[1], patches[2], patches[3], \
                 patch.object(export_all_chats, "export_one",
                              return_value=(True, 1, 1, None)) as export_one:
                out = io.StringIO()
                with redirect_stdout(out):
                    export_all_chats.main([tmp, "--from-plan-csv", plan])

            export_one.assert_called_once()
            self.assertEqual(export_one.call_args.args[0], "wxid_alice")
            output = out.getvalue()
            self.assertIn("开始导出", output)
            self.assertIn("完成导出", output)

    def test_from_plan_csv_whitelist_exports_only_explicit_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = os.path.join(tmp, "plan.csv")
            _write_csv(plan, [
                {"export": "1", "username": "wxid_alice", "chat_name": "Alice"},
                {"export": "", "username": "12345@chatroom", "chat_name": "Group"},
            ])
            patches = self._base_patches()
            with patches[0], patches[1], patches[2], patches[3], \
                 patch.object(export_all_chats, "export_one",
                              return_value=(True, 1, 1, None)) as export_one:
                out = io.StringIO()
                with redirect_stdout(out):
                    export_all_chats.main([
                        tmp, "--from-plan-csv", plan,
                        "--plan-mode", "whitelist",
                    ])

            export_one.assert_called_once()
            self.assertEqual(export_one.call_args.args[0], "wxid_alice")
            self.assertIn("本次选择: 1 个会话", out.getvalue())

    def test_from_plan_csv_dry_run_does_not_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = os.path.join(tmp, "plan.csv")
            _write_csv(plan, [
                {"export": "1", "username": "wxid_alice", "chat_name": "Alice"},
            ])
            patches = self._base_patches()
            with patches[0], patches[1], patches[2], patches[3], \
                 patch.object(export_all_chats, "export_one") as export_one:
                out = io.StringIO()
                with redirect_stdout(out):
                    export_all_chats.main([
                        tmp, "--from-plan-csv", plan, "--dry-run"
                    ])

            self.assertIn("Alice", out.getvalue())
            export_one.assert_not_called()


class ExportOneMetadataTests(unittest.TestCase):
    def test_single_chat_json_includes_contact_metadata_and_message_dates(self):
        username = "wxid_zhangsan"
        table_name = "Msg_" + hashlib.md5(username.encode()).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "message_0.db")
            _create_export_message_db(db_path, table_name)
            ctx = {
                "username": username,
                "display_name": "张三",
                "message_tables": [{"db_path": db_path, "table_name": table_name}],
                "is_group": False,
            }
            with patch.object(export_all_chats.mcp_server, "_resolve_chat_context",
                              return_value=ctx), \
                 patch.object(export_all_chats, "_resolve_sender",
                              side_effect=["me", "张三备注"]), \
                 patch.object(export_all_chats, "_extract_content",
                              side_effect=[("你好", None), ("收到", None)]), \
                 patch.object(export_all_chats.mcp_server, "get_contact_full",
                              return_value=[{
                                  "username": username,
                                  "remark": "张三备注",
                                  "nick_name": "张三昵称",
                                  "phone": "13800000000",
                                  "description": "重要客户",
                              }]), \
                 patch.object(export_all_chats.mcp_server,
                              "get_contact_tag_names_by_username",
                              return_value={username: ["客户", "北京"]}):
                ok, total, new_count, reason = export_all_chats.export_one(
                    username, tmp, {"wxid_zhangsan": "张三"}
                )

            self.assertTrue(ok, reason)
            self.assertEqual(total, 2)
            self.assertEqual(new_count, 2)
            out_path = os.path.join(tmp, "single_张三.json")
            with open(out_path, encoding="utf-8") as f:
                data = json.load(f)

        self.assertEqual(data["contact_remark"], "张三备注")
        self.assertEqual(data["contact_nick_name"], "张三昵称")
        self.assertEqual(data["contact_phone"], "13800000000")
        self.assertEqual(data["contact_tags"], ["客户", "北京"])
        self.assertEqual(data["contact_memo"], "重要客户")
        self.assertEqual(data["date_first_msg"], "2026-05-01 08:00:00")
        self.assertEqual(data["date_last_msg"], "2026-05-01 08:01:00")


class ExportIndexTests(unittest.TestCase):
    def _ctx_for(self, username, display_name, db_path, table_name):
        return {
            "username": username,
            "display_name": display_name,
            "message_tables": [{"db_path": db_path, "table_name": table_name}],
            "is_group": False,
        }

    def test_incremental_export_renames_existing_file_when_remark_changes(self):
        username = "wxid_zhangsan"
        table_name = "Msg_" + hashlib.md5(username.encode()).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "message_0.db")
            _create_export_message_db(db_path, table_name)
            old_path = os.path.join(tmp, "single_张三.json")
            with open(old_path, "w", encoding="utf-8") as f:
                json.dump({
                    "chat": "张三",
                    "username": username,
                    "messages": [
                        {"local_id": 1, "timestamp": 1777593600,
                         "sender": "me", "content": "你好"}
                    ],
                }, f, ensure_ascii=False)

            with patch.object(export_all_chats.mcp_server, "_resolve_chat_context",
                              return_value=self._ctx_for(
                                  username, "张三-客户", db_path, table_name
                              )), \
                 patch.object(export_all_chats, "_resolve_sender",
                              return_value="张三备注"), \
                 patch.object(export_all_chats, "_extract_content",
                              return_value=("收到", None)), \
                 patch.object(export_all_chats, "_contact_metadata_for_export",
                              return_value={}):
                ok, total, new_count, reason = export_all_chats.export_one(
                    username, tmp, {username: "张三-客户"}, incremental=True
                )

            new_path = os.path.join(tmp, "single_张三-客户.json")
            index_path = os.path.join(tmp, "_export_index.json")

            self.assertTrue(ok, reason)
            self.assertFalse(os.path.exists(old_path))
            self.assertTrue(os.path.exists(new_path))
            self.assertEqual(total, 2)
            self.assertEqual(new_count, 1)
            with open(new_path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual([m["local_id"] for m in data["messages"]], [1, 2])
            with open(index_path, encoding="utf-8") as f:
                index = json.load(f)
            entry = index["chats"][username]
            self.assertEqual(entry["current_file"], "single_张三-客户.json")
            self.assertIn("single_张三.json", entry["previous_files"])

    def test_export_uses_username_suffix_when_display_name_file_belongs_to_other_chat(self):
        username = "wxid_zhangsan"
        table_name = "Msg_" + hashlib.md5(username.encode()).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "message_0.db")
            _create_export_message_db(db_path, table_name)
            occupied_path = os.path.join(tmp, "single_张三.json")
            with open(occupied_path, "w", encoding="utf-8") as f:
                json.dump({
                    "chat": "张三",
                    "username": "wxid_other",
                    "messages": [],
                }, f, ensure_ascii=False)

            with patch.object(export_all_chats.mcp_server, "_resolve_chat_context",
                              return_value=self._ctx_for(
                                  username, "张三", db_path, table_name
                              )), \
                 patch.object(export_all_chats, "_resolve_sender",
                              side_effect=["me", "张三备注"]), \
                 patch.object(export_all_chats, "_extract_content",
                              side_effect=[("你好", None), ("收到", None)]), \
                 patch.object(export_all_chats, "_contact_metadata_for_export",
                              return_value={}):
                ok, total, new_count, reason = export_all_chats.export_one(
                    username, tmp, {username: "张三"}
                )

            collision_path = os.path.join(tmp, "single_张三__wxid_zhangsan.json")

            self.assertTrue(ok, reason)
            self.assertEqual(total, 2)
            self.assertEqual(new_count, 2)
            self.assertTrue(os.path.exists(occupied_path))
            self.assertTrue(os.path.exists(collision_path))
            with open(occupied_path, encoding="utf-8") as f:
                occupied = json.load(f)
            with open(collision_path, encoding="utf-8") as f:
                exported = json.load(f)
            self.assertEqual(occupied["username"], "wxid_other")
            self.assertEqual(exported["username"], username)


def _write_csv(path, rows):
    fields = export_all_chats.PLAN_CSV_FIELDS
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            full = {field: "" for field in fields}
            full.update(row)
            writer.writerow(full)


def _write_custom_csv(path, fields, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _create_message_db(path, table_name):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(f"""
            CREATE TABLE [{table_name}] (
                create_time INTEGER,
                message_content TEXT,
                compress_content TEXT,
                packed_info_data BLOB
            )
        """)
        conn.execute(
            f"INSERT INTO [{table_name}] VALUES (?, ?, ?, ?)",
            (100, "abc", "zz", b"1234"),
        )
        conn.execute(
            f"INSERT INTO [{table_name}] VALUES (?, ?, ?, ?)",
            (200, "hello", None, b"1"),
        )
        conn.commit()


def _create_resource_db(path, username):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE ChatName2Id (user_name TEXT)")
        conn.execute("""
            CREATE TABLE MessageResourceInfo (
                message_id INTEGER,
                chat_id INTEGER,
                message_create_time INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE MessageResourceDetail (
                message_id INTEGER,
                size INTEGER
            )
        """)
        conn.execute("INSERT INTO ChatName2Id(rowid, user_name) VALUES (?, ?)",
                     (1, username))
        conn.execute("INSERT INTO MessageResourceInfo VALUES (?, ?, ?)",
                     (10, 1, 100))
        conn.execute("INSERT INTO MessageResourceInfo VALUES (?, ?, ?)",
                     (11, 1, 200))
        conn.execute("INSERT INTO MessageResourceDetail VALUES (?, ?)", (10, 7))
        conn.execute("INSERT INTO MessageResourceDetail VALUES (?, ?)", (11, 8))
        conn.commit()


def _create_media_db(path, username):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
        conn.execute("""
            CREATE TABLE VoiceInfo (
                chat_name_id INTEGER,
                local_id INTEGER,
                create_time INTEGER,
                voice_data BLOB
            )
        """)
        conn.execute("INSERT INTO Name2Id(rowid, user_name) VALUES (?, ?)",
                     (1, username))
        conn.execute("INSERT INTO VoiceInfo VALUES (?, ?, ?, ?)",
                     (1, 1, 100, b"abc"))
        conn.execute("INSERT INTO VoiceInfo VALUES (?, ?, ?, ?)",
                     (1, 2, 200, b"abcde"))
        conn.commit()


def _create_export_message_db(path, table_name):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
        conn.execute(f"""
            CREATE TABLE [{table_name}] (
                local_id INTEGER,
                local_type INTEGER,
                create_time INTEGER,
                real_sender_id INTEGER,
                message_content TEXT,
                WCDB_CT_message_content INTEGER
            )
        """)
        conn.execute("INSERT INTO Name2Id(rowid, user_name) VALUES (?, ?)",
                     (1, "self_wxid"))
        conn.execute("INSERT INTO Name2Id(rowid, user_name) VALUES (?, ?)",
                     (2, "wxid_zhangsan"))
        conn.execute(
            f"INSERT INTO [{table_name}] VALUES (?, ?, ?, ?, ?, ?)",
            (1, 1, 1777593600, 1, "你好", 0),
        )
        conn.execute(
            f"INSERT INTO [{table_name}] VALUES (?, ?, ?, ?, ?, ?)",
            (2, 1, 1777593660, 2, "收到", 0),
        )
        conn.commit()


if __name__ == "__main__":
    unittest.main()
