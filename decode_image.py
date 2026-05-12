r"""
微信图片 .dat 文件解密模块

支持两种加密格式:
  - 旧格式: 单字节 XOR 加密，key 通过对比文件头与已知图片 magic bytes 自动检测
  - V2 格式 (2025-08+): AES-128-ECB + XOR 混合加密，需要从微信进程内存提取 AES key

V2 文件结构:
  [6B signature: 07 08 V2 08 07] [4B aes_size LE] [4B xor_size LE] [1B padding]
  [aligned_aes_size bytes AES-ECB] [raw_data] [xor_size bytes XOR]

文件路径格式:
  D:\xwechat_files\<wxid>\msg\attach\<md5(username)>\<YYYY-MM>\Img\<file_md5>[_t|_h].dat

映射链:
  message_*.db (local_id) → message_resource.db (packed_info 含 MD5) → .dat 文件 → 解密
"""

import os
import sys
import glob
import hashlib
import sqlite3
import struct

# V2 格式完整 magic (6 bytes)
V2_MAGIC = b'\x07\x08\x56\x32'       # 前 4 字节用于快速检测
V2_MAGIC_FULL = b'\x07\x08V2\x08\x07' # 完整 6 字节签名
V1_MAGIC_FULL = b'\x07\x08V1\x08\x07' # V1 签名 (固定 key)

# 常见图片格式的 magic bytes (按长度降序排列，避免短 magic 假阳性)
IMAGE_MAGIC = {
    'png': [0x89, 0x50, 0x4E, 0x47],
    'gif': [0x47, 0x49, 0x46, 0x38],
    'tif': [0x49, 0x49, 0x2A, 0x00],   # little-endian TIFF
    'webp': [0x52, 0x49, 0x46, 0x46],  # RIFF header
    'jpg': [0xFF, 0xD8, 0xFF],
    # BMP 只有 2 字节 magic，容易假阳性，需要额外验证
}


def is_v2_format(dat_path):
    """检测是否是微信 V2 加密格式 (2025-08+)"""
    try:
        with open(dat_path, 'rb') as f:
            magic = f.read(4)
        return magic == V2_MAGIC
    except (OSError, IOError):
        return False


def detect_xor_key(dat_path):
    """通过对比文件头和已知图片 magic bytes 自动检测 XOR key

    返回 key (int) 或 None。V2 格式文件返回 None。
    """
    with open(dat_path, 'rb') as f:
        header = f.read(16)

    if len(header) < 4:
        return None

    # V2 新格式无法用 XOR 解密
    if header[:4] == V2_MAGIC:
        return None

    # 先尝试 3+ 字节 magic 的格式（可靠匹配）
    for fmt, magic in IMAGE_MAGIC.items():
        key = header[0] ^ magic[0]
        match = True
        for i in range(1, len(magic)):
            if i >= len(header):
                break
            if (header[i] ^ key) != magic[i]:
                match = False
                break
        if match:
            return key

    # 最后尝试 BMP (2 字节 magic，需要额外验证)
    bmp_magic = [0x42, 0x4D]
    key = header[0] ^ bmp_magic[0]
    if len(header) >= 2 and (header[1] ^ key) == bmp_magic[1]:
        # 额外验证: XOR 解密后检查 BMP file size 和 offset 字段
        if len(header) >= 14:
            dec = bytes(b ^ key for b in header[:14])
            bmp_size = struct.unpack_from('<I', dec, 2)[0]
            bmp_offset = struct.unpack_from('<I', dec, 10)[0]
            file_size = os.path.getsize(dat_path)
            # BMP file_size 字段应与实际文件大小接近，offset 应在合理范围
            if (abs(bmp_size - file_size) < 1024 and 14 <= bmp_offset <= 1078):
                return key

    return None


def detect_image_format(header_bytes):
    """根据解密后的文件头检测图片格式"""
    if header_bytes[:3] == bytes([0xFF, 0xD8, 0xFF]):
        return 'jpg'
    if header_bytes[:4] == bytes([0x89, 0x50, 0x4E, 0x47]):
        return 'png'
    if header_bytes[:3] == b'GIF':
        return 'gif'
    if header_bytes[:2] == b'BM':
        return 'bmp'
    if header_bytes[:4] == b'RIFF' and len(header_bytes) >= 12 and header_bytes[8:12] == b'WEBP':
        return 'webp'
    if header_bytes[:4] == bytes([0x49, 0x49, 0x2A, 0x00]):
        return 'tif'
    return 'bin'


