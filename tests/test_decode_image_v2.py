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


def _make_resource_db(path, local_id, file_md5, username="wxid_test123",
                       chat_id=1, message_create_time=1700000000,
                       message_local_type=3, extra_rows=()):
    """构造最小 message_resource.db, 表 schema 对齐真实微信结构。

    真实表里 message_local_id 不全局唯一 (跨 chat 重复, 活跃 chat 内也会复用),
    解析必须用 ChatName2Id.rowid -> chat_id 限定 + message_local_type=3 过滤图片。

    packed_info 里嵌入 extract_md5_from_packed_info 期望的 protobuf marker
    (\\x12\\x22\\x0a\\x20) 加 32 字节 ASCII hex MD5。

    Args:
        extra_rows: 额外 (chat_id, message_local_id, message_local_type,
                    message_create_time, file_md5) 元组列表, 用于构造同 local_id
                    跨 chat / 同 chat 多版本的歧义场景。
    """
    marker = b'\x12\x22\x0a\x20'
    def _packed(md5_hex):
        return b'\x00' * 8 + marker + md5_hex.encode('ascii') + b'\x00' * 4

    conn = sqlite3.connect(path)
    try:
        conn.execute("""
            CREATE TABLE MessageResourceInfo (
                message_id INTEGER PRIMARY KEY,
                chat_id INTEGER,
                sender_id INTEGER,
                message_local_type INTEGER,
                message_create_time INTEGER,
                message_local_id INTEGER,
                message_svr_id INTEGER,
                message_origin_source INTEGER,
                packed_info BLOB
            )
        """)
        conn.execute(
            "CREATE TABLE ChatName2Id (user_name TEXT PRIMARY KEY, update_time INTEGER)"
        )
        conn.execute(
            "INSERT INTO ChatName2Id (rowid, user_name, update_time) VALUES (?, ?, ?)",
            (chat_id, username, message_create_time),
        )
        next_msg_id = 1
        conn.execute(
            "INSERT INTO MessageResourceInfo "
            "(message_id, chat_id, sender_id, message_local_type, message_create_time, "
            " message_local_id, message_svr_id, message_origin_source, packed_info) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (next_msg_id, chat_id, 0, message_local_type, message_create_time,
             local_id, 0, 0, _packed(file_md5)),
        )
        next_msg_id += 1
        for extra in extra_rows:
            ex_chat_id, ex_local_id, ex_type, ex_ctime, ex_md5 = extra
            conn.execute(
                "INSERT INTO MessageResourceInfo "
                "(message_id, chat_id, sender_id, message_local_type, message_create_time, "
                " message_local_id, message_svr_id, message_origin_source, packed_info) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (next_msg_id, ex_chat_id, 0, ex_type, ex_ctime, ex_local_id, 0, 0, _packed(ex_md5)),
            )
            next_msg_id += 1
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

    def test_decode_image_disambiguates_local_id_across_chats(self):
        """同 local_id 跨 chat 重复时, 必须按 username -> chat_id 选对; 否则会拿到
        别的 chat 的 MD5 (或视频 type=43 的 packed_info), 解出错图。

        生产 DB 上同一个 message_local_id 实测会出现在 5+ 个不同 chat 里,
        其中混有图片 (type=3) / 视频 (type=43) / 群聊 / 私聊, 必须 chat-scoped
        + type 过滤才能定位。
        """
        os.unlink(self.db_path)
        other_md5 = "f" * 32
        video_md5 = "a" * 32
        _make_resource_db(
            self.db_path, self.local_id, self.file_md5,
            username=self.username, chat_id=7,
            message_create_time=1778487726,
            extra_rows=[
                # 另一个 chat 同 local_id 同图片类型, MD5 不同 —— 选错就拿这个
                (5, self.local_id, 3, 1700000000, other_md5),
                # 又一个 chat 同 local_id 但是视频 (type=43), 应被 type 过滤
                (132, self.local_id, 43, 1750000000, video_md5),
            ],
        )
        # 给冲突 chat 也注册 user_name, 否则 chat-scope 等价
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO ChatName2Id (rowid, user_name, update_time) VALUES (5, ?, 0), (132, ?, 0)",
            ("other_chat_wxid", "video_chat_wxid"),
        )
        conn.commit()
        conn.close()

        resolver = ImageResolver(
            self.wechat_base, self.out_dir, self.cache,
            aes_key=TEST_AES_KEY, xor_key=TEST_XOR_KEY,
        )
        result = resolver.decode_image(self.username, self.local_id)
        self.assertTrue(result['success'], msg=result)
        # 必须拿目标 chat 的 MD5, 不是 other_chat 也不是视频
        self.assertEqual(result['md5'], self.file_md5)

    def test_decode_image_picks_latest_when_same_chat_local_id_reused(self):
        """活跃 chat 里 local_id 会被复用 (实测同 chat 同 local_id 最多 7 条);
        默认应返回 message_create_time 最新的那张, 对应用户最近一次 reference。
        """
        os.unlink(self.db_path)
        old_md5 = "c" * 32
        # self.file_md5 / self.local_id 在 _make_resource_db 默认插入为 "latest" 那条
        _make_resource_db(
            self.db_path, self.local_id, self.file_md5,
            username=self.username, chat_id=1,
            message_create_time=1778487726,
            extra_rows=[
                # 同 chat 同 local_id 但更早, 不应该被选中
                (1, self.local_id, 3, 1700000000, old_md5),
            ],
        )

        resolver = ImageResolver(
            self.wechat_base, self.out_dir, self.cache,
            aes_key=TEST_AES_KEY, xor_key=TEST_XOR_KEY,
        )
        result = resolver.decode_image(self.username, self.local_id)
        self.assertTrue(result['success'], msg=result)
        self.assertEqual(result['md5'], self.file_md5)

    def test_decode_image_unknown_chat_returns_error(self):
        """username 在 ChatName2Id 里找不到时, 应返回结构化错误而不是 crash 或乱选 row。"""
        resolver = ImageResolver(
            self.wechat_base, self.out_dir, self.cache,
            aes_key=TEST_AES_KEY, xor_key=TEST_XOR_KEY,
        )
        result = resolver.decode_image("wxid_does_not_exist", self.local_id)
        self.assertFalse(result['success'])
        self.assertIn('wxid_does_not_exist', result['error'])

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

        resolver = ImageResolver(self.wechat_base, self.out_dir, self.cache, aes_key=None)
        result = resolver.decode_image(self.username, self.local_id)
        self.assertTrue(result['success'], msg=result)
        self.assertEqual(result['format'], 'png')


if __name__ == '__main__':
    unittest.main()
