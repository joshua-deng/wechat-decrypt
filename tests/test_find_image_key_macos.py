"""单元测试：find_image_key_macos 派生算法 + 端到端 smoke。

不依赖真实微信数据；用 tempdir + 合成密文构造测试。
"""
import hashlib
import json
import multiprocessing
import os
import queue as _queue_mod
import tempfile
import unittest
from unittest.mock import patch

from Crypto.Cipher import AES

import find_image_key_macos as fkm


class NormalizeWxidTests(unittest.TestCase):
    def test_wxid_with_extra_segments_keeps_only_first(self):
        # wxid_<seg> 形式只保留第一段下划线之内的内容
        self.assertEqual(fkm.normalize_wxid("wxid_abc123_extra_more"), "wxid_abc123")

    def test_wxid_no_extra_segments(self):
        self.assertEqual(fkm.normalize_wxid("wxid_abc123"), "wxid_abc123")

    def test_account_with_4char_alnum_suffix_stripped(self):
        # macOS 路径常见：your_wxid_a1b2 → your_wxid
        self.assertEqual(fkm.normalize_wxid("your_wxid_a1b2"), "your_wxid")

    def test_account_without_recognizable_suffix_returned_asis(self):
        self.assertEqual(fkm.normalize_wxid("simple"), "simple")
        self.assertEqual(fkm.normalize_wxid("foo_bar_baz"), "foo_bar_baz")  # baz 是 3 char

    def test_empty_or_none_returns_empty(self):
        self.assertEqual(fkm.normalize_wxid(""), "")
        self.assertEqual(fkm.normalize_wxid(None), "")
        self.assertEqual(fkm.normalize_wxid("   "), "")


class DeriveImageKeysTests(unittest.TestCase):
    def test_xor_is_low_byte_of_code(self):
        xor, _ = fkm.derive_image_keys(0x12345678, "anything")
        self.assertEqual(xor, 0x78)

    def test_xor_handles_small_codes(self):
        self.assertEqual(fkm.derive_image_keys(0xFF, "x")[0], 0xFF)
        self.assertEqual(fkm.derive_image_keys(0x00, "x")[0], 0x00)

    def test_aes_is_md5_hex_truncated_to_16(self):
        # Golden value: 合成 fixture (uin=12345678) 派生; 算法正确性由公式
        # md5(str(uin)+wxid)[:16] 决定, 测试值无需对应任何真实账号。
        xor, aes = fkm.derive_image_keys(12345678, "your_wxid")
        self.assertEqual(xor, 0x4E)  # 12345678 & 0xFF
        self.assertEqual(aes, "a0c093edddc98490")

    def test_aes_does_not_normalize_wxid_internally(self):
        # 归一化由调用方负责；不同 wxid 字符串产出不同 key
        _, aes_full = fkm.derive_image_keys(12345678, "your_wxid_a1b2")
        _, aes_norm = fkm.derive_image_keys(12345678, "your_wxid")
        self.assertNotEqual(aes_full, aes_norm)


class DeriveKvcommDirCandidatesTests(unittest.TestCase):
    def test_canonical_macos_path_is_first_candidate(self):
        db_dir = (
            "/Users/x/Library/Containers/com.tencent.xinWeChat/Data/Documents/"
            "xwechat_files/wxid_abc/db_storage"
        )
        candidates = fkm.derive_kvcomm_dir_candidates(db_dir)
        self.assertGreater(len(candidates), 0)
        expected_primary = (
            "/Users/x/Library/Containers/com.tencent.xinWeChat/Data/Documents/"
            "app_data/net/kvcomm"
        )
        self.assertEqual(candidates[0], expected_primary)

    def test_returns_multiple_candidates(self):
        # 多候选是 Round 1 review 的关键修复点：跨版本路径覆盖
        db_dir = (
            "/Users/x/Library/Containers/com.tencent.xinWeChat/Data/Documents/"
            "xwechat_files/wxid_abc/db_storage"
        )
        candidates = fkm.derive_kvcomm_dir_candidates(db_dir)
        self.assertGreaterEqual(len(candidates), 3,
                                "应返回多个候选路径以覆盖不同微信版本布局")

    def test_no_xwechat_files_still_returns_home_fallback(self):
        # 即使无法从 db_dir 推算，也至少返回 HOME 默认路径作兜底
        candidates = fkm.derive_kvcomm_dir_candidates("/random/path")
        self.assertGreaterEqual(len(candidates), 1)
        self.assertTrue(any("Containers/com.tencent.xinWeChat" in c
                            for c in candidates))

    def test_candidates_are_unique(self):
        db_dir = "/x/y/Documents/xwechat_files/wxid_abc/db_storage"
        candidates = fkm.derive_kvcomm_dir_candidates(db_dir)
        self.assertEqual(len(candidates), len(set(candidates)))