def v2_decrypt_file(dat_path, out_path=None, aes_key=None, xor_key=0x88):
    """解密 V2 格式 .dat 文件 (AES-ECB + XOR)

    Args:
        dat_path: V2 .dat 文件路径
        out_path: 输出路径 (None 则自动命名)
        aes_key: 16 字节 AES key (bytes 或 str)
        xor_key: XOR key (int 或可被 int(_, 0) 解析的 str, 默认 0x88)

    Returns:
        (output_path, format) 或 (None, None)
    """
    if aes_key is None:
        return None, None

    from Crypto.Cipher import AES
    from Crypto.Util import Padding

    # 确保 key 是 16 字节 bytes
    if isinstance(aes_key, str):
        aes_key = aes_key.encode('ascii')[:16]
    if len(aes_key) < 16:
        return None, None

    # 与 aes_key 的 str→bytes 处理对称: 允许 config.json 写 "0x88" / "136" 等字符串形式
    if isinstance(xor_key, str):
        xor_key = int(xor_key, 0)

    with open(dat_path, 'rb') as f:
        data = f.read()

    if len(data) < 15:
        return None, None

    # 解析 header
    sig = data[:6]
    if sig not in (V2_MAGIC_FULL, V1_MAGIC_FULL):
        return None, None

    aes_size, xor_size = struct.unpack_from('<LL', data, 6)

    # V1 用固定 key
    if sig == V1_MAGIC_FULL:
        aes_key = b'cfcd208495d565ef'  # md5("0")[:16]

    # AES 对齐: PKCS7 填充使实际密文 >= aes_size，向上对齐到 16
    # 当 aes_size 是 16 的倍数时，还需要加 16 (完整填充块)
    aligned_aes_size = aes_size
    aligned_aes_size -= ~(~aligned_aes_size % 16)  # 同 wx-dat 的公式

    offset = 15
    if offset + aligned_aes_size > len(data):
        return None, None

    # AES-ECB 解密
    aes_data = data[offset:offset + aligned_aes_size]
    try:
        cipher = AES.new(aes_key[:16], AES.MODE_ECB)
        dec_aes = Padding.unpad(cipher.decrypt(aes_data), AES.block_size)
    except (ValueError, KeyError):
        return None, None
    offset += aligned_aes_size

    # Raw 部分 (不加密)
    raw_end = len(data) - xor_size
    raw_data = data[offset:raw_end] if offset < raw_end else b''
    offset = raw_end

    # XOR 部分
    xor_data = data[offset:]
    dec_xor = bytes(b ^ xor_key for b in xor_data)

    decrypted = dec_aes + raw_data + dec_xor
    fmt = detect_image_format(decrypted[:16])

    # wxgf (HEVC 裸流) 格式
    if decrypted[:4] == b'wxgf':
        fmt = 'hevc'
    elif fmt == 'bin':
        # detect_image_format 返回 'bin' = magic 不匹配任何已知图片格式,
        # 通常说明 AES key 错(解密后产生随机字节)。拒绝写出无意义的 .bin
        # 垃圾文件,让 caller 知道解密失败。
        return None, None
    elif xor_size >= 2:
        # XOR key 错时 AES/raw 段可能产生合法 magic(看似正常 jpg/png 头),
        # 但 XOR 段乱码。用尾部 magic 验证 XOR key 正确性:
        # - JPG 必须以 FF D9 (EOI marker) 收尾
        # - PNG 末尾 12 字节必须含 IEND chunk
        # 其他格式 (gif/bmp/tif/webp/hevc) 缺乏强制 trailer signature,
        # 不做校验以避免误杀。xor_size < 2 时无 XOR 段或样本过小,跳过。
        if fmt == 'jpg' and decrypted[-2:] != b'\xff\xd9':
            return None, None
        if fmt == 'png' and b'IEND' not in decrypted[-12:]:
            return None, None

    if out_path is None:
        base = os.path.splitext(dat_path)[0]
        for suffix in ('_t', '_h'):
            if base.endswith(suffix):
                base = base[:-len(suffix)]
                break
        out_path = f"{base}.{fmt}"

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'wb') as f:
        f.write(decrypted)

    return out_path, fmt


