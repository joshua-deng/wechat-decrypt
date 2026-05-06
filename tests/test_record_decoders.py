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


# -------- WCPay transfer (appmsg type=2000) --------------------------------
#
# All fixtures use synthetic placeholder values — no real wxid / fee / id /
# memo. paysubtype semantics are community consensus from open-source wechat
# tooling; treat any "未识别" branch as forward-compatible degradation.


class TransferPaysubTypeLabelTests(unittest.TestCase):
    def test_known_subtypes_present(self):
        labels = mcp_server._TRANSFER_PAYSUBTYPE_LABEL
        self.assertEqual(labels['1'], '发起转账')
        self.assertEqual(labels['3'], '已收款')
        self.assertEqual(labels['4'], '已退还')
        # 5/7/8 are version-dependent variants — locked to current text so a
        # silent rename in mcp_server.py would surface here.
        self.assertEqual(labels['5'], '过期已退还')
        self.assertEqual(labels['7'], '待领取')
        self.assertEqual(labels['8'], '已领取')


def _transfer_appmsg(
    paysubtype='1',
    fee_desc='¥100.00',
    pay_memo='',
    payer='wxid_payer_synth',
    receiver='wxid_recv_synth',
    transferid='1' + '0' * 27,
    transcationid='1' + '0' * 27,
    begin_ts='1746528000',
    invalid_ts='1746614400',
    title='微信转账',
    des='请收钱',
    feedesc_tag='feedesc',
    paymemo_tag='pay_memo',
):
    """Build a synthetic appmsg type=2000 root element. All values are
    placeholder; tests never run against real wechat data."""
    import xml.etree.ElementTree as ET
    fee_node = f'<{feedesc_tag}>{fee_desc}</{feedesc_tag}>' if fee_desc else ''
    memo_node = f'<{paymemo_tag}>{pay_memo}</{paymemo_tag}>' if pay_memo else ''
    xml_text = (
        f'<msg><appmsg><title>{title}</title><des>{des}</des>'
        f'<type>2000</type>'
        f'<wcpayinfo>'
        f'<paysubtype>{paysubtype}</paysubtype>'
        f'{fee_node}{memo_node}'
        f'<transferid>{transferid}</transferid>'
        f'<transcationid>{transcationid}</transcationid>'
        f'<begintransfertime>{begin_ts}</begintransfertime>'
        f'<invalidtime>{invalid_ts}</invalidtime>'
        f'<payer_username>{payer}</payer_username>'
        f'<receiver_username>{receiver}</receiver_username>'
        f'</wcpayinfo></appmsg></msg>'
    )
    return ET.fromstring(xml_text), xml_text


class ExtractTransferInfoTests(unittest.TestCase):
    def test_full_fields_round_trip(self):
        root, _ = _transfer_appmsg(paysubtype='3', pay_memo='lunch split')
        appmsg = root.find('.//appmsg')
        info = mcp_server._extract_transfer_info(appmsg)
        self.assertIsNotNone(info)
        self.assertEqual(info['paysubtype'], '3')
        self.assertEqual(info['paysubtype_label'], '已收款')
        self.assertEqual(info['fee_desc'], '¥100.00')
        self.assertEqual(info['pay_memo'], 'lunch split')
        self.assertEqual(info['payer_username'], 'wxid_payer_synth')
        self.assertEqual(info['receiver_username'], 'wxid_recv_synth')
        self.assertEqual(info['begin_transfer_time'], '1746528000')
        self.assertEqual(info['invalid_time'], '1746614400')
        self.assertTrue(info['transfer_id'].startswith('1'))
        self.assertTrue(info['transcation_id'].startswith('1'))

    def test_missing_wcpayinfo_returns_none(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(
            '<msg><appmsg><title>x</title><type>2000</type></appmsg></msg>'
        )
        appmsg = root.find('.//appmsg')
        self.assertIsNone(mcp_server._extract_transfer_info(appmsg))

    def test_camelcase_feedesc_falls_back(self):
        # 部分微信版本字段名为 feeDesc 而非 feedesc
        root, _ = _transfer_appmsg(feedesc_tag='feeDesc')
        appmsg = root.find('.//appmsg')
        info = mcp_server._extract_transfer_info(appmsg)
        self.assertEqual(info['fee_desc'], '¥100.00')

    def test_camelcase_paymemo_falls_back(self):
        # paymemo (无下划线) 也是已知变体
        root, _ = _transfer_appmsg(pay_memo='note', paymemo_tag='paymemo')
        appmsg = root.find('.//appmsg')
        info = mcp_server._extract_transfer_info(appmsg)
        self.assertEqual(info['pay_memo'], 'note')

    def test_unknown_paysubtype_label_degraded(self):
        root, _ = _transfer_appmsg(paysubtype='99')
        appmsg = root.find('.//appmsg')
        info = mcp_server._extract_transfer_info(appmsg)
        self.assertEqual(info['paysubtype'], '99')
        self.assertIn('99', info['paysubtype_label'])

    def test_empty_paysubtype_label_empty(self):
        root, _ = _transfer_appmsg(paysubtype='')
        appmsg = root.find('.//appmsg')
        info = mcp_server._extract_transfer_info(appmsg)
        self.assertEqual(info['paysubtype_label'], '')


class FormatTransferMessageTextTests(unittest.TestCase):
    def test_initiate_with_amount(self):
        root, _ = _transfer_appmsg(paysubtype='1')
        appmsg = root.find('.//appmsg')
        out = mcp_server._format_transfer_message_text(appmsg, '微信转账')
        self.assertIn('[转账·发起转账]', out)
        self.assertIn('¥100.00', out)

    def test_received_with_memo(self):
        root, _ = _transfer_appmsg(paysubtype='3', pay_memo='lunch')
        appmsg = root.find('.//appmsg')
        out = mcp_server._format_transfer_message_text(appmsg, '微信转账')
        self.assertIn('[转账·已收款]', out)
        self.assertIn('备注: lunch', out)

    def test_missing_wcpayinfo_falls_back_to_title(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(
            '<msg><appmsg><title>微信转账</title><type>2000</type></appmsg></msg>'
        )
        appmsg = root.find('.//appmsg')
        out = mcp_server._format_transfer_message_text(appmsg, '微信转账')
        self.assertEqual(out, '[转账] 微信转账')

    def test_missing_fee_desc_safe(self):
        # 没有金额时也要给一行能看的输出，不能崩
        root, _ = _transfer_appmsg(paysubtype='4', fee_desc='')
        appmsg = root.find('.//appmsg')
        out = mcp_server._format_transfer_message_text(appmsg, '微信转账')
        self.assertIn('[转账·已退还]', out)


class AppMessageDispatchTransferTests(unittest.TestCase):
    """type=2000 must route through _format_transfer_message_text via
    _format_app_message_text (so get_chat_history / export_chat both pick it up)."""

    def test_dispatch_calls_transfer_helper(self):
        _, xml_text = _transfer_appmsg(paysubtype='3', pay_memo='dinner')
        out = mcp_server._format_app_message_text(
            xml_text, 49, False, 'wxid_dummy', 'dummy', {}
        )
        self.assertIsNotNone(out)
        self.assertIn('[转账·已收款]', out)
        self.assertIn('¥100.00', out)
        self.assertIn('备注: dinner', out)


if __name__ == '__main__':
    unittest.main()