class FindExistingKvcommDirTests(unittest.TestCase):
    def test_returns_first_existing_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 构造合法 db_dir 路径，在第一个候选位置创建实际目录
            base = os.path.join(tmp, "Documents", "xwechat_files", "wxid_x")
            db_dir = os.path.join(base, "db_storage")
            os.makedirs(db_dir)
            kvcomm = os.path.join(tmp, "Documents", "app_data", "net", "kvcomm")
            os.makedirs(kvcomm)

            self.assertEqual(fkm.find_existing_kvcomm_dir(db_dir), kvcomm)

    def test_returns_none_when_no_candidate_exists(self):
        # 即使 HOME fallback 候选也不存在时，应返回 None。
        # 隔离测试不能依赖宿主机有/无微信安装；patch expanduser 指向 tmp。
        with tempfile.TemporaryDirectory() as fake_home:
            with patch("os.path.expanduser", return_value=fake_home):
                self.assertIsNone(fkm.find_existing_kvcomm_dir("/nonexistent/x/y/z"))


class CollectKvcommCodesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.kvdir = self._tmp.name

    def _touch(self, name):
        with open(os.path.join(self.kvdir, name), "w") as f:
            f.write("")

    def test_extracts_code_from_filename(self):
        # 长格式: 模拟真实 kvcomm 缓存文件命名 (合成 ID/时间戳, 测 regex 提
        # uin 的能力, 不绑定任何真实账号)
        self._touch("key_12345678_1111111111_1_1700000000_22222_3600_input.statistic")
        self._touch("key_99999999_yyy_zzz.statistic")
        self.assertEqual(fkm.collect_kvcomm_codes(self.kvdir), [12345678, 99999999])

    def test_ignores_files_with_non_numeric_first_segment(self):
        self._touch("key_reportnow_12345678_xxx.statistic")
        self._touch("key_abc_def.statistic")
        self._touch("config.ini")
        self._touch("monitordata_x")
        self.assertEqual(fkm.collect_kvcomm_codes(self.kvdir), [])

    def test_dedupes_same_code_across_files(self):
        self._touch("key_42_a.statistic")
        self._touch("key_42_b.statistic")
        self.assertEqual(fkm.collect_kvcomm_codes(self.kvdir), [42])

    def test_missing_dir_returns_empty(self):
        self.assertEqual(fkm.collect_kvcomm_codes("/nonexistent/xxx"), [])

    def test_none_dir_returns_empty(self):
        self.assertEqual(fkm.collect_kvcomm_codes(None), [])


class CollectWxidCandidatesTests(unittest.TestCase):
    def test_returns_raw_and_normalized_when_different(self):
        db_dir = "/x/Documents/xwechat_files/your_wxid_a1b2/db_storage"
        self.assertEqual(fkm.collect_wxid_candidates(db_dir),
                         ["your_wxid_a1b2", "your_wxid"])

    def test_returns_one_when_normalize_is_identity(self):
        db_dir = "/x/Documents/xwechat_files/wxid_abc/db_storage"
        self.assertEqual(fkm.collect_wxid_candidates(db_dir), ["wxid_abc"])

    def test_no_xwechat_files_returns_empty(self):
        self.assertEqual(fkm.collect_wxid_candidates("/random/path"), [])

    def test_xwechat_files_at_end_returns_empty(self):
        self.assertEqual(fkm.collect_wxid_candidates("/x/xwechat_files"), [])