def xor_decrypt_file(dat_path, out_path=None, key=None):
    """解密单个 .dat 文件，返回 (output_path, format)"""
    if key is None:
        key = detect_xor_key(dat_path)
    if key is None:
        return None, None

    with open(dat_path, 'rb') as f:
        data = f.read()

    decrypted = bytes(b ^ key for b in data)
    fmt = detect_image_format(decrypted[:16])

    if out_path is None:
        base = os.path.splitext(dat_path)[0]
        # 去掉 _t, _h 后缀
        for suffix in ('_t', '_h'):
            if base.endswith(suffix):
                base = base[:-len(suffix)]
                break
        out_path = f"{base}.{fmt}"

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'wb') as f:
        f.write(decrypted)

    return out_path, fmt


def decrypt_dat_file(dat_path, out_path=None, aes_key=None, xor_key=0x88):
    """智能解密 .dat 文件 (自动检测格式)

    Args:
        dat_path: .dat 文件路径
        out_path: 输出路径
        aes_key: V2 格式的 AES key (str 或 bytes, 16 字节)
        xor_key: XOR key (int)

    Returns:
        (output_path, format) 或 (None, None)
    """
    with open(dat_path, 'rb') as f:
        head = f.read(6)

    # V2 新格式
    if head == V2_MAGIC_FULL:
        return v2_decrypt_file(dat_path, out_path, aes_key, xor_key)

    # V1 格式 (固定 AES key)
    if head == V1_MAGIC_FULL:
        return v2_decrypt_file(dat_path, out_path, b'cfcd208495d565ef', xor_key)

    # 旧 XOR 格式
    return xor_decrypt_file(dat_path, out_path)


def extract_md5_from_packed_info(blob):
    """从 message_resource.db 的 packed_info (protobuf) 中提取文件 MD5

    格式: ... \\x12\\x22\\x0a\\x20 + 32 字节 ASCII hex MD5 ...
    """
    if not blob or not isinstance(blob, bytes):
        return None

    # 查找 protobuf 标记
    marker = b'\x12\x22\x0a\x20'
    idx = blob.find(marker)
    if idx >= 0 and idx + len(marker) + 32 <= len(blob):
        md5_bytes = blob[idx + len(marker): idx + len(marker) + 32]
        try:
            md5_str = md5_bytes.decode('ascii')
            # 验证是合法的 hex 字符串
            int(md5_str, 16)
            return md5_str
        except (UnicodeDecodeError, ValueError):
            pass

    # 备用方案：扫描 32 字节连续 hex 字符
    hex_chars = set(b'0123456789abcdef')
    i = 0
    while i <= len(blob) - 32:
        if blob[i] in hex_chars:
            candidate = blob[i:i+32]
            if all(b in hex_chars for b in candidate):
                try:
                    return candidate.decode('ascii')
                except UnicodeDecodeError:
                    pass
            i += 32
        else:
            i += 1

    return None


