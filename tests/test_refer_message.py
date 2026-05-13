"""微信引用回复消息（appmsg type=57）解析鉴定测试。

旧逻辑直接把 refermsg/content 按 [:160] 截断当摘要，对 type=3 (图片) /
34 (语音) / 47 (动画表情) / 49 (嵌套卡片) 这些"二进制"被引用消息会渲染
成 cdnurl + aeskey + md5 一坨乱码 (issue #44 #45)。本组测试 pin 新行为：
按 refer_type 给 schema-aware 摘要，cdnurl / aeskey / md5 / cdnthumb /
voiceurl / externurl 全部不再泄漏到聊天历史。

合成 fixture：wxid_synth_a / wxid_synth_b / 12345@chatroom / Sender A/B /
svrid 1 + 0*18，无真实 PII。
"""
import unittest
import xml.etree.ElementTree as ET

import mcp_server


# ---------- 合成 fixture ----------

def _appmsg(refermsg_xml='', title='我的回复'):
    """组装一个最小 type=57 appmsg 元素。"""
    xml = (
        f'<msg><appmsg><type>57</type><title>{title}</title>'
        f'{refermsg_xml}</appmsg></msg>'
    )
    root = ET.fromstring(xml)
    return root.find('.//appmsg')


def _refermsg(refer_type, content, fromusr='wxid_synth_a',
              displayname='Sender A', svrid='1' + '0' * 18,
              chatusr='', createtime='1700000000'):
    return (
        '<refermsg>'
        f'<type>{refer_type}</type>'
        f'<svrid>{svrid}</svrid>'
        f'<fromusr>{fromusr}</fromusr>'
        f'<chatusr>{chatusr}</chatusr>'
        f'<displayname>{displayname}</displayname>'
        f'<createtime>{createtime}</createtime>'
        f'<content>{content}</content>'
        '</refermsg>'
    )


# ---------- 标签映射 ----------

class ReferInnerTypeLabelTests(unittest.TestCase):
    def test_known_refer_inner_labels(self):
        self.assertEqual(mcp_server._REFER_INNER_TYPE_LABEL['3'], '图片')
        self.assertEqual(mcp_server._REFER_INNER_TYPE_LABEL['34'], '语音')
        self.assertEqual(mcp_server._REFER_INNER_TYPE_LABEL['47'], '动画表情')
        self.assertEqual(mcp_server._REFER_INNER_TYPE_LABEL['49'], '链接/卡片')

    def test_known_inner_appmsg_labels(self):
        self.assertEqual(mcp_server._INNER_APPMSG_TYPE_LABEL['5'], '链接')
        self.assertEqual(mcp_server._INNER_APPMSG_TYPE_LABEL['6'], '文件')
        self.assertEqual(mcp_server._INNER_APPMSG_TYPE_LABEL['19'], '聊天记录')


# ---------- _extract_refer_info ----------

class ExtractReferInfoTests(unittest.TestCase):
    def test_full_fields_round_trip(self):
        appmsg = _appmsg(_refermsg('1', '原文本'), title='回复正文')
        info = mcp_server._extract_refer_info(appmsg)
        self.assertEqual(info['reply_text'], '回复正文')
        self.assertEqual(info['refer_type'], '1')
        self.assertEqual(info['refer_fromusr'], 'wxid_synth_a')
        self.assertEqual(info['refer_displayname'], 'Sender A')
        self.assertEqual(info['refer_svrid'], '1' + '0' * 18)
        self.assertEqual(info['refer_content'], '原文本')

    def test_missing_refermsg_returns_none(self):
        appmsg = _appmsg(refermsg_xml='', title='孤儿回复')
        self.assertIsNone(mcp_server._extract_refer_info(appmsg))


# ---------- _summarize_refer_content ----------