class VerifyAesKeyTests(unittest.TestCase):
    KEY = "a0c093edddc98490"

    def _encrypt(self, plaintext_16):
        return AES.new(self.KEY.encode("ascii"), AES.MODE_ECB).encrypt(plaintext_16)

    def test_jpeg_magic_passes(self):
        ct = self._encrypt(b"\xff\xd8\xff\xe0" + b"\x00" * 12)
        self.assertTrue(fkm.verify_aes_key(self.KEY, ct))

    def test_png_magic_passes(self):
        ct = self._encrypt(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
        self.assertTrue(fkm.verify_aes_key(self.KEY, ct))

    def test_gif_magic_passes(self):
        ct = self._encrypt(b"GIF89a" + b"\x00" * 10)
        self.assertTrue(fkm.verify_aes_key(self.KEY, ct))

    def test_wxgf_magic_passes(self):
        ct = self._encrypt(b"wxgf" + b"\x00" * 12)
        self.assertTrue(fkm.verify_aes_key(self.KEY, ct))

    def test_random_data_fails(self):
        self.assertFalse(fkm.verify_aes_key(self.KEY, bytes(range(16))))

    def test_wrong_length_template_fails(self):
        self.assertFalse(fkm.verify_aes_key(self.KEY, b"short"))
        self.assertFalse(fkm.verify_aes_key(self.KEY, b""))

    def test_short_aes_key_fails(self):
        self.assertFalse(fkm.verify_aes_key("short", b"\x00" * 16))

    def test_empty_aes_key_fails(self):
        self.assertFalse(fkm.verify_aes_key("", b"\x00" * 16))


class VerifyAesKeyAgainstAllTests(unittest.TestCase):
    """交叉验证：必须所有模板都通过才算命中（防短 magic 偶然碰撞）。"""

    KEY = "a0c093edddc98490"

    def _encrypt(self, plaintext_16):
        return AES.new(self.KEY.encode("ascii"), AES.MODE_ECB).encrypt(plaintext_16)

    def test_all_templates_pass(self):
        ct1 = self._encrypt(b"\xff\xd8\xff\xe0" + b"\x00" * 12)
        ct2 = self._encrypt(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
        self.assertTrue(fkm.verify_aes_key_against_all(self.KEY, [ct1, ct2]))

    def test_one_template_fails_overall_fails(self):
        ct1 = self._encrypt(b"\xff\xd8\xff\xe0" + b"\x00" * 12)  # passes
        ct2 = bytes(range(16))                                    # random, fails
        self.assertFalse(fkm.verify_aes_key_against_all(self.KEY, [ct1, ct2]))

    def test_empty_template_list_returns_false(self):
        # 没模板就不能验证；不视为通过（防"零样本=自动通过"陷阱）
        self.assertFalse(fkm.verify_aes_key_against_all(self.KEY, []))


class FindV2TemplateCiphertextsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def _build_v2_dat(self, name, ciphertext_16, subdir=""):
        target_dir = os.path.join(self.dir, subdir) if subdir else self.dir
        os.makedirs(target_dir, exist_ok=True)
        path = os.path.join(target_dir, name)
        with open(path, "wb") as f:
            f.write(fkm.V2_MAGIC + b"\x00" * 9 + ciphertext_16 + b"\x00\x00")
        return path

    def test_finds_one_template_in_v2_thumb(self):
        ct = bytes(range(0xF, 0x1F))
        self._build_v2_dat("abc_t.dat", ct)
        result = fkm.find_v2_template_ciphertexts(self.dir)
        self.assertEqual(result, [ct])

    def test_finds_multiple_distinct_templates(self):
        cts = [bytes([i] * 16) for i in (0x11, 0x22, 0x33)]
        for i, ct in enumerate(cts):
            self._build_v2_dat(f"chat{i}_t.dat", ct, subdir=f"chat{i}")
        result = fkm.find_v2_template_ciphertexts(self.dir, max_templates=3)
        self.assertEqual(set(result), set(cts))

    def test_dedupes_identical_templates(self):
        ct = b"\x42" * 16
        self._build_v2_dat("a_t.dat", ct, subdir="a")
        self._build_v2_dat("b_t.dat", ct, subdir="b")
        result = fkm.find_v2_template_ciphertexts(self.dir)
        self.assertEqual(result, [ct])

    def test_falls_back_to_any_dat_if_no_thumb(self):
        ct = b"\x33" * 16
        self._build_v2_dat("only_full.dat", ct)
        self.assertEqual(fkm.find_v2_template_ciphertexts(self.dir), [ct])

    def test_skips_non_v2_files(self):
        path = os.path.join(self.dir, "abc_t.dat")
        with open(path, "wb") as f:
            f.write(b"\x00" * 100)
        self.assertEqual(fkm.find_v2_template_ciphertexts(self.dir), [])

    def test_empty_dir_returns_empty(self):
        self.assertEqual(fkm.find_v2_template_ciphertexts(self.dir), [])

    def test_missing_dir_returns_empty(self):
        self.assertEqual(fkm.find_v2_template_ciphertexts("/nonexistent"), [])

    def test_walks_into_subdirs(self):
        ct = b"\x44" * 16
        self._build_v2_dat("x_t.dat", ct, subdir="sub/deeper")
        self.assertEqual(fkm.find_v2_template_ciphertexts(self.dir), [ct])

    def test_respects_max_templates(self):
        cts = [bytes([i] * 16) for i in range(10)]
        for i, ct in enumerate(cts):
            self._build_v2_dat(f"x{i}_t.dat", ct, subdir=f"d{i}")
        result = fkm.find_v2_template_ciphertexts(self.dir, max_templates=2)
        self.assertEqual(len(result), 2)


class FindImageKeyMacosIntegrationTests(unittest.TestCase):
    """端到端集成：合成 kvcomm 文件 + 合成 V2 模板 → 期望派生出已知 key。"""

    def _build_test_env(self, tmpdir, code, wxid_raw, num_templates=2):
        """构造测试环境，返回 (db_dir, expected_xor, expected_aes)。"""
        wxid_norm = fkm.normalize_wxid(wxid_raw)
        base = os.path.join(tmpdir, "Documents", "xwechat_files", wxid_raw)
        db_dir = os.path.join(base, "db_storage")
        os.makedirs(db_dir)

        kvcomm = os.path.join(tmpdir, "Documents", "app_data", "net", "kvcomm")
        os.makedirs(kvcomm)
        with open(os.path.join(kvcomm, f"key_{code}_x.statistic"), "w") as f:
            f.write("")

        xor_expected, aes_expected = fkm.derive_image_keys(code, wxid_norm)
        # 多个模板用不同的 plaintext 加密（仍是图像 magic 开头但内容不同）
        plaintexts = [
            b"\xff\xd8\xff\xe0" + b"\x00" * 12,           # JPEG
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 8,           # PNG
            b"GIF89a" + b"\x01\x02" + b"\x00" * 8,        # GIF
        ]
        for i in range(num_templates):
            pt = plaintexts[i % len(plaintexts)]
            ct = AES.new(aes_expected.encode("ascii"), AES.MODE_ECB).encrypt(pt)
            attach = os.path.join(base, "msg", "attach", f"chat{i}")
            os.makedirs(attach)
            with open(os.path.join(attach, f"img{i}_t.dat"), "wb") as f:
                f.write(fkm.V2_MAGIC + b"\x00" * 9 + ct + b"\x00\x00")
        return db_dir, xor_expected, aes_expected

    def test_full_flow_succeeds_with_normalized_wxid(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_dir, xor_exp, aes_exp = self._build_test_env(
                tmp, code=12345678, wxid_raw="your_wxid_a1b2", num_templates=3)
            result = fkm.find_image_key_macos(db_dir)
            self.assertIsNotNone(result, "派生应该成功")
            self.assertEqual(result, (xor_exp, aes_exp))

    def test_returns_none_when_no_kvcomm_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "Documents", "xwechat_files", "wxid_x")
            db_dir = os.path.join(base, "db_storage")
            os.makedirs(db_dir)
            self.assertIsNone(fkm.find_image_key_macos(db_dir))

    def test_returns_none_when_no_v2_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "Documents", "xwechat_files", "wxid_x")
            db_dir = os.path.join(base, "db_storage")
            os.makedirs(db_dir)
            kvcomm = os.path.join(tmp, "Documents", "app_data", "net", "kvcomm")
            os.makedirs(kvcomm)
            with open(os.path.join(kvcomm, "key_42_x.statistic"), "w") as f:
                f.write("")
            self.assertIsNone(fkm.find_image_key_macos(db_dir))

    def test_returns_none_when_no_combination_verifies(self):
        # 有 code 也有 V2 .dat，但密文是随机的，没有任何 key 能解出
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "Documents", "xwechat_files", "wxid_x")
            db_dir = os.path.join(base, "db_storage")
            os.makedirs(db_dir)
            kvcomm = os.path.join(tmp, "Documents", "app_data", "net", "kvcomm")
            os.makedirs(kvcomm)
            with open(os.path.join(kvcomm, "key_42_x.statistic"), "w") as f:
                f.write("")
            attach = os.path.join(base, "msg", "attach", "x")
            os.makedirs(attach)
            with open(os.path.join(attach, "x_t.dat"), "wb") as f:
                f.write(fkm.V2_MAGIC + b"\x00" * 9 + b"\xde\xad\xbe\xef" * 4 + b"\x00\x00")
            self.assertIsNone(fkm.find_image_key_macos(db_dir))

    def test_empty_db_dir_returns_none_without_crash(self):
        # 防御：空字符串、不合理路径不应抛异常。
        # patch expanduser 让 HOME fallback 也指向不存在的路径，避免
        # 测试在装了真实微信的开发机上意外深入到 wxid 缺失分支。
        with tempfile.TemporaryDirectory() as fake_home:
            with patch("os.path.expanduser", return_value=fake_home):
                self.assertIsNone(fkm.find_image_key_macos(""))


