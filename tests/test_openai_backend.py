"""
issue #59: opt-in OpenAI Whisper API 后端的两条关键回归测试。

只测两件事：
1. 隐私契约: 文件 > 25MB 在调用 OpenAI SDK 之前就被拒绝（保证不会无意上传）
2. 缓存正确性: backend 不匹配的旧条目不会被命中（避免切后端时返回错后端结果）

其余路径要么琐碎（默认值读取）、要么坏掉时声音很大（SDK 错误、ImportError），
不再单独覆盖。
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import mcp_server


class _CacheIsolationMixin:
    """与 test_voice_transcription_cache.py 同款隔离：避免污染 module-level 缓存状态。"""

    def setUp(self):
        self._saved_cache = mcp_server._voice_transcription_cache
        self._saved_path = mcp_server.VOICE_TRANSCRIPTION_CACHE_FILE
        self._saved_warned = mcp_server._voice_transcription_save_warned

        mcp_server._voice_transcription_cache = None
        mcp_server._voice_transcription_save_warned = False

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        mcp_server.VOICE_TRANSCRIPTION_CACHE_FILE = os.path.join(
            self._tmp.name, "voice_transcriptions.json"
        )

    def tearDown(self):
        mcp_server._voice_transcription_cache = self._saved_cache
        mcp_server.VOICE_TRANSCRIPTION_CACHE_FILE = self._saved_path
        mcp_server._voice_transcription_save_warned = self._saved_warned


class OpenAIBackendPrivacyTests(unittest.TestCase):
    """隐私契约：超限文件必须在 OpenAI SDK 实例化之前就被拒绝。

    若有人把 size check 移到 OpenAI(api_key=...) 之后（即便仍在 upload 前），
    本测试会失败 —— 这层防御边界值得守住。
    """

    def test_oversize_audio_rejected_before_sdk_call(self):
        # 写一个 26MB 临时 WAV (用稀疏写法快速生成)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.seek(26 * 1024 * 1024)
            f.write(b"\0")
            big_path = f.name
        self.addCleanup(os.unlink, big_path)

        # 注入一个假的 openai 模块，保证 import 成功；OpenAI 构造函数若被调用即测试失败
        fake_openai = MagicMock()
        fake_openai.OpenAI = MagicMock(
            side_effect=AssertionError("OpenAI() must not be instantiated for oversize files")
        )
        fake_openai.AuthenticationError = type("AuthenticationError", (Exception,), {})
        fake_openai.RateLimitError = type("RateLimitError", (Exception,), {})
        fake_openai.APIError = type("APIError", (Exception,), {})

        with patch.dict(sys.modules, {"openai": fake_openai}):
            with self.assertRaises(RuntimeError) as ctx:
                mcp_server._transcribe_openai(big_path)

        self.assertIn("25MB", str(ctx.exception))
        fake_openai.OpenAI.assert_not_called()


class CacheBackendMatchTests(_CacheIsolationMixin, unittest.TestCase):
    """缓存正确性：backend 不匹配 → 视为 miss，避免切后端时返回错后端结果。"""

    def test_cache_hit_requires_backend_match(self):
        # 种入一条 openai 后端的缓存条目
        key = mcp_server._voice_transcription_cache_key("wxid_test", 42)
        cache = mcp_server._load_voice_transcription_cache()
        cache[key] = {
            "text": "openai-result",
            "language": "zh",
            "create_time": 1700000000,
            "backend": "openai",
            "model_size": "whisper-1",
        }
        mcp_server._save_voice_transcription_cache()

        # 当前后端是 local，应当 miss → 走转录流程而非返回 "openai-result"
        with patch.object(mcp_server, "TRANSCRIPTION_BACKEND", "local"), \
             patch.object(mcp_server, "OPENAI_API_KEY", ""), \
             patch.object(mcp_server, "resolve_username", return_value="wxid_test"), \
             patch.object(mcp_server, "_fetch_voice_row",
                          return_value=(b"\x02fake-silk-blob", 1700000001)), \
             patch.object(mcp_server, "_silk_to_wav",
                          return_value=("/tmp/fake.wav", 24000 * 2)), \
             patch.object(mcp_server, "_transcribe_local",
                          return_value={"text": "local-result", "language": "zh"}), \
             patch.dict(sys.modules, {"whisper": MagicMock(), "pysilk": MagicMock()}):
            result = mcp_server.transcribe_voice("test_contact", 42)

        # 没返回旧 openai 缓存，而是走了 local 转录流程
        self.assertNotIn("openai-result", result)
        self.assertIn("local-result", result)

        # 落盘的新条目应记录当前后端
        mcp_server._voice_transcription_cache = None
        reloaded = mcp_server._load_voice_transcription_cache()
        self.assertEqual(reloaded[key]["backend"], "local")
        self.assertEqual(reloaded[key]["text"], "local-result")


if __name__ == "__main__":
    unittest.main()
