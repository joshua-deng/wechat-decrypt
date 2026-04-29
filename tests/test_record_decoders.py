"""Helper-level regression tests for the recorditem / decoder additions.

Focused on locking in the bugs fixed across PR #65's many review rounds so
they don't regress. Covers helpers that are easy to call in isolation:

- `_safe_basename`         path-traversal sanitize (round-4 high #1)
- `_md5_file_chunked`      streaming hash + size cap (round-6 medium #3)
- `_parse_message_content` group prefix stripping for both `:\n` and
                           `:<?xml`/`:<msg` shapes (round-7 high #1)
- `_parse_app_message_outer` retry-with-wider-limit only fires for
                           `<type>19</type>` content (round-5 medium #3)
- `_format_record_message_text` end-to-end expansion of a >20KB outer
                           type-19 message (round-5 high #1, round-2 P2-1)
- `_format_record_dataitem` per-datatype rendering for the 14 known
                           types incl. text / file / image / 视频号 etc.

The two MCP-tool wrappers (decode_file_message / decode_record_item) lean
heavily on module globals (WECHAT_BASE_DIR, _cache, MSG_DB_KEYS) and the
real wechat cache layout. They are exercised by real-data smoke runs in
the PR description rather than mocked here — mocking the entire wechat
cache tree would dwarf the actual logic under test.
"""

import hashlib
import os
import tempfile
import unittest

import mcp_server


# -------- _safe_basename ----------------------------------------------------


class SafeBasenameTests(unittest.TestCase):
    def test_normal_filename_passes(self):
        self.assertEqual(mcp_server._safe_basename('normal.pdf'), 'normal.pdf')
        self.assertEqual(
            mcp_server._safe_basename('Lec 4- 零和.pdf'), 'Lec 4- 零和.pdf'
        )
        self.assertEqual(
            mcp_server._safe_basename('file (1).pdf'), 'file (1).pdf'
        )

    def test_absolute_path_rejected(self):
        self.assertEqual(mcp_server._safe_basename('/etc/passwd'), '')

    def test_parent_dir_rejected(self):
        # Strict reject — should not return the basename 'sensitive'.
        self.assertEqual(mcp_server._safe_basename('../../sensitive'), '')
        self.assertEqual(mcp_server._safe_basename('..'), '')

    def test_path_separator_rejected(self):
        self.assertEqual(mcp_server._safe_basename('subdir/x.pdf'), '')
        self.assertEqual(mcp_server._safe_basename('a\\b\\c.pdf'), '')

    def test_nul_rejected(self):
        self.assertEqual(mcp_server._safe_basename('with\x00nul.pdf'), '')

    def test_empty_or_dot_rejected(self):
        self.assertEqual(mcp_server._safe_basename(''), '')
        self.assertEqual(mcp_server._safe_basename('.'), '')

    def test_inner_dots_pass(self):
        # 'file...with..dots.pdf' has no separator → fine.
        self.assertEqual(
            mcp_server._safe_basename('file...with..dots.pdf'),
            'file...with..dots.pdf',
        )


# -------- _md5_file_chunked -------------------------------------------------


class Md5FileChunkedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.write(b'x' * 1000)
        self.tmp.close()
        self.addCleanup(lambda: os.unlink(self.tmp.name))

    def test_happy_path_matches_hashlib(self):
        md5, err = mcp_server._md5_file_chunked(self.tmp.name)
        self.assertIsNone(err)
        self.assertEqual(md5, hashlib.md5(b'x' * 1000).hexdigest())

    def test_size_cap_rejects_oversized_file(self):
        md5, err = mcp_server._md5_file_chunked(self.tmp.name, max_size=500)
        self.assertIsNone(md5)
        self.assertIn('超过 md5 校验上限', err)

    def test_missing_file_returns_error(self):
        md5, err = mcp_server._md5_file_chunked('/tmp/no/such/path/here_xxx')
        self.assertIsNone(md5)
        self.assertIsNotNone(err)


# -------- _parse_message_content --------------------------------------------


