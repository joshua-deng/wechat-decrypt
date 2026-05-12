"""Tests for `_format_namecard_text` (msg_type=42 鉴定).

Before this helper, type=42 messages fell through the generic non-text branch
and emitted `[名片] <raw XML>`, dumping the full `<msg .../>` element including
antispamticket, biznamecardinfo and head-image URLs. Those tokens are PII that
should not be piped to downstream LLM / log systems.

These tests pin the new behaviour: a compact `[名片] <head>: <bio>` line,
without any source-only XML fields.
"""
import unittest

import mcp_server


# Realistic-shape sample with the noisy / sensitive attrs that used to leak.
_REAL_NAMECARD = (
    '<msg username="wxid_friend_demo" nickname="李雷" '
    'antispamticket="v2_abc123def456_should_not_leak" '
    'fullpy="lilei" shortpy="LL" alias="" '
    'imagestatus="3" scene="17" province="北京" city="海淀" sign="" '
    'sex="1" certflag="0" certinfo="搬砖工人 / 业余摄影" '
    'brandIconUrl="https://wx.qlogo.cn/should_not_leak" '
    'bigheadimgurl="https://wx.qlogo.cn/should_not_leak_big" '
    'smallheadimgurl="https://wx.qlogo.cn/should_not_leak_small" />'
)


class FormatNamecardTextTests(unittest.TestCase):
    def test_compact_line_for_real_namecard(self):
        out = mcp_server._format_namecard_text(_REAL_NAMECARD)
        self.assertEqual(out, "[名片] 李雷: 搬砖工人 / 业余摄影")

    def test_no_pii_or_url_in_output(self):
        out = mcp_server._format_namecard_text(_REAL_NAMECARD)
        self.assertNotIn("antispamticket", out)
        self.assertNotIn("v2_abc123def456", out)
        self.assertNotIn("qlogo.cn", out)
        self.assertNotIn("brandIconUrl", out)
        self.assertNotIn("headimgurl", out)

    def test_official_account_marked(self):
        xml = (
            '<msg username="gh_some_official" nickname="Some Official Account" '
            'certinfo="一个公众号" />'
        )
        out = mcp_server._format_namecard_text(xml)
        self.assertEqual(
            out, "[名片] Some Official Account (公众号 gh_some_official): 一个公众号"
        )

    def test_no_certinfo_falls_back_to_head_only(self):
        xml = '<msg username="wxid_demo" nickname="韩梅梅" />'
        out = mcp_server._format_namecard_text(xml)
        self.assertEqual(out, "[名片] 韩梅梅")

    def test_only_username_when_nickname_missing(self):
        xml = '<msg username="wxid_demo" nickname="" />'
        out = mcp_server._format_namecard_text(xml)
        self.assertEqual(out, "[名片] wxid_demo")

    def test_missing_both_identifiers_returns_none(self):
        xml = '<msg nickname="" username="" />'
        self.assertIsNone(mcp_server._format_namecard_text(xml))

    def test_broken_xml_returns_none(self):
        self.assertIsNone(mcp_server._format_namecard_text(""))
        self.assertIsNone(mcp_server._format_namecard_text("<msg "))
        self.assertIsNone(mcp_server._format_namecard_text("not xml at all"))


if __name__ == "__main__":
    unittest.main()
