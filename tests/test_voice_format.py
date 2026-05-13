"""Tests for `_format_voice_text` (msg_type=34 鉴定).

Voice messages previously rendered as a bare `[语音]` from the generic
non-text branch. LLMs reading chat history had no way to (a) judge whether a
clip was worth transcribing, or (b) call `decode_voice` without first round-
tripping through `get_voice_messages` to look up the `local_id`.

These tests pin the new behaviour: `[语音 Ns]` (duration to 1 decimal) when
the embedded `<voicemsg voicelength="…">` is parseable, with graceful
fallback to `[语音]` on missing / zero / malformed length.
"""
import unittest

import mcp_server


def _voice_xml(length_ms):
    return (
        f'<msg><voicemsg endflag="1" length="2048" voicelength="{length_ms}" '
        'clientmsgid="abc" fromusername="wxid_synth_a" '
        'cancelflag="0" voiceformat="4" forwardflag="0" /></msg>'
    )


class FormatVoiceTextTests(unittest.TestCase):
    def test_renders_duration_with_one_decimal(self):
        self.assertEqual(mcp_server._format_voice_text(_voice_xml(3300)), "[语音 3.3s]")

    def test_subsecond_voice(self):
        self.assertEqual(mcp_server._format_voice_text(_voice_xml(800)), "[语音 0.8s]")

    def test_long_clip(self):
        self.assertEqual(mcp_server._format_voice_text(_voice_xml(62000)), "[语音 62.0s]")

    def test_missing_voicelength_falls_back(self):
        xml = '<msg><voicemsg endflag="1" length="2048" /></msg>'
        self.assertEqual(mcp_server._format_voice_text(xml), "[语音]")

    def test_zero_voicelength_falls_back(self):
        self.assertEqual(mcp_server._format_voice_text(_voice_xml(0)), "[语音]")

    def test_non_numeric_voicelength_falls_back(self):
        xml = '<msg><voicemsg voicelength="abc" /></msg>'
        self.assertEqual(mcp_server._format_voice_text(xml), "[语音]")

    def test_empty_content(self):
        self.assertEqual(mcp_server._format_voice_text(""), "[语音]")
        self.assertEqual(mcp_server._format_voice_text(None), "[语音]")

    def test_missing_voicemsg_tag(self):
        self.assertEqual(mcp_server._format_voice_text("<msg></msg>"), "[语音]")

    def test_malformed_xml(self):
        self.assertEqual(mcp_server._format_voice_text("<msg><voicemsg"), "[语音]")

    def test_xxe_payload_rejected(self):
        xxe = (
            '<!DOCTYPE foo [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
            '<msg><voicemsg voicelength="1000" /></msg>'
        )
        self.assertEqual(mcp_server._format_voice_text(xxe), "[语音]")

    def test_end_to_end_format_message_text_with_voicelength(self):
        xml = _voice_xml(3300)
        _, text = mcp_server._format_message_text(
            local_id=72481, local_type=34, content=xml, is_group=False,
            chat_username="wxid_synth_a", chat_display_name="A", names={},
            create_time=1700000000,
        )
        self.assertEqual(text, "[语音 3.3s] (local_id=72481, ts=1700000000)")

    def test_end_to_end_without_voicelength(self):
        _, text = mcp_server._format_message_text(
            local_id=99, local_type=34, content="<msg></msg>", is_group=False,
            chat_username="wxid_synth_a", chat_display_name="A", names={},
            create_time=0,
        )
        self.assertEqual(text, "[语音] (local_id=99)")


if __name__ == "__main__":
    unittest.main()