class ParseMessageContentTests(unittest.TestCase):
    def test_legacy_newline_prefix_in_group(self):
        sender, text = mcp_server._parse_message_content(
            'wxid_abc:\n<msg>hi</msg>', 1, is_group=True
        )
        self.assertEqual(sender, 'wxid_abc')
        self.assertEqual(text, '<msg>hi</msg>')

    def test_xml_decl_inline_prefix_in_group(self):
        # round-7 high #1: 'sender:<?xml...' without newline
        sender, text = mcp_server._parse_message_content(
            'wxid_abc:<?xml version="1.0"?><msg>x</msg>', 1, is_group=True
        )
        self.assertEqual(sender, 'wxid_abc')
        self.assertTrue(text.startswith('<?xml'))

    def test_msg_inline_prefix_in_group(self):
        sender, text = mcp_server._parse_message_content(
            'wxid_abc:<msg>x</msg>', 1, is_group=True
        )
        self.assertEqual(sender, 'wxid_abc')
        self.assertEqual(text, '<msg>x</msg>')

    def test_private_chat_does_not_strip(self):
        sender, text = mcp_server._parse_message_content(
            'wxid_abc:<msg>x</msg>', 1, is_group=False
        )
        self.assertEqual(sender, '')
        self.assertEqual(text, 'wxid_abc:<msg>x</msg>')

    def test_bytes_content_returns_marker(self):
        sender, text = mcp_server._parse_message_content(b'\x00\x01', 1, is_group=False)
        self.assertEqual(sender, '')
        self.assertEqual(text, '(二进制内容)')


# -------- _parse_app_message_outer ------------------------------------------


class ParseAppMessageOuterTests(unittest.TestCase):
    def test_small_xml_uses_default_path(self):
        outer = '<msg><appmsg><type>5</type><title>x</title></appmsg></msg>'
        root = mcp_server._parse_app_message_outer(outer)
        self.assertIsNotNone(root)

    def test_oversized_non_record_xml_short_circuits(self):
        # round-5 medium #3: only <type>19</type> content should retry under
        # the wider 500K cap. A 25KB non-type-19 message must NOT be parsed
        # under the wider limit.
        outer = '<msg><appmsg><type>5</type><title>' + 'X' * 25000 + '</title></appmsg></msg>'
        root = mcp_server._parse_app_message_outer(outer)
        self.assertIsNone(root)

    def test_oversized_record_xml_retries(self):
        # type=19 content > 20KB should succeed under the wider cap.
        big_desc = 'A' * 25000
        outer = (
            '<msg><appmsg><type>19</type><title>x</title>'
            f'<recorditem><![CDATA[<recordinfo><title>x</title>'
            f'<datalist count="1"><dataitem datatype="1">'
            f'<datadesc>{big_desc}</datadesc></dataitem></datalist>'
            f'</recordinfo>]]></recorditem></appmsg></msg>'
        )
        self.assertGreater(len(outer), 20000)
        root = mcp_server._parse_app_message_outer(outer)
        self.assertIsNotNone(root)


# -------- _format_record_dataitem ------------------------------------------


class FormatRecordDataitemTests(unittest.TestCase):
    def _item(self, xml):
        import xml.etree.ElementTree as ET
        return ET.fromstring(xml)

    def test_text(self):
        item = self._item(
            '<dataitem datatype="1"><datadesc>hello world</datadesc></dataitem>'
        )
        self.assertEqual(mcp_server._format_record_dataitem(item), 'hello world')

    def test_file_with_title(self):
        item = self._item(
            '<dataitem datatype="8"><datatitle>report.pdf</datatitle></dataitem>'
        )
        self.assertEqual(
            mcp_server._format_record_dataitem(item), '[文件] report.pdf'
        )

    def test_image(self):
        item = self._item('<dataitem datatype="2"></dataitem>')
        self.assertEqual(mcp_server._format_record_dataitem(item), '[图片]')

    def test_finder_feed(self):
        # round-2 datatype 22 视频号
        item = self._item(
            '<dataitem datatype="22"><finderFeed><desc>video desc</desc></finderFeed></dataitem>'
        )
        self.assertEqual(
            mcp_server._format_record_dataitem(item), '[视频号] video desc'
        )

    def test_music(self):
        item = self._item(
            '<dataitem datatype="29"><datatitle>song</datatitle><datadesc>artist</datadesc></dataitem>'
        )
        self.assertEqual(
            mcp_server._format_record_dataitem(item), '[音乐] song - artist'
        )

    def test_unknown_datatype_falls_back_to_desc(self):
        item = self._item(
            '<dataitem datatype="99"><datadesc>fallback content</datadesc></dataitem>'
        )
        self.assertEqual(
            mcp_server._format_record_dataitem(item), 'fallback content'
        )

    def test_unknown_datatype_with_no_desc_uses_label(self):
        item = self._item('<dataitem datatype="999"></dataitem>')
        self.assertEqual(
            mcp_server._format_record_dataitem(item), '[未知类型 999]'
        )