class MainShortCircuitTests(unittest.TestCase):
    """main() 短路：已有 image_aes_key 仍然有效时，不应重新派生 / 不应改写 config。"""

    def test_existing_valid_key_skips_derivation(self):
        with tempfile.TemporaryDirectory() as tmp:
            wxid = "wxid_abc"
            base = os.path.join(tmp, "Documents", "xwechat_files", wxid)
            db_dir = os.path.join(base, "db_storage")
            os.makedirs(db_dir)

            # kvcomm 里放个 code，证明若真去派生也能算出 key
            kvcomm = os.path.join(tmp, "Documents", "app_data", "net", "kvcomm")
            os.makedirs(kvcomm)
            code = 42
            with open(os.path.join(kvcomm, f"key_{code}_x.statistic"), "w") as f:
                f.write("")

            # 用真实派生的 key 加密 V2 模板，使现有 key 在该模板上能验证通过
            xor_exp, aes_exp = fkm.derive_image_keys(code, wxid)
            jpeg_pt = b"\xff\xd8\xff\xe0" + b"\x00" * 12
            ct = AES.new(aes_exp.encode("ascii"), AES.MODE_ECB).encrypt(jpeg_pt)
            attach = os.path.join(base, "msg", "attach", "x")
            os.makedirs(attach)
            with open(os.path.join(attach, "test_t.dat"), "wb") as f:
                f.write(fkm.V2_MAGIC + b"\x00" * 9 + ct + b"\x00\x00")

            # 写入"已有有效 key"的 config
            cfg_path = os.path.join(tmp, "config.json")
            cfg_initial = {
                "db_dir": db_dir,
                "image_aes_key": aes_exp,
                "image_xor_key": xor_exp,
                "extra_field": "must_be_preserved",  # 证明 main 不会重写
            }
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(cfg_initial, f)
            mtime_before = os.path.getmtime(cfg_path)

            # 关键：patch find_image_key_macos 让它若被误调用立刻可见
            with patch.object(fkm, "find_image_key_macos") as mock_derive:
                fkm.main(config_path=cfg_path)

            mock_derive.assert_not_called()  # 短路应直接 return，不进派生
            # config.json 不应被重写
            self.assertEqual(os.path.getmtime(cfg_path), mtime_before)
            with open(cfg_path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), cfg_initial)

    def test_existing_invalid_key_falls_through_to_derivation(self):
        with tempfile.TemporaryDirectory() as tmp:
            wxid = "wxid_abc"
            base = os.path.join(tmp, "Documents", "xwechat_files", wxid)
            db_dir = os.path.join(base, "db_storage")
            os.makedirs(db_dir)

            kvcomm = os.path.join(tmp, "Documents", "app_data", "net", "kvcomm")
            os.makedirs(kvcomm)
            code = 42
            with open(os.path.join(kvcomm, f"key_{code}_x.statistic"), "w") as f:
                f.write("")

            xor_exp, aes_exp = fkm.derive_image_keys(code, wxid)
            jpeg_pt = b"\xff\xd8\xff\xe0" + b"\x00" * 12
            ct = AES.new(aes_exp.encode("ascii"), AES.MODE_ECB).encrypt(jpeg_pt)
            attach = os.path.join(base, "msg", "attach", "x")
            os.makedirs(attach)
            with open(os.path.join(attach, "test_t.dat"), "wb") as f:
                f.write(fkm.V2_MAGIC + b"\x00" * 9 + ct + b"\x00\x00")

            cfg_path = os.path.join(tmp, "config.json")
            cfg_initial = {
                "db_dir": db_dir,
                "image_aes_key": "deadbeefdeadbeef",  # 故意写一个错的
            }
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(cfg_initial, f)

            fkm.main(config_path=cfg_path)

            # 短路应失败，进入派生路径，配置应被改写为正确的 key
            with open(cfg_path, encoding="utf-8") as f:
                cfg_after = json.load(f)
            self.assertEqual(cfg_after["image_aes_key"], aes_exp)
            self.assertEqual(cfg_after["image_xor_key"], xor_exp)


