"""Tests for `chat_export_helpers._extract_content` group prefix handling.

Issue #88: 群聊里的引用回复 / appmsg 卡片在 export_chat / export_all_chats
渲染成 link_or_file 且 content 为空。根因是 `_extract_content` 把带
`wxid_xxx:\\n` 群前缀的原始 content 直接喂给 `_format_app_message_text`，
XML 解析器在前缀文本上崩溃。

修复后：
- 检测到 chat_username 是 @chatroom，先用 `_parse_message_content` 剥前缀
- 把 `is_group=True` 透传给 `_format_app_message_text` 让引用回复的发送者
  标签解析走群路径
- 用真实的 contact names dict 而不是 `{}` 让 1-on-1 也能解出昵称
"""
import unittest
from unittest.mock import patch

import chat_export_helpers
import mcp_server


def _refer_appmsg(refer_content="hello world"):
    """合成一条引用回复 appmsg。"""
    return (
        '<msg><appmsg appid="" sdkver="0">'
        '<title>quote reply</title>'
        '<type>57</type>'
        '<refermsg>'
        '<type>1</type>'
        f'<content>{refer_content}</content>'
        '<fromusr>wxid_orig_sender</fromusr>'
        '<displayname>Original Sender</displayname>'
        '</refermsg>'
        '</appmsg></msg>'
    )


class ExtractContentGroupPrefixTests(unittest.TestCase):
    def setUp(self):
        # Skip decompression
        self._patch = patch.object(
            mcp_server, '_decompress_content',
            side_effect=lambda content, ct: content,
        )
        self._patch.start()
        self._names_patch = patch.object(
            mcp_server, 'get_contact_names',
            return_value={'wxid_orig_sender': 'Alice'},
        )
        self._names_patch.start()

    def tearDown(self):
        self._patch.stop()
        self._names_patch.stop()

    def test_group_appmsg_with_prefix_renders_correctly(self):
        """Issue #88: 群引用回复带 'wxid_xxx:\\n' 前缀，需要正确剥离后再解析。"""
        prefixed = 'wxid_group_member:\n' + _refer_appmsg('hello group')
        rendered, extras = chat_export_helpers._extract_content(
            local_id=100, local_type=49, content=prefixed, ct=0,
            chat_username='12345@chatroom', chat_display_name='Test Group',
        )
        self.assertIsNotNone(rendered, "群引用回复不应该解析失败返回 None")
        self.assertIn('quote reply', rendered)
        self.assertIn('hello group', rendered, "被引用内容应该出现在渲染结果里")

    def test_one_on_one_appmsg_unaffected(self):
        """1-on-1 场景没有前缀，行为应该保持不变。"""
        rendered, _ = chat_export_helpers._extract_content(
            local_id=100, local_type=49, content=_refer_appmsg('hi'), ct=0,
            chat_username='wxid_friend', chat_display_name='Friend',
        )
        self.assertIsNotNone(rendered)
        self.assertIn('hi', rendered)

    def test_group_text_prefix_stripped(self):
        """群里的 base=1 text 消息，content 也带前缀，应该被剥掉。"""
        text, _ = chat_export_helpers._extract_content(
            local_id=100, local_type=1, content='wxid_xx:\nhello group',
            ct=0, chat_username='12345@chatroom', chat_display_name='Group',
        )
        self.assertEqual(text, 'hello group')

    def test_one_on_one_text_unaffected(self):
        """1-on-1 text 没有前缀概念，原样返回。"""
        text, _ = chat_export_helpers._extract_content(
            local_id=100, local_type=1, content='hello friend', ct=0,
            chat_username='wxid_friend', chat_display_name='Friend',
        )
        self.assertEqual(text, 'hello friend')

    def test_group_quote_uses_real_names(self):
        """群引用回复的发送者标签应该用真实 contact names 解析。"""
        prefixed = 'wxid_group_member:\n' + _refer_appmsg()
        rendered, _ = chat_export_helpers._extract_content(
            local_id=100, local_type=49, content=prefixed, ct=0,
            chat_username='12345@chatroom', chat_display_name='Test Group',
        )
        # is_group=True 走 group 分支：用 ref_user (wxid_orig_sender) 查 names
        # → 'Alice'。原先 names={} 会回退到 displayname。
        self.assertIn('Alice', rendered, "应该用 names dict 解析出 'Alice'")


if __name__ == "__main__":
    unittest.main()