class SummarizeReferContentTests(unittest.TestCase):
    def test_text_returns_original(self):
        self.assertEqual(mcp_server._summarize_refer_content('1', '你好'), '你好')

    def test_text_truncates_to_max_len(self):
        long = '中' * 200
        out = mcp_server._summarize_refer_content('1', long, max_len=160)
        self.assertEqual(len(out), 161)  # 160 + '…'
        self.assertTrue(out.endswith('…'))

    def test_image_returns_label_not_xml(self):
        v2_image_xml = (
            '<msg><img cdnthumburl="http://cdn.example/leak_thumb" '
            'aeskey="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" '
            'md5="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" '
            'cdnurl="http://cdn.example/leak_main" /></msg>'
        )
        out = mcp_server._summarize_refer_content('3', v2_image_xml)
        self.assertEqual(out, '[图片]')
        # PII / 二进制元数据不能泄漏到摘要
        for leak in ('cdnurl', 'aeskey', 'md5', 'cdnthumb', 'leak_main'):
            self.assertNotIn(leak, out)

    def test_voice_returns_label(self):
        v_xml = '<msg><voicemsg voicelength="3300" '\
                'voiceurl="http://cdn.example/leak.silk" /></msg>'
        out = mcp_server._summarize_refer_content('34', v_xml)
        self.assertEqual(out, '[语音]')
        self.assertNotIn('voiceurl', out)

    def test_emoji_returns_label(self):
        out = mcp_server._summarize_refer_content(
            '47', '<msg><emoji md5="xx" externurl="leak.gif"/></msg>'
        )
        self.assertEqual(out, '[动画表情]')
        self.assertNotIn('externurl', out)
        self.assertNotIn('leak', out)

    def test_nested_link_card_summary(self):
        nested = '<msg><appmsg><type>5</type><title>分享标题</title>'\
                 '<url>http://example.com/leak</url></appmsg></msg>'
        out = mcp_server._summarize_refer_content('49', nested)
        self.assertEqual(out, '[链接] 分享标题')
        self.assertNotIn('http', out)
        self.assertNotIn('url', out)

    def test_nested_record_card_summary(self):
        nested = '<msg><appmsg><type>19</type><title>群聊天记录</title></appmsg></msg>'
        out = mcp_server._summarize_refer_content('49', nested)
        self.assertEqual(out, '[聊天记录] 群聊天记录')

    def test_nested_invalid_xml_falls_back_to_card(self):
        self.assertEqual(
            mcp_server._summarize_refer_content('49', '<msg><appmsg'),
            '[卡片]',
        )

    def test_unknown_refer_type_falls_back(self):
        out = mcp_server._summarize_refer_content('999', 'irrelevant')
        self.assertEqual(out, '[type=999]')

    def test_empty_content_with_known_type(self):
        self.assertEqual(mcp_server._summarize_refer_content('3', ''), '[图片]')

    def test_xxe_payload_rejected_in_nested(self):
        xxe = (
            '<!DOCTYPE foo [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
            '<msg><appmsg><type>5</type><title>&x;</title></appmsg></msg>'
        )
        out = mcp_server._summarize_refer_content('49', xxe)
        self.assertEqual(out, '[卡片]')


# ---------- _format_refer_message_text ----------

class FormatReferMessageTextTests(unittest.TestCase):
    def _names(self):
        return {'wxid_synth_a': 'Sender A', 'wxid_synth_b': 'Sender B'}

    def test_text_refer_in_1v1(self):
        appmsg = _appmsg(_refermsg('1', '你吃了吗'), title='吃了')
        out = mcp_server._format_refer_message_text(
            appmsg, is_group=False, chat_username='wxid_synth_a',
            chat_display_name='Sender A', names=self._names(),
        )
        self.assertEqual(out, '吃了\n  ↳ 回复 Sender A: 你吃了吗')

    def test_image_refer_uses_label_not_xml_payload(self):
        v2_image = (
            '&lt;msg&gt;&lt;img cdnurl="leak" aeskey="leak" md5="leak"/&gt;&lt;/msg&gt;'
        )
        appmsg = _appmsg(_refermsg('3', v2_image), title='这张?')
        out = mcp_server._format_refer_message_text(
            appmsg, is_group=False, chat_username='wxid_synth_a',
            chat_display_name='Sender A', names=self._names(),
        )
        self.assertIn('[图片]', out)
        for leak in ('cdnurl', 'aeskey', 'md5'):
            self.assertNotIn(leak, out)

    def test_missing_refermsg_falls_back_to_title(self):
        appmsg = _appmsg(refermsg_xml='', title='孤儿回复')
        out = mcp_server._format_refer_message_text(
            appmsg, is_group=False, chat_username='wxid_synth_a',
            chat_display_name='Sender A', names={},
        )
        self.assertEqual(out, '孤儿回复')

    def test_empty_reply_uses_placeholder(self):
        appmsg = _appmsg(_refermsg('1', 'hi'), title='')
        out = mcp_server._format_refer_message_text(
            appmsg, is_group=False, chat_username='wxid_synth_a',
            chat_display_name='Sender A', names=self._names(),
        )
        self.assertTrue(out.startswith('[引用消息]'))


# ---------- 调度入口 ----------

class AppMessageDispatchReferTests(unittest.TestCase):
    def test_type57_dispatches_to_helper(self):
        # _format_app_message_text 的 type=57 分支必须走 _format_refer_message_text，
        # 不再走旧的 inline [:160] 截断。
        v2_image = '&lt;msg&gt;&lt;img cdnurl="leak_main"/&gt;&lt;/msg&gt;'
        content = (
            f'<msg><appmsg><type>57</type><title>看这个</title>'
            f'{_refermsg("3", v2_image)}</appmsg></msg>'
        )
        out = mcp_server._format_app_message_text(
            content, local_type=49, is_group=False,
            chat_username='wxid_synth_a', chat_display_name='Sender A', names={},
        )
        self.assertIn('[图片]', out)
        self.assertNotIn('leak_main', out)
        self.assertNotIn('cdnurl', out)


if __name__ == '__main__':
    unittest.main()