# ---------- 方案2 (wxid 后缀候选搜索, fallback) 单元测试 ---------- #

class ExtractWxidPartsTests(unittest.TestCase):
    """extract_wxid_parts: 从 db_dir 提 (full, norm, suffix)。"""

    def test_extracts_norm_and_suffix_from_alnum_suffix(self):
        db_dir = "/foo/Documents/xwechat_files/your_wxid_25d5/db_storage"
        self.assertEqual(
            fkm.extract_wxid_parts(db_dir),
            ("your_wxid_25d5", "your_wxid", "25d5"),
        )

    def test_wxid_format_with_4char_suffix(self):
        db_dir = "/foo/Documents/xwechat_files/wxid_abc_e2f4/db_storage"
        self.assertEqual(
            fkm.extract_wxid_parts(db_dir),
            ("wxid_abc_e2f4", "wxid_abc", "e2f4"),
        )

    def test_uppercase_suffix_lowercased(self):
        db_dir = "/foo/Documents/xwechat_files/your_wxid_ABCD/db_storage"
        result = fkm.extract_wxid_parts(db_dir)
        self.assertIsNotNone(result)
        self.assertEqual(result[2], "abcd")

    def test_no_4char_suffix_returns_none(self):
        # 6字符尾缀不匹配 _<4字符>$, 算法假设破灭
        db_dir = "/foo/Documents/xwechat_files/wxid_simple/db_storage"
        self.assertIsNone(fkm.extract_wxid_parts(db_dir))

    def test_no_xwechat_files_returns_none(self):
        self.assertIsNone(fkm.extract_wxid_parts("/random/path/db_storage"))


