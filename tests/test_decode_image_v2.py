"""ImageResolver 在 V2 加密格式下的端到端解密测试。

覆盖:
- v2_decrypt_file 能正确还原 AES-ECB + XOR 混合加密的合成数据
- decrypt_dat_file 按 magic 自动分发 V2 / V1 / 老 XOR 三条路径
- ImageResolver 通过 __init__ 注入 aes_key/xor_key 后,能端到端解密 V2 .dat
- 没传 aes_key 时遇到 V2 文件返回结构化错误,而不是 crash 或返回错误数据
- 默认参数下老 XOR 路径不受影响,保持向后兼容
"""
import hashlib
import os
import sqlite3
import struct
import tempfile
import unittest

from Crypto.Cipher import AES
from Crypto.Util import Padding

from decode_image import (
    V1_MAGIC_FULL,
    V2_MAGIC_FULL,
    ImageResolver,
    decrypt_dat_file,
    v2_decrypt_file,
)


# 测试用 16 字节 AES key (任意值,仅用于合成测试数据)
TEST_AES_KEY = b'1234567890abcdef'
TEST_XOR_KEY = 0x37
# 最小可识别的 PNG payload (含 IHDR 和 IEND chunk),长度 88 字节
TEST_PNG_PAYLOAD = (
    b'\x89PNG\r\n\x1a\n'
    + b'\x00\x00\x00\rIHDR'
    + b'\x00' * 64
    + b'IEND\xaeB`\x82'
)


def _build_v2_dat(plaintext, aes_size, xor_size,
                  aes_key=TEST_AES_KEY, xor_key=TEST_XOR_KEY,
                  magic=V2_MAGIC_FULL):
    """构造合成的 V2 / V1 .dat 字节串。

    布局: [6B magic][4B aes_size LE][4B xor_size LE][1B pad][AES-ECB][raw][XOR]
    aes_size / xor_size 是明文字段长度,AES 段做 PKCS7 padding 后向上对齐到 16 倍数。
    """
    if aes_size + xor_size > len(plaintext):
        raise ValueError("aes_size + xor_size 超过 plaintext 长度")
    aes_plain = plaintext[:aes_size]
    raw_plain = plaintext[aes_size:len(plaintext) - xor_size]
    xor_plain = plaintext[len(plaintext) - xor_size:]

    cipher = AES.new(aes_key[:16], AES.MODE_ECB)
    aes_cipher = cipher.encrypt(Padding.pad(aes_plain, AES.block_size))
    xor_cipher = bytes(b ^ xor_key for b in xor_plain)

    header = magic + struct.pack('<LL', aes_size, xor_size) + b'\x00'
    return header + aes_cipher + raw_plain + xor_cipher


class _FakeCache:
    """ImageResolver 测试用最小缓存桩,绕过真实 DB 解密。"""

    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, rel_key):
        return self._mapping.get(rel_key)


def _make_resource_db(path, local_id, file_md5):
    """构造最小 message_resource.db,只含一条 packed_info 记录。

    packed_info 里嵌入 extract_md5_from_packed_info 期望的 protobuf marker
    (\\x12\\x22\\x0a\\x20) 加 32 字节 ASCII hex MD5。
    """
    marker = b'\x12\x22\x0a\x20'
    packed = b'\x00' * 8 + marker + file_md5.encode('ascii') + b'\x00' * 4
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE MessageResourceInfo (local_id INTEGER PRIMARY KEY, packed_info BLOB)"
        )
        conn.execute(
            "INSERT INTO MessageResourceInfo VALUES (?, ?)",
            (local_id, packed),
        )
        conn.commit()
    finally:
        conn.close()


