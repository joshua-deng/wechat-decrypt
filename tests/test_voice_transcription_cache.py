import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

import mcp_server


class _CacheIsolationMixin:
    """所有测试共享：隔离 module-level 缓存状态 + 指向 tempdir 的 cache 文件。"""

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


class VoiceTranscriptionCachePersistenceTests(_CacheIsolationMixin, unittest.TestCase):
    """_load_voice_transcription_cache / _save_voice_transcription_cache 的持久化行为。"""

    def test_load_missing_file_returns_empty_dict(self):
        self.assertEqual(mcp_server._load_voice_transcription_cache(), {})

    def test_save_and_reload_roundtrip(self):
        cache = mcp_server._load_voice_transcription_cache()
        cache["wxid_foo:42"] = {
            "text": "你好",
            "language": "zh",
            "create_time": 1700000000,
            "model_size": "base",
        }
        mcp_server._save_voice_transcription_cache()

        # 强制下一次 load 从磁盘读
        mcp_server._voice_transcription_cache = None
        reloaded = mcp_server._load_voice_transcription_cache()
        self.assertEqual(reloaded["wxid_foo:42"]["text"], "你好")
        self.assertEqual(reloaded["wxid_foo:42"]["language"], "zh")

    def test_corrupt_file_returns_empty_dict(self):
        with open(mcp_server.VOICE_TRANSCRIPTION_CACHE_FILE, "w", encoding="utf-8") as f:
            f.write("{{ not valid json")
        self.assertEqual(mcp_server._load_voice_transcription_cache(), {})

    def test_non_dict_payload_returns_empty_dict(self):
        with open(mcp_server.VOICE_TRANSCRIPTION_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(["not", "a", "dict"], f)
        self.assertEqual(mcp_server._load_voice_transcription_cache(), {})

    def test_utf8_preserved_on_disk(self):
        # ensure_ascii=False 必须生效，否则中文会被转义成 \uXXXX
        cache = mcp_server._load_voice_transcription_cache()
        cache["wxid_bar:1"] = {"text": "中文测试", "language": "zh"}
        mcp_server._save_voice_transcription_cache()

        with open(mcp_server.VOICE_TRANSCRIPTION_CACHE_FILE, "rb") as f:
            raw = f.read()
        self.assertIn("中文测试".encode("utf-8"), raw)

    def test_save_without_prior_load_persists_empty_dict(self):
        # 从未 load 过就直接 save：应落盘一个空 dict，而不是静默丢弃。
        mcp_server._voice_transcription_cache = None
        mcp_server._save_voice_transcription_cache()
        self.assertTrue(os.path.exists(mcp_server.VOICE_TRANSCRIPTION_CACHE_FILE))
        with open(mcp_server.VOICE_TRANSCRIPTION_CACHE_FILE, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {})


class VoiceTranscriptionCacheAtomicityTests(_CacheIsolationMixin, unittest.TestCase):
    """原子写 + crash-during-save 行为。"""

    def test_write_is_atomic_via_rename(self):
        # 先写入一份已有缓存
        cache = mcp_server._load_voice_transcription_cache()
        cache["wxid_x:1"] = {"text": "initial", "language": "zh", "model_size": "base"}
        mcp_server._save_voice_transcription_cache()

        # 模拟：写 .tmp 正常但 os.replace 阶段失败
        original_replace = os.replace

        def flaky_replace(src, dst):
            raise OSError("disk full during rename")

        cache["wxid_x:1"] = {"text": "MUTATED", "language": "zh", "model_size": "base"}
        with patch.object(os, "replace", side_effect=flaky_replace):
            mcp_server._save_voice_transcription_cache()  # 不应抛

        # 磁盘上应仍然是 initial，不是 MUTATED，也不是损坏的半截文件
        with open(mcp_server.VOICE_TRANSCRIPTION_CACHE_FILE, encoding="utf-8") as f:
            disk = json.load(f)
        self.assertEqual(disk["wxid_x:1"]["text"], "initial")

        # .tmp 应该被清理，避免污染目录
        tmp_path = mcp_server.VOICE_TRANSCRIPTION_CACHE_FILE + ".tmp"
        # 注：patch 生效期间 os.replace 失败，finally 里会尝试 unlink
        _ = original_replace  # 防 lint 警告
        self.assertFalse(os.path.exists(tmp_path))

    def test_early_save_error_preserves_existing_file(self):
        # json.dump 在 .tmp 上抛异常时（模拟磁盘满 / 权限问题），主文件应保持原样；
        # 注意此测试不是"写到一半中断"而是"写前就失败"的场景。
        cache = mcp_server._load_voice_transcription_cache()
        cache["wxid_y:1"] = {"text": "survives", "language": "zh", "model_size": "base"}
        mcp_server._save_voice_transcription_cache()

        cache["wxid_y:1"] = {"text": "DO NOT SEE", "language": "zh", "model_size": "base"}

        def boom(*args, **kwargs):
            raise OSError("disk full")

        with patch.object(mcp_server.json, "dump", side_effect=boom):
            mcp_server._save_voice_transcription_cache()  # 静默降级，不抛

        # 主文件没被破坏：仍然可 json.load 出原先内容
        mcp_server._voice_transcription_cache = None
        reloaded = mcp_server._load_voice_transcription_cache()
        self.assertEqual(reloaded["wxid_y:1"]["text"], "survives")


class VoiceTranscriptionCacheConcurrencyTests(_CacheIsolationMixin, unittest.TestCase):
    """多线程下的 load/save 行为。"""

    def test_concurrent_load_returns_same_dict_instance(self):
        # 16 个线程同时触发首次 load，应当只实际化一份 dict（lock 生效）
        barrier = threading.Barrier(16)
        results = []
        results_lock = threading.Lock()

        def worker():
            barrier.wait()
            d = mcp_server._load_voice_transcription_cache()
            with results_lock:
                results.append(id(d))

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(set(results)), 1, "并发 load 应返回同一个 dict 对象")

    def test_concurrent_save_does_not_corrupt(self):
        # 多个线程同时 save，磁盘上最终文件必须是合法 JSON（原子写 + lock 保障）
        cache = mcp_server._load_voice_transcription_cache()
        for i in range(100):
            cache[f"wxid_z:{i}"] = {
                "text": f"msg-{i}",
                "language": "zh",
                "model_size": "base",
            }

        def worker():
            mcp_server._save_voice_transcription_cache()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        with open(mcp_server.VOICE_TRANSCRIPTION_CACHE_FILE, encoding="utf-8") as f:
            disk = json.load(f)  # 必须能解析
        self.assertEqual(len(disk), 100)