class DeriveXorKeyFromV2DatTests(unittest.TestCase):
    """derive_xor_key_from_v2_dat: 末字节投票反推 xor_key。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _write_v2_dat(self, name, last_byte, subdir=""):
        d = os.path.join(self.dir, subdir) if subdir else self.dir
        os.makedirs(d, exist_ok=True)
        body = (fkm.V2_MAGIC + b"\x00" * 9 + b"\x11" * 16
                + b"\x00" * 4 + bytes([last_byte]))
        with open(os.path.join(d, name), "wb") as f:
            f.write(body)

    def test_unanimous_vote(self):
        # 全部末字节 = 0xA6 → xor_key = 0xA6 ^ 0xD9 = 0x7F
        for i in range(10):
            self._write_v2_dat(f"x{i}_t.dat", 0xA6)
        self.assertEqual(fkm.derive_xor_key_from_v2_dat(self.dir),
                         (0x7F, 10, 10))

    def test_majority_vote_with_dissent(self):
        # 8 个 0xA6, 2 个 0x55: 多数 0x7F 胜出
        for i in range(8):
            self._write_v2_dat(f"good{i}_t.dat", 0xA6)
        for i in range(2):
            self._write_v2_dat(f"bad{i}_t.dat", 0x55)
        result = fkm.derive_xor_key_from_v2_dat(self.dir)
        self.assertEqual(result, (0x7F, 8, 10))

    def test_no_v2_dat_returns_none(self):
        self.assertIsNone(fkm.derive_xor_key_from_v2_dat(self.dir))

    def test_below_min_samples_returns_none(self):
        # 默认 min_samples=3, 仅有 2 个样本应被视为不可信
        for i in range(2):
            self._write_v2_dat(f"x{i}_t.dat", 0xA6)
        self.assertIsNone(fkm.derive_xor_key_from_v2_dat(self.dir))

    def test_missing_dir_returns_none(self):
        self.assertIsNone(fkm.derive_xor_key_from_v2_dat("/nonexistent"))

    def test_walks_into_subdirs(self):
        for i in range(10):
            self._write_v2_dat(f"x{i}_t.dat", 0xA6, subdir=f"deep/sub{i}")
        result = fkm.derive_xor_key_from_v2_dat(self.dir)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], 0x7F)

    def test_skips_non_v2_files(self):
        # 不是 V2 magic 的 .dat 不计入投票
        with open(os.path.join(self.dir, "junk.dat"), "wb") as f:
            f.write(b"NOT_V2" + b"\x00" * 30)
        for i in range(10):
            self._write_v2_dat(f"x{i}_t.dat", 0xA6)
        result = fkm.derive_xor_key_from_v2_dat(self.dir)
        self.assertEqual(result, (0x7F, 10, 10))


class BruteforceUinCandidatesTests(unittest.TestCase):
    """bruteforce_uin_candidates: 候选枚举 + md5 前缀匹配。

    注意：test_real_bruteforce_against_golden 单核 ~7-8 秒，全套测试耗时大头。
    """

    def test_real_bruteforce_against_golden(self):
        # 真跑全空间 2^24 候选, 同时验证: (a) 合成 uin 在结果里
        # (b) 候选数合理 (~256) (c) 候选都满足 xor_key 约束
        # md5("12345678")[:4] == "25d5", 12345678 & 0xff == 0x4E
        out = fkm.bruteforce_uin_candidates(0x4E, "25d5")
        self.assertIn(12345678, out, "合成 uin 应在候选里")
        self.assertTrue(200 <= len(out) <= 350,
                        f"候选数 {len(out)} 偏离 ~256 (理论 2^24/2^16)")
        for uin in out[:20]:
            self.assertEqual(uin & 0xFF, 0x4E,
                             f"uin {uin} 不满足 xor_key 约束")



class FindViaBruteforceTests(unittest.TestCase):
    """方案2 端到端 (合成 fixture, 多进程 worker 实跑)。

    注: parallel 路径在合成 uin (低数值, 在 worker 0 chunk 早期命中) 上
    < 0.2s 完成, 不需要 mock 加速。worker spawn 开销是真实集成测试的合理代价。
    """

    def _build_bruteforce_env(self, tmp, uin, wxid_norm, suffix):
        wxid_full = f"{wxid_norm}_{suffix}"
        base = os.path.join(tmp, "Documents", "xwechat_files", wxid_full)
        db_dir = os.path.join(base, "db_storage")
        attach_dir = os.path.join(base, "msg", "attach")
        os.makedirs(db_dir)
        os.makedirs(attach_dir)
        xor_exp, aes_exp = fkm.derive_image_keys(uin, wxid_norm)
        last_byte = 0xD9 ^ xor_exp
        jpeg_pt = b"\xff\xd8\xff\xe0" + b"\x00" * 12
        ct = AES.new(aes_exp.encode("ascii"), AES.MODE_ECB).encrypt(jpeg_pt)
        # 构造 10 个 V2 dat 让 derive_xor_key 投票稳定
        for i in range(10):
            with open(os.path.join(attach_dir, f"img{i}_t.dat"), "wb") as f:
                f.write(fkm.V2_MAGIC + b"\x00" * 9 + ct
                        + b"\x00" * 4 + bytes([last_byte]))
        return db_dir, attach_dir, xor_exp, aes_exp

    def test_full_flow_finds_synthetic_uin(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_dir, attach_dir, xor_exp, aes_exp = self._build_bruteforce_env(
                tmp, uin=12345678, wxid_norm="your_wxid", suffix="25d5")
            templates = fkm.find_v2_template_ciphertexts(attach_dir)
            result = fkm._find_via_bruteforce(db_dir, attach_dir, templates)
            self.assertEqual(result, (xor_exp, aes_exp))

    def test_returns_none_when_no_wxid_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "Documents", "xwechat_files", "wxid_nosuffix")
            db_dir = os.path.join(base, "db_storage")
            attach_dir = os.path.join(base, "msg", "attach")
            os.makedirs(db_dir)
            os.makedirs(attach_dir)
            self.assertIsNone(fkm._find_via_bruteforce(db_dir, attach_dir, []))

    def test_returns_none_when_no_v2_dat(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "Documents", "xwechat_files",
                                "your_wxid_25d5")
            db_dir = os.path.join(base, "db_storage")
            attach_dir = os.path.join(base, "msg", "attach")
            os.makedirs(db_dir)
            os.makedirs(attach_dir)
            self.assertIsNone(fkm._find_via_bruteforce(db_dir, attach_dir, []))


class DispatcherFallbackTests(unittest.TestCase):
    """find_image_key_macos dispatcher: 方案1 失败 → fallback 方案2。"""

    def test_kvcomm_missing_falls_back_to_bruteforce(self):
        with tempfile.TemporaryDirectory() as tmp:
            uin, wxid_norm, suffix = 12345678, "your_wxid", "25d5"
            wxid_full = f"{wxid_norm}_{suffix}"
            base = os.path.join(tmp, "Documents", "xwechat_files", wxid_full)
            db_dir = os.path.join(base, "db_storage")
            attach_dir = os.path.join(base, "msg", "attach")
            os.makedirs(db_dir)
            os.makedirs(attach_dir)

            # 不创建 kvcomm → 方案1 失败
            xor_exp, aes_exp = fkm.derive_image_keys(uin, wxid_norm)
            last_byte = 0xD9 ^ xor_exp
            jpeg_pt = b"\xff\xd8\xff\xe0" + b"\x00" * 12
            ct = AES.new(aes_exp.encode("ascii"), AES.MODE_ECB).encrypt(jpeg_pt)
            for i in range(10):
                with open(os.path.join(attach_dir, f"img{i}_t.dat"), "wb") as f:
                    f.write(fkm.V2_MAGIC + b"\x00" * 9 + ct
                            + b"\x00" * 4 + bytes([last_byte]))

            # patch HOME 让兜底 kvcomm 路径也找不到, 强制走方案2
            with patch("os.path.expanduser", return_value=tmp):
                result = fkm.find_image_key_macos(db_dir)
            self.assertEqual(result, (xor_exp, aes_exp))


class BruteforceParallelTests(unittest.TestCase):
    """方案2 多进程实现的两层覆盖:
    - 算法核心 (_bruteforce_worker_chunk): 直接调用, 无 process spawn, 极快
    - 集成 (_bruteforce_with_aes_parallel): workers=1 验证 spawn + pickle 链路

    多进程 e2e 由 FindViaBruteforceTests / DispatcherFallbackTests 间接覆盖
    (cpu_count workers, 真实 fixture)。这里只测函数契约, 避免 spawn 开销
    被反复支付。
    """

    @classmethod
    def setUpClass(cls):
        # 合成 fixture, 跨多个测试复用
        cls.uin = 12345678
        cls.xor_key = cls.uin & 0xFF  # 0x4E
        cls.wxid_norm = "your_wxid"
        cls.suffix_hex = hashlib.md5(str(cls.uin).encode()).hexdigest()[:4]
        cls.suffix_bytes = bytes.fromhex(cls.suffix_hex)
        cls.aes_hex = hashlib.md5(
            f"{cls.uin}{cls.wxid_norm}".encode()
        ).hexdigest()[:16]
        cls.template = AES.new(
            cls.aes_hex.encode("ascii"), AES.MODE_ECB
        ).encrypt(b"\xff\xd8\xff\xe0" + b"\x00" * 12)
        # i = (uin - xor_key) >> 8: worker 用 i 索引, 主进程倒推区间
        cls.target_i = (cls.uin - cls.xor_key) >> 8

    # 注: multiprocessing.Queue.put() 通过 feeder thread 异步刷到 pipe,
    # get_nowait() 读取会 race。所有 queue 读用 get(timeout=...):
    # - 命中场景: timeout=2s 给 feeder 充足时间 (实际 ~ms 级)
    # - 不命中场景: timeout=0.5s 既证空又不拖慢测试

    def test_worker_finds_known_uin_in_chunk(self):
        q = multiprocessing.Queue()
        fkm._bruteforce_worker_chunk(
            self.target_i - 50, self.target_i + 50,
            self.xor_key, self.suffix_bytes,
            self.wxid_norm.encode("ascii"),
            [self.template], q,
        )
        result = q.get(timeout=2)
        self.assertEqual(result, (self.uin, self.aes_hex))

    def test_worker_no_match_returns_silently(self):
        # 区间不含 target_i (~48k), worker 扫完, queue 应保持空
        q = multiprocessing.Queue()
        fkm._bruteforce_worker_chunk(
            0, 1000,
            self.xor_key, self.suffix_bytes,
            self.wxid_norm.encode("ascii"),
            [self.template], q,
        )
        with self.assertRaises(_queue_mod.Empty):
            q.get(timeout=0.5)

    def test_worker_skips_when_aes_fails(self):
        # md5 prefix 命中但 AES 模板错: 不入队 (防止 md5 单 gate 假阳)
        q = multiprocessing.Queue()
        wrong_template = b"\x00" * 16  # AES 解出来非图像 magic
        fkm._bruteforce_worker_chunk(
            self.target_i - 50, self.target_i + 50,
            self.xor_key, self.suffix_bytes,
            self.wxid_norm.encode("ascii"),
            [wrong_template], q,
        )
        with self.assertRaises(_queue_mod.Empty):
            q.get(timeout=0.5)

    def test_parallel_workers_1_finds_synthetic_uin(self):
        # 集成: workers=1 验证 process spawn + pickle + queue 跨进程通信
        result = fkm._bruteforce_with_aes_parallel(
            self.xor_key, self.suffix_hex, self.wxid_norm,
            [self.template], workers=1, timeout=30,
        )
        self.assertEqual(result, (self.uin, self.aes_hex))


class SaveConfigAtomicTests(unittest.TestCase):
    """原子写测试：os.replace 保证 config.json 不会被半截覆盖。"""

    def test_roundtrip_writes_pretty_utf8(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            cfg = {"db_dir": "/x", "image_aes_key": "中文测试key"}
            fkm._save_config_atomic(cfg_path, cfg)
            with open(cfg_path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), cfg)
            # ensure_ascii=False：中文应直接落盘，不被转义
            with open(cfg_path, "rb") as f:
                self.assertIn("中文测试key".encode("utf-8"), f.read())

    def test_failed_replace_leaves_original_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({"original": True}, f)
            with patch.object(os, "replace",
                              side_effect=OSError("disk full during rename")):
                with self.assertRaises(OSError):
                    fkm._save_config_atomic(cfg_path, {"new": True})
            # 原文件应保持不变
            with open(cfg_path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"original": True})


if __name__ == "__main__":
    unittest.main()