class TestV2DecryptSynthetic(unittest.TestCase):
    """v2_decrypt_file / decrypt_dat_file 在合成数据上的正确性"""

    def test_v2_round_trip_recovers_payload(self):
        # 合成 V2 .dat 解密后字节级等于原始 payload
        with tempfile.TemporaryDirectory() as td:
            dat_path = os.path.join(td, "test.dat")
            with open(dat_path, 'wb') as f:
                f.write(_build_v2_dat(TEST_PNG_PAYLOAD, aes_size=32, xor_size=16))

            out_path, fmt = v2_decrypt_file(
                dat_path, aes_key=TEST_AES_KEY, xor_key=TEST_XOR_KEY
            )
            self.assertIsNotNone(out_path)
            self.assertEqual(fmt, 'png')
            with open(out_path, 'rb') as f:
                self.assertEqual(f.read(), TEST_PNG_PAYLOAD)

    def test_decrypt_dat_file_routes_v2_by_magic(self):
        # decrypt_dat_file 看到 V2 magic 应自动走 V2 路径
        with tempfile.TemporaryDirectory() as td:
            dat_path = os.path.join(td, "test.dat")
            with open(dat_path, 'wb') as f:
                f.write(_build_v2_dat(TEST_PNG_PAYLOAD, aes_size=32, xor_size=16))

            out_path, fmt = decrypt_dat_file(
                dat_path, aes_key=TEST_AES_KEY, xor_key=TEST_XOR_KEY
            )
            self.assertIsNotNone(out_path)
            self.assertEqual(fmt, 'png')

    def test_decrypt_dat_file_v1_uses_fixed_key(self):
        # V1 magic 走固定 key,即便外部不传 aes_key 也能解密
        v1_fixed_key = b'cfcd208495d565ef'  # md5("0")[:16],由 v2_decrypt_file 内部使用
        with tempfile.TemporaryDirectory() as td:
            dat_path = os.path.join(td, "test.dat")
            with open(dat_path, 'wb') as f:
                f.write(_build_v2_dat(
                    TEST_PNG_PAYLOAD, aes_size=32, xor_size=16,
                    aes_key=v1_fixed_key, magic=V1_MAGIC_FULL,
                ))

            out_path, fmt = decrypt_dat_file(
                dat_path, aes_key=None, xor_key=TEST_XOR_KEY
            )
            self.assertIsNotNone(out_path)
            self.assertEqual(fmt, 'png')

    def test_decrypt_dat_file_legacy_xor_route(self):
        # 老 XOR 格式 (无 V1/V2 magic),decrypt_dat_file 应回退到 xor_decrypt_file 不需要 aes_key
        xor_key = 0x37
        with tempfile.TemporaryDirectory() as td:
            dat_path = os.path.join(td, "test.dat")
            with open(dat_path, 'wb') as f:
                f.write(bytes(b ^ xor_key for b in TEST_PNG_PAYLOAD))

            out_path, fmt = decrypt_dat_file(dat_path, aes_key=None)
            self.assertIsNotNone(out_path)
            self.assertEqual(fmt, 'png')

    def test_v2_accepts_str_aes_key_from_config(self):
        # 真实场景下 aes_key 来自 config.json,是 ASCII string 不是 bytes;
        # v2_decrypt_file 内部应自行 encode,避免 TypeError
        with tempfile.TemporaryDirectory() as td:
            dat_path = os.path.join(td, "test.dat")
            with open(dat_path, 'wb') as f:
                f.write(_build_v2_dat(TEST_PNG_PAYLOAD, aes_size=32, xor_size=16))

            out_path, fmt = decrypt_dat_file(
                dat_path, aes_key=TEST_AES_KEY.decode('ascii'), xor_key=TEST_XOR_KEY,
            )
            self.assertIsNotNone(out_path)
            self.assertEqual(fmt, 'png')

    def test_v2_accepts_str_xor_key_from_config(self):
        # 与 aes_key 的 str 处理对称: config.json 里把 xor_key 写成 "0x88" / "136" 也应能正常解密
        with tempfile.TemporaryDirectory() as td:
            dat_path = os.path.join(td, "test.dat")
            with open(dat_path, 'wb') as f:
                f.write(_build_v2_dat(TEST_PNG_PAYLOAD, aes_size=32, xor_size=16))

            out_path, fmt = decrypt_dat_file(
                dat_path, aes_key=TEST_AES_KEY, xor_key=hex(TEST_XOR_KEY),
            )
            self.assertIsNotNone(out_path)
            self.assertEqual(fmt, 'png')

    def test_v2_wxgf_payload_returns_hevc_format(self):
        # 微信 V2 动图 (wxgf 裸流 HEVC) 解密后 fmt='hevc',输出文件以 .hevc 结尾;
        # 当前 ImageResolver 不再向 JPEG 转 (那是 monitor_web 的职责),保持原样输出。
        wxgf_payload = b'wxgf' + b'\x00' * 84  # 88 字节,与 PNG payload 同长度,避免改 aes/xor sizes
        with tempfile.TemporaryDirectory() as td:
            dat_path = os.path.join(td, "test.dat")
            with open(dat_path, 'wb') as f:
                f.write(_build_v2_dat(wxgf_payload, aes_size=32, xor_size=16))

            out_path, fmt = decrypt_dat_file(
                dat_path, aes_key=TEST_AES_KEY, xor_key=TEST_XOR_KEY,
            )
            self.assertIsNotNone(out_path)
            self.assertEqual(fmt, 'hevc')
            self.assertTrue(out_path.endswith('.hevc'))

    def test_v2_rejects_wrong_aes_key(self):
        # AES key 错时 detect_image_format 返回 'bin' (magic 不识别),v2_decrypt_file
        # 应拒绝写出 .bin 垃圾文件并返回 (None, None),让 caller 知道解密失败。
        with tempfile.TemporaryDirectory() as td:
            dat_path = os.path.join(td, "test.dat")
            with open(dat_path, 'wb') as f:
                f.write(_build_v2_dat(TEST_PNG_PAYLOAD, aes_size=32, xor_size=16))

            wrong_aes_key = b'wrongkey00000000'
            out_path, fmt = v2_decrypt_file(
                dat_path, aes_key=wrong_aes_key, xor_key=TEST_XOR_KEY,
            )
            self.assertIsNone(out_path)
            self.assertIsNone(fmt)

    def test_v2_rejects_wrong_xor_key_jpg_trailer(self):
        # JPG 必须以 FF D9 (EOI) 收尾。XOR key 错时尾部 16 字节乱码,
        # FF D9 被破坏,触发尾部 magic 校验失败。
        jpg_payload = b'\xff\xd8\xff' + b'\x00' * 83 + b'\xff\xd9'  # 88 bytes, FF D9 在末尾
        with tempfile.TemporaryDirectory() as td:
            dat_path = os.path.join(td, "test.dat")
            with open(dat_path, 'wb') as f:
                f.write(_build_v2_dat(jpg_payload, aes_size=32, xor_size=16))

            # 翻转所有 XOR 字节: TEST_XOR_KEY ^ 0xff 保证每字节都错位
            out_path, fmt = v2_decrypt_file(
                dat_path, aes_key=TEST_AES_KEY, xor_key=TEST_XOR_KEY ^ 0xff,
            )
            self.assertIsNone(out_path)
            self.assertIsNone(fmt)

    def test_v2_rejects_wrong_xor_key_png_iend(self):
        # PNG 末尾 12 字节必须含 IEND chunk。XOR key 错时 IEND 被破坏。
        with tempfile.TemporaryDirectory() as td:
            dat_path = os.path.join(td, "test.dat")
            with open(dat_path, 'wb') as f:
                f.write(_build_v2_dat(TEST_PNG_PAYLOAD, aes_size=32, xor_size=16))

            out_path, fmt = v2_decrypt_file(
                dat_path, aes_key=TEST_AES_KEY, xor_key=TEST_XOR_KEY ^ 0xff,
            )
            self.assertIsNone(out_path)
            self.assertIsNone(fmt)

    def test_v2_skip_xor_validation_when_xor_size_zero(self):
        # xor_size < 2 时没有 XOR 段(或样本不足以验证),不应触发尾部 magic 校验。
        # 构造 xor_size=0 的 PNG (整张图都在 AES + raw 段),xor_key 实际不参与解密。
        with tempfile.TemporaryDirectory() as td:
            dat_path = os.path.join(td, "test.dat")
            with open(dat_path, 'wb') as f:
                f.write(_build_v2_dat(TEST_PNG_PAYLOAD, aes_size=32, xor_size=0))

            # xor_key 传 0 也应成功 (XOR 段长度 0)
            out_path, fmt = v2_decrypt_file(
                dat_path, aes_key=TEST_AES_KEY, xor_key=0x00,
            )
            self.assertIsNotNone(out_path)
            self.assertEqual(fmt, 'png')

    def test_v2_wxgf_skips_trailer_validation(self):
        # wxgf (HEVC 裸流) 没有强制 trailer signature,XOR key 错时也不应被尾部校验误杀
        # (wxgf 路径在 elif 链前面命中,直接 fmt='hevc',不进入 XOR 校验分支)。
        # 这里验证:即便 XOR key 错导致末尾字节乱码,只要 wxgf magic 在头部正确,
        # 仍按 hevc 输出 — 因为我们只校验 jpg/png,其他格式跳过。
        wxgf_payload = b'wxgf' + b'\x00' * 84
        with tempfile.TemporaryDirectory() as td:
            dat_path = os.path.join(td, "test.dat")
            with open(dat_path, 'wb') as f:
                f.write(_build_v2_dat(wxgf_payload, aes_size=32, xor_size=16))

            out_path, fmt = v2_decrypt_file(
                dat_path, aes_key=TEST_AES_KEY, xor_key=TEST_XOR_KEY ^ 0xff,
            )
            self.assertIsNotNone(out_path)
            self.assertEqual(fmt, 'hevc')