class TranscribeVoiceCacheHitTests(_CacheIsolationMixin, unittest.TestCase):
    """transcribe_voice 的缓存命中 / 失效路径。"""

    def _seed(self, key, entry):
        cache = mcp_server._load_voice_transcription_cache()
        cache[key] = entry
        mcp_server._save_voice_transcription_cache()

    def test_cache_hit_skips_fetch_and_transcribe(self):
        key = mcp_server._voice_transcription_cache_key("wxid_test", 7)
        self._seed(key, {
            "text": "缓存命中文本",
            "language": "zh",
            "create_time": 1700000000,
            "model_size": mcp_server.DEFAULT_WHISPER_MODEL,
        })

        with patch.object(mcp_server, "resolve_username", return_value="wxid_test") as mock_resolve, \
             patch.object(mcp_server, "_fetch_voice_row") as mock_fetch, \
             patch.object(mcp_server, "_silk_to_wav") as mock_silk, \
             patch.object(mcp_server, "_get_whisper_model") as mock_model:
            result = mcp_server.transcribe_voice("test_contact", 7)

        mock_resolve.assert_called_once_with("test_contact")
        mock_fetch.assert_not_called()
        mock_silk.assert_not_called()
        mock_model.assert_not_called()
        self.assertIn("缓存命中文本", result)
        self.assertIn("(zh)", result)

    def test_cache_hit_uses_placeholder_when_create_time_missing(self):
        # 旧条目若没有 create_time 字段，不应崩溃
        key = mcp_server._voice_transcription_cache_key("wxid_test", 8)
        self._seed(key, {
            "text": "历史条目",
            "language": "zh",
            "model_size": mcp_server.DEFAULT_WHISPER_MODEL,
        })

        with patch.object(mcp_server, "resolve_username", return_value="wxid_test"), \
             patch.object(mcp_server, "_fetch_voice_row") as mock_fetch:
            result = mcp_server.transcribe_voice("test_contact", 8)

        mock_fetch.assert_not_called()
        self.assertIn("历史条目", result)

    def test_cache_hit_returns_empty_text_without_retranscribing(self):
        # Whisper 返回空也要缓存；再次调用应直接返回空，不进入 miss 路径
        key = mcp_server._voice_transcription_cache_key("wxid_test", 9)
        self._seed(key, {
            "text": "",
            "language": "zh",
            "create_time": 1700000000,
            "model_size": mcp_server.DEFAULT_WHISPER_MODEL,
        })

        with patch.object(mcp_server, "resolve_username", return_value="wxid_test"), \
             patch.object(mcp_server, "_fetch_voice_row") as mock_fetch:
            result = mcp_server.transcribe_voice("test_contact", 9)

        mock_fetch.assert_not_called()
        self.assertIn("(zh)", result)

    def test_model_mismatch_is_treated_as_miss(self):
        # 缓存条目的 model_size 和当前 DEFAULT_WHISPER_MODEL 不一致时，
        # 不应命中；进入 miss 路径（这里无 whisper 依赖，应落到"缺少依赖"分支）。
        key = mcp_server._voice_transcription_cache_key("wxid_test", 10)
        self._seed(key, {
            "text": "旧模型结果",
            "language": "zh",
            "create_time": 1700000000,
            "model_size": "OUTDATED_MODEL",
        })

        with patch.object(mcp_server, "resolve_username", return_value="wxid_test"), \
             patch.dict("sys.modules", {"whisper": None}):
            # whisper=None 时 `import whisper` 触发 ImportError
            result = mcp_server.transcribe_voice("test_contact", 10)

        # 走了 miss 路径 → 返回缺依赖提示，而不是返回旧缓存文本
        self.assertNotIn("旧模型结果", result)
        self.assertIn("缺少依赖", result)

    def test_cache_key_handles_colon_in_username(self):
        # 若上游未来的 resolve_username 放出带 ':' 的 username，也不会和其他条目冲突
        key_a = mcp_server._voice_transcription_cache_key("wxid:foo", 1)
        key_b = mcp_server._voice_transcription_cache_key("wxid", 1)
        self.assertNotEqual(key_a, key_b)


if __name__ == "__main__":
    unittest.main()
