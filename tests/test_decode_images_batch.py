"""decode_image.decode_all_dats() batch CLI 行为测试。

覆盖:
- 路径扫描:glob 命中 attach/<chat_hash>/<YYYY-MM>/Img/*.dat
- 路径解析:chat_hash / YYYY-MM 提取,_t / _h 后缀移除归并到原图 basename
- 幂等性:目标 basename 已存在(任何扩展名)时跳过;--force 强制重解
- 原子写:写到 tmp 再 os.replace;失败/异常路径不留 .tmp
- V2 无 key:计入 skipped_no_key 而非 failed
- 错误隔离:单文件异常不阻塞批次;返回失败计数

decrypt_dat_file 用 mock 隔离(避免依赖真实加密图片);is_v2_format
单独覆盖真实 magic 检测路径。
"""
import os
import struct
import tempfile
import unittest
from contextlib import redirect_stderr
import io
from unittest.mock import patch

import decode_image


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _v2_magic_bytes():
    # 仅用于让 is_v2_format() 返回 True
    return decode_image.V2_MAGIC_FULL + struct.pack("<LL", 0, 0) + b"\x00"


def _v1_magic_bytes():
    return decode_image.V1_MAGIC_FULL + struct.pack("<LL", 0, 0) + b"\x00"


class _MockedDecrypt:
    """mock decrypt_dat_file:不真解密,只往 tmp 写一个 marker 字节串然后返回 (tmp, ext)。

    通过实例化时配置返回的 ext / 是否抛异常 / 是否返回 (None, None),覆盖
    各种成功/失败路径。
    """
    def __init__(self, ext="jpg", marker=b"DECODED", returns_none=False, raises=None):
        self.ext = ext
        self.marker = marker
        self.returns_none = returns_none
        self.raises = raises
        self.calls = []

    def __call__(self, dat_path, out_path=None, aes_key=None, xor_key=0x88):
        self.calls.append((dat_path, out_path, aes_key, xor_key))
        if self.raises:
            raise self.raises
        if self.returns_none:
            return None, None
        # 写 tmp(decode_all_dats 期望我们写完才能 os.replace)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(self.marker)
        return out_path, self.ext


def _make_dat(attach_dir, chat_hash, ym, basename, content=None):
    """在 attach_dir 下造一个 .dat 文件,返回完整路径。content 默认是非 V2 占位。"""
    if content is None:
        content = b"\x00\x00\x00\x00"  # 非 V2 / 非 V1 magic
    p = os.path.join(attach_dir, chat_hash, ym, "Img", f"{basename}.dat")
    _write(p, content)
    return p