class TestImageResolverV2(unittest.TestCase):
    """ImageResolver 端到端:从 local_id 到解密文件,验证 V2 keys 注入路径"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = self._tmp.name

        self.wechat_base = os.path.join(tmp, "wechat")
        self.out_dir = os.path.join(tmp, "decoded")
        os.makedirs(self.out_dir, exist_ok=True)

        self.username = "wxid_test123"
        self.local_id = 42
        self.file_md5 = "0123456789abcdef0123456789abcdef"

        username_hash = hashlib.md5(self.username.encode()).hexdigest()
        img_dir = os.path.join(
            self.wechat_base, "msg", "attach", username_hash, "2025-08", "Img"
        )
        os.makedirs(img_dir, exist_ok=True)
        self.dat_path = os.path.join(img_dir, f"{self.file_md5}.dat")
        with open(self.dat_path, 'wb') as f:
            f.write(_build_v2_dat(TEST_PNG_PAYLOAD, aes_size=32, xor_size=16))

        self.db_path = os.path.join(tmp, "message_resource.db")
        _make_resource_db(self.db_path, self.local_id, self.file_md5)
        self.cache = _FakeCache({"message/message_resource.db": self.db_path})

    def test_decode_image_v2_with_keys(self):
        resolver = ImageResolver(
            self.wechat_base, self.out_dir, self.cache,
            aes_key=TEST_AES_KEY, xor_key=TEST_XOR_KEY,
        )
        result = resolver.decode_image(self.username, self.local_id)
        self.assertTrue(result['success'], msg=result)
        self.assertEqual(result['format'], 'png')
        self.assertEqual(result['md5'], self.file_md5)
        with open(result['path'], 'rb') as f:
            self.assertEqual(f.read(), TEST_PNG_PAYLOAD)

    def test_decode_image_v2_missing_aes_key_returns_error(self):
        # 没传 aes_key 时遇到 V2 文件应返回 success=False,而不是 crash 或写入错误文件
        resolver = ImageResolver(
            self.wechat_base, self.out_dir, self.cache, aes_key=None,
        )
        result = resolver.decode_image(self.username, self.local_id)
        self.assertFalse(result['success'])
        self.assertIn('AES key', result['error'])
        self.assertEqual(result['md5'], self.file_md5)

    def test_decode_image_default_args_preserve_legacy_xor(self):
        # 默认参数 (aes_key=None) + 老 XOR .dat 应保持向后兼容
        os.unlink(self.dat_path)
        legacy_xor_key = 0x37
        with open(self.dat_path, 'wb') as f:
            f.write(bytes(b ^ legacy_xor_key for b in TEST_PNG_PAYLOAD))

        resolver = ImageResolver(self.wechat_base, self.out_dir, self.cache)
        result = resolver.decode_image(self.username, self.local_id)
        self.assertTrue(result['success'], msg=result)
        self.assertEqual(result['format'], 'png')

    def test_decode_image_v1_no_aes_key_uses_fixed_key(self):
        # V1 magic 不会被 is_v2_format guard 拦截 (V1 magic 是 \x07\x08V1, V2 是 \x07\x08V2);
        # 即便 ImageResolver(aes_key=None), V1 文件也应通过 decrypt_dat_file 内置固定 key 解密
        os.unlink(self.dat_path)
        v1_fixed_key = b'cfcd208495d565ef'
        with open(self.dat_path, 'wb') as f:
            f.write(_build_v2_dat(
                TEST_PNG_PAYLOAD, aes_size=32, xor_size=16,
                aes_key=v1_fixed_key, magic=V1_MAGIC_FULL,
            ))

        # xor_key 必须跟 _build_v2_dat 加密时用的一致,否则 XOR 段乱码,
        # 触发新的尾部 magic 校验失败 (PNG IEND chunk 错位)。
        resolver = ImageResolver(
            self.wechat_base, self.out_dir, self.cache,
            aes_key=None, xor_key=TEST_XOR_KEY,
        )
        result = resolver.decode_image(self.username, self.local_id)
        self.assertTrue(result['success'], msg=result)
        self.assertEqual(result['format'], 'png')


if __name__ == '__main__':
    unittest.main()