# -------- _format_record_message_text end-to-end ---------------------------


class FormatRecordMessageTextTests(unittest.TestCase):
    def _outer_with_items(self, items_xml, title='Big card', is_chatroom=False):
        chatroom = '<isChatRoom>1</isChatRoom>' if is_chatroom else ''
        recordinfo = (
            f'<recordinfo><title>{title}</title>{chatroom}'
            f'<datalist count="{items_xml.count("<dataitem")}">{items_xml}</datalist>'
            f'</recordinfo>'
        )
        return (
            '<?xml version="1.0"?><msg><appmsg><title>x</title><type>19</type>'
            f'<recorditem><![CDATA[{recordinfo}]]></recorditem>'
            '</appmsg></msg>'
        )

    def test_large_outer_expands_via_app_message_path(self):
        # round-2 P2-1 + round-5 high #1: 大 outer 端到端必须能展开
        items_xml = ''.join(
            f'<dataitem datatype="1"><sourcename>S{i}</sourcename>'
            f'<sourcetime>2025-01-01 00:00</sourcetime>'
            f'<datadesc>{"X" * 600}</datadesc></dataitem>'
            for i in range(40)
        )
        outer = self._outer_with_items(items_xml)
        self.assertGreater(len(outer), 20000)
        out = mcp_server._format_app_message_text(
            outer,
            (19 << 32) | 49,
            False,
            'wxid_dummy',
            'dummy',
            {},
        )
        self.assertIsNotNone(out)
        self.assertIn('[聊天记录]', out)
        self.assertIn('共 40 条', out)
        # 每行带 0-based index
        self.assertIn('[0] ', out)
        self.assertIn('[1] ', out)

    def test_empty_datalist_marks_loading(self):
        # 空 datalist 应展示"（待加载）"而非"共 0 条"
        outer = (
            '<?xml version="1.0"?><msg><appmsg><title>x</title><type>19</type>'
            '<recorditem><![CDATA[<recordinfo><title>x</title>'
            '<isChatRoom>0</isChatRoom></recordinfo>]]></recorditem>'
            '</appmsg></msg>'
        )
        out = mcp_server._format_app_message_text(
            outer, (19 << 32) | 49, False, 'd', 'd', {}
        )
        self.assertIn('待加载', out)

    def test_chatroom_marker_appended(self):
        items_xml = (
            '<dataitem datatype="1"><sourcename>A</sourcename>'
            '<datadesc>hi</datadesc></dataitem>'
        )
        outer = self._outer_with_items(items_xml, title='G', is_chatroom=True)
        out = mcp_server._format_app_message_text(
            outer, (19 << 32) | 49, True, 'd', 'd', {}
        )
        self.assertIn('群聊转发', out)

    def test_overflow_truncation_marker(self):
        # > _RECORD_MAX_ITEMS dataitems should produce a
        # "…（还有 N 条未显示）" line.
        original_max = mcp_server._RECORD_MAX_ITEMS
        try:
            mcp_server._RECORD_MAX_ITEMS = 3
            items_xml = ''.join(
                f'<dataitem datatype="1"><datadesc>m{i}</datadesc></dataitem>'
                for i in range(7)
            )
            outer = self._outer_with_items(items_xml)
            out = mcp_server._format_app_message_text(
                outer, (19 << 32) | 49, False, 'd', 'd', {}
            )
            self.assertIn('还有 4 条未显示', out)
        finally:
            mcp_server._RECORD_MAX_ITEMS = original_max


if __name__ == '__main__':
    unittest.main()