class ImageResolver:
    """封装从 local_id 到图片文件的完整解析链"""

    def __init__(self, wechat_base_dir, decoded_image_dir, cache, aes_key=None, xor_key=0x88):
        """
        Args:
            wechat_base_dir: 微信数据根目录 (如 D:\\xwechat_files\\<wxid>)
            decoded_image_dir: 解密图片输出目录
            cache: DBCache 实例，用于解密 message_resource.db
            aes_key: V2 格式的 AES key (16 字节 str/bytes)，None 表示不支持 V2 文件
            xor_key: XOR key (int, 默认 0x88)，用于 V2 文件的 XOR 段
        """
        self.base_dir = wechat_base_dir
        self.attach_dir = os.path.join(wechat_base_dir, "msg", "attach")
        self.out_dir = decoded_image_dir
        self.cache = cache
        self.aes_key = aes_key
        self.xor_key = xor_key

    def get_image_md5(self, local_id):
        """通过 local_id 查 message_resource.db 获取图片文件 MD5"""
        path = self.cache.get("message/message_resource.db")
        if not path:
            return None

        conn = sqlite3.connect(path)
        try:
            row = conn.execute(
                "SELECT packed_info FROM MessageResourceInfo WHERE local_id = ?",
                (local_id,)
            ).fetchone()
            if row and row[0]:
                return extract_md5_from_packed_info(row[0])
        except Exception:
            pass
        finally:
            conn.close()

        return None

    def find_dat_files(self, username, file_md5):
        """在 attach 目录下查找对应的 .dat 文件

        路径: attach/<md5(username)>/<YYYY-MM>/Img/<file_md5>[_t|_h].dat
        """
        username_hash = hashlib.md5(username.encode()).hexdigest()
        search_base = os.path.join(self.attach_dir, username_hash)

        if not os.path.isdir(search_base):
            return []

        # 在所有月份目录下搜索
        results = []
        pattern = os.path.join(search_base, "*", "Img", f"{file_md5}*.dat")
        for p in glob.glob(pattern):
            results.append(p)

        return sorted(results)

    def decode_image(self, username, local_id):
        """完整流程：local_id → MD5 → .dat → 解密

        Returns:
            dict with keys: success, path, format, md5, error
        """
        # 1. 获取 MD5
        file_md5 = self.get_image_md5(local_id)
        if not file_md5:
            return {'success': False, 'error': f'无法从 message_resource.db 找到 local_id={local_id} 的图片信息'}

        # 2. 找 .dat 文件
        dat_files = self.find_dat_files(username, file_md5)
        if not dat_files:
            return {'success': False, 'error': f'找不到 .dat 文件 (MD5={file_md5})', 'md5': file_md5}

        # 优先选标准版（非 _t/_h），然后高清 _h，最后缩略图 _t
        selected = dat_files[0]
        for f in dat_files:
            fname = os.path.basename(f)
            if not fname.startswith(file_md5 + '_'):
                selected = f
                break
        for f in dat_files:
            if f.endswith('_h.dat'):
                selected = f
                break

        # 3. 解密 (decrypt_dat_file 会按 magic 自动分发 V2 / V1 / 老 XOR)
        out_name = f"{file_md5}"
        out_path_base = os.path.join(self.out_dir, out_name)

        # 提前拦截以给出具体错误信息;否则会在 v2_decrypt_file 内 silent-fail 成笼统的"解密失败"
        if is_v2_format(selected) and not self.aes_key:
            return {'success': False, 'error': f'V2 格式 .dat 文件需要 AES key (文件: {selected})', 'md5': file_md5}

        result_path, fmt = decrypt_dat_file(selected, f"{out_path_base}.tmp", self.aes_key, self.xor_key)
        if not result_path:
            return {'success': False, 'error': f'解密失败 (文件: {selected})', 'md5': file_md5}

        # 重命名为正确扩展名
        final_path = f"{out_path_base}.{fmt}"
        if os.path.exists(final_path):
            os.unlink(final_path)
        os.rename(result_path, final_path)

        return {
            'success': True,
            'path': final_path,
            'format': fmt,
            'md5': file_md5,
            'source': selected,
            'size': os.path.getsize(final_path),
        }

    def list_chat_images(self, db_path, table_name, username, limit=20):
        """列出某个聊天中的所有图片消息"""
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(f"""
                SELECT local_id, create_time
                FROM [{table_name}]
                WHERE local_type = 3
                ORDER BY create_time DESC
                LIMIT ?
            """, (limit,)).fetchall()
        except Exception as e:
            conn.close()
            return []
        conn.close()

        results = []
        for local_id, create_time in rows:
            file_md5 = self.get_image_md5(local_id)
            info = {
                'local_id': local_id,
                'create_time': create_time,
                'md5': file_md5,
            }
            if file_md5:
                dat_files = self.find_dat_files(username, file_md5)
                if dat_files:
                    info['dat_file'] = dat_files[0]
                    try:
                        info['size'] = os.path.getsize(dat_files[0])
                    except OSError:
                        pass
            results.append(info)

        return results


# ============ CLI 测试 ============

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python decode_image.py <dat_file> [output_file]")
        print("  解密单个 .dat 文件")
        sys.exit(1)

    dat_file = sys.argv[1]
    out_file = sys.argv[2] if len(sys.argv) > 2 else None

    if not os.path.exists(dat_file):
        print(f"文件不存在: {dat_file}")
        sys.exit(1)

    result_path, fmt = decrypt_dat_file(dat_file, out_file)
    if result_path:
        size = os.path.getsize(result_path)
        print(f"解密成功: {result_path}")
        print(f"格式: {fmt}, 大小: {size:,} bytes")
    else:
        print("解密失败")
        sys.exit(1)