class PathParsingTests(unittest.TestCase):
    """路径扫描 / 解析 / _t _h 归并。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.attach = os.path.join(self._tmp.name, "attach")
        self.out = os.path.join(self._tmp.name, "out")

    def test_finds_dat_files_under_chat_month_img(self):
        _make_dat(self.attach, "hash1", "2026-01", "abc123")
        _make_dat(self.attach, "hash2", "2026-02", "def456")
        mock = _MockedDecrypt()
        with patch.object(decode_image, "decrypt_dat_file", mock), \
             redirect_stderr(io.StringIO()):
            stats = decode_image.decode_all_dats(
                self.attach, self.out, aes_key="x" * 16, progress_every=None,
            )
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["decoded"], 2)
        self.assertEqual(stats["failed"], 0)

    def test_strips_t_suffix(self):
        _make_dat(self.attach, "hash1", "2026-01", "abc123_t")
        mock = _MockedDecrypt(ext="png")
        with patch.object(decode_image, "decrypt_dat_file", mock), \
             redirect_stderr(io.StringIO()):
            decode_image.decode_all_dats(
                self.attach, self.out, aes_key="x" * 16, progress_every=None,
            )
        # 期望产出: out/hash1/2026-01/abc123.png(_t 已被剥)
        produced = os.path.join(self.out, "hash1", "2026-01", "abc123.png")
        self.assertTrue(os.path.exists(produced), f"missing: {produced}")

    def test_strips_h_suffix(self):
        _make_dat(self.attach, "hash1", "2026-01", "abc_h")
        mock = _MockedDecrypt(ext="jpg")
        with patch.object(decode_image, "decrypt_dat_file", mock), \
             redirect_stderr(io.StringIO()):
            decode_image.decode_all_dats(
                self.attach, self.out, aes_key="x" * 16, progress_every=None,
            )
        produced = os.path.join(self.out, "hash1", "2026-01", "abc.jpg")
        self.assertTrue(os.path.exists(produced))

    def test_mirrors_chat_and_month(self):
        _make_dat(self.attach, "abcdef0123456789", "2026-04", "img1")
        mock = _MockedDecrypt(ext="jpg")
        with patch.object(decode_image, "decrypt_dat_file", mock), \
             redirect_stderr(io.StringIO()):
            decode_image.decode_all_dats(
                self.attach, self.out, aes_key="x" * 16, progress_every=None,
            )
        produced = os.path.join(self.out, "abcdef0123456789", "2026-04", "img1.jpg")
        self.assertTrue(os.path.exists(produced))


class IdempotentTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.attach = os.path.join(self._tmp.name, "attach")
        self.out = os.path.join(self._tmp.name, "out")

    def test_existing_target_basename_skipped(self):
        _make_dat(self.attach, "hash1", "2026-01", "img1")
        # 预先放一个目标(任何扩展名)
        existing = os.path.join(self.out, "hash1", "2026-01", "img1.png")
        _write(existing, b"OLD")
        mock = _MockedDecrypt()
        with patch.object(decode_image, "decrypt_dat_file", mock), \
             redirect_stderr(io.StringIO()):
            stats = decode_image.decode_all_dats(
                self.attach, self.out, aes_key="x" * 16, progress_every=None,
            )
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["decoded"], 0)
        self.assertEqual(len(mock.calls), 0, "decrypt_dat_file 不该被调用")
        # 目标内容未被改写
        with open(existing, "rb") as f:
            self.assertEqual(f.read(), b"OLD")

    def test_force_overrides_skip(self):
        _make_dat(self.attach, "hash1", "2026-01", "img1")
        existing = os.path.join(self.out, "hash1", "2026-01", "img1.png")
        _write(existing, b"OLD")
        mock = _MockedDecrypt(ext="jpg", marker=b"NEW")
        with patch.object(decode_image, "decrypt_dat_file", mock), \
             redirect_stderr(io.StringIO()):
            stats = decode_image.decode_all_dats(
                self.attach, self.out, aes_key="x" * 16,
                force=True, progress_every=None,
            )
        self.assertEqual(stats["decoded"], 1)
        self.assertEqual(stats["skipped"], 0)
        # 新文件以新 ext 落盘
        new_file = os.path.join(self.out, "hash1", "2026-01", "img1.jpg")
        self.assertTrue(os.path.exists(new_file))

    def test_skip_ignores_tmp_files(self):
        """残留的 .tmp 不应该被当成"已存在目标"误判跳过。"""
        _make_dat(self.attach, "hash1", "2026-01", "img1")
        # 模拟之前一次中断留下的 .tmp
        leftover_tmp = os.path.join(self.out, "hash1", "2026-01", "img1.unknown.tmp")
        _write(leftover_tmp, b"PARTIAL")
        mock = _MockedDecrypt(ext="jpg")
        with patch.object(decode_image, "decrypt_dat_file", mock), \
             redirect_stderr(io.StringIO()):
            stats = decode_image.decode_all_dats(
                self.attach, self.out, aes_key="x" * 16, progress_every=None,
            )
        self.assertEqual(stats["decoded"], 1, "残留 .tmp 不应该阻止重解")


class AtomicWriteTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.attach = os.path.join(self._tmp.name, "attach")
        self.out = os.path.join(self._tmp.name, "out")
        _make_dat(self.attach, "hash1", "2026-01", "img1")

    def test_success_path_no_tmp_leftover(self):
        mock = _MockedDecrypt(ext="jpg")
        with patch.object(decode_image, "decrypt_dat_file", mock), \
             redirect_stderr(io.StringIO()):
            decode_image.decode_all_dats(
                self.attach, self.out, aes_key="x" * 16, progress_every=None,
            )
        target_dir = os.path.join(self.out, "hash1", "2026-01")
        leftovers = [f for f in os.listdir(target_dir) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [], "成功路径不应有 .tmp 残留")

    def test_decrypt_returns_none_no_tmp_leftover(self):
        mock = _MockedDecrypt(returns_none=True)
        with patch.object(decode_image, "decrypt_dat_file", mock), \
             redirect_stderr(io.StringIO()):
            stats = decode_image.decode_all_dats(
                self.attach, self.out, aes_key="x" * 16, progress_every=None,
            )
        self.assertEqual(stats["failed"], 1)
        # decrypt 没写 tmp(returns_none=True 时也不写),所以目录可能不存在或为空
        target_dir = os.path.join(self.out, "hash1", "2026-01")
        if os.path.isdir(target_dir):
            leftovers = [f for f in os.listdir(target_dir) if f.endswith(".tmp")]
            self.assertEqual(leftovers, [])

    def test_decrypt_raises_no_tmp_leftover(self):
        mock = _MockedDecrypt(raises=RuntimeError("synthetic decrypt failure"))
        with patch.object(decode_image, "decrypt_dat_file", mock), \
             redirect_stderr(io.StringIO()):
            stats = decode_image.decode_all_dats(
                self.attach, self.out, aes_key="x" * 16, progress_every=None,
            )
        self.assertEqual(stats["failed"], 1)
        target_dir = os.path.join(self.out, "hash1", "2026-01")
        if os.path.isdir(target_dir):
            leftovers = [f for f in os.listdir(target_dir) if f.endswith(".tmp")]
            self.assertEqual(leftovers, [], "异常路径必须清理 .tmp")


class V2NoKeyTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.attach = os.path.join(self._tmp.name, "attach")
        self.out = os.path.join(self._tmp.name, "out")

    def test_v2_dat_with_no_aes_key_skipped(self):
        _make_dat(self.attach, "hash1", "2026-01", "v2img", content=_v2_magic_bytes())
        mock = _MockedDecrypt()
        with patch.object(decode_image, "decrypt_dat_file", mock), \
             redirect_stderr(io.StringIO()):
            stats = decode_image.decode_all_dats(
                self.attach, self.out, aes_key=None, progress_every=None,
            )
        self.assertEqual(stats["skipped_no_key"], 1)
        self.assertEqual(stats["decoded"], 0)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(len(mock.calls), 0, "无 key 的 V2 文件不应该走 decrypt_dat_file")

    def test_v1_dat_with_no_aes_key_still_decoded(self):
        """V1 用固定 AES key,不需要 image_aes_key,仍应被处理。"""
        _make_dat(self.attach, "hash1", "2026-01", "v1img", content=_v1_magic_bytes())
        mock = _MockedDecrypt(ext="jpg")
        with patch.object(decode_image, "decrypt_dat_file", mock), \
             redirect_stderr(io.StringIO()):
            stats = decode_image.decode_all_dats(
                self.attach, self.out, aes_key=None, progress_every=None,
            )
        # is_v2_format 只识别 V2(纯 V2 magic),V1 不算 V2,所以会进入 decrypt 流程
        self.assertEqual(stats["decoded"], 1)
        self.assertEqual(stats["skipped_no_key"], 0)


class CallbackTests(unittest.TestCase):

    def test_on_file_callback_fires_per_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            attach = os.path.join(tmp, "attach")
            out = os.path.join(tmp, "out")
            _make_dat(attach, "h1", "2026-01", "a")
            _make_dat(attach, "h1", "2026-01", "b")
            _make_dat(attach, "h1", "2026-01", "c")
            events = []
            mock = _MockedDecrypt()
            with patch.object(decode_image, "decrypt_dat_file", mock), \
                 redirect_stderr(io.StringIO()):
                decode_image.decode_all_dats(
                    attach, out, aes_key="x" * 16, progress_every=None,
                    on_file=lambda i, total, p, status, fmt: events.append(status),
                )
            self.assertEqual(len(events), 3)
            self.assertTrue(all(s == "decoded" for s in events))


if __name__ == "__main__":
    unittest.main()
