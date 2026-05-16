"""批量解密 .dat 图片文件

用法: python batch_decrypt_images.py <文件夹路径> [输出目录]

递归扫描指定文件夹下的所有 .dat 文件并解密。
输出目录默认为 <文件夹路径>_decoded/，保持原有子目录结构。
"""
import os
import sys
import glob
import struct

# Windows 控制台 UTF-8
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config import load_config

_cfg = load_config()
IMAGE_AES_KEY = _cfg.get("image_aes_key", "")
IMAGE_XOR_KEY = _cfg.get("image_xor_key", 0x88)

# ── V2/V1 magic ──────────────────────────────────────────────────────────────
_V2_MAGIC_FULL = b'\x07\x08V2\x08\x07'
_V1_MAGIC_FULL = b'\x07\x08V1\x08\x07'

_IMAGE_MAGICS = {
    'jpg': [0xFF, 0xD8, 0xFF],
    'png': [0x89, 0x50, 0x4E, 0x47],
    'gif': [0x47, 0x49, 0x46, 0x38],
    'webp': [0x52, 0x49, 0x46, 0x46],
    'bmp': [0x42, 0x4D],
    'tif': [0x49, 0x49, 0x2A, 0x00],
}


def _detect_format(header):
    if header[:3] == bytes([0xFF, 0xD8, 0xFF]):
        return 'jpg'
    if header[:4] == bytes([0x89, 0x50, 0x4E, 0x47]):
        return 'png'
    if header[:3] == b'GIF':
        return 'gif'
    if header[:2] == b'BM':
        return 'bmp'
    if header[:4] == b'RIFF' and len(header) >= 12 and header[8:12] == b'WEBP':
        return 'webp'
    if header[:4] == bytes([0x49, 0x49, 0x2A, 0x00]):
        return 'tif'
    if header[:4] == b'wxgf':
        return 'hevc'
    return 'bin'


def decrypt_dat(dat_path):
    """解密单个 .dat 文件，返回 (bytes, format) 或 (None, None)"""
    with open(dat_path, 'rb') as f:
        data = f.read()
    if len(data) < 6:
        return None, None

    head6 = data[:6]

    # V2 / V1 格式 (AES-ECB + XOR)
    if head6 in (_V2_MAGIC_FULL, _V1_MAGIC_FULL):
        if head6 == _V1_MAGIC_FULL:
            aes_key = b'cfcd208495d565ef'
        elif IMAGE_AES_KEY:
            aes_key = IMAGE_AES_KEY.encode('ascii')[:16] if isinstance(IMAGE_AES_KEY, str) else IMAGE_AES_KEY[:16]
        else:
            return None, None
        if len(aes_key) < 16:
            return None, None
        try:
            from Crypto.Cipher import AES
            from Crypto.Util import Padding
            if len(data) < 15:
                return None, None
            aes_size, xor_size = struct.unpack_from('<LL', data, 6)
            aligned = aes_size - ~(~aes_size % 16)
            offset = 15
            if offset + aligned > len(data):
                return None, None
            cipher = AES.new(aes_key[:16], AES.MODE_ECB)
            dec_aes = Padding.unpad(cipher.decrypt(data[offset:offset+aligned]), AES.block_size)
            offset += aligned
            raw_end = len(data) - xor_size
            raw_data = data[offset:raw_end] if offset < raw_end else b''
            xor_data = data[raw_end:]
            xor_key = IMAGE_XOR_KEY if isinstance(IMAGE_XOR_KEY, int) else 0x88
            dec_xor = bytes(b ^ xor_key for b in xor_data)
            result = dec_aes + raw_data + dec_xor
            fmt = _detect_format(result[:16])
            return result, fmt
        except Exception as e:
            print(f"  AES 解密失败: {e}")
            return None, None

    # 旧 XOR 格式
    for fmt_name, magic in _IMAGE_MAGICS.items():
        key = data[0] ^ magic[0]
        match = all(i < len(data) and (data[i] ^ key) == magic[i] for i in range(len(magic)))
        if match:
            result = bytes(b ^ key for b in data)
            fmt = _detect_format(result[:16])
            return result, fmt

    return None, None


def main():
    if len(sys.argv) < 2:
        print("用法: python batch_decrypt_images.py <文件夹路径> [输出目录]")
        print("  递归扫描文件夹下所有 .dat 文件并解密")
        sys.exit(1)

    source_dir = os.path.abspath(sys.argv[1])
    if not os.path.isdir(source_dir):
        print(f"目录不存在: {source_dir}")
        sys.exit(1)

    if len(sys.argv) >= 3:
        output_dir = os.path.abspath(sys.argv[2])
    else:
        output_dir = source_dir.rstrip(os.sep) + "_decoded"

    # 递归收集所有 .dat 文件
    dat_files = []
    for root, _dirs, files in os.walk(source_dir):
        for f in files:
            if f.lower().endswith('.dat'):
                dat_files.append(os.path.join(root, f))
    dat_files.sort()

    print(f"源目录: {source_dir}")
    print(f"输出目录: {output_dir}")
    print(f"找到 {len(dat_files)} 个 .dat 文件")
    print()

    total = len(dat_files)
    success = 0
    skipped = 0
    failed = 0

    for dat_path in dat_files:
        # 保持相对目录结构
        rel = os.path.relpath(dat_path, source_dir)
        rel_dir = os.path.dirname(rel)
        out_subdir = os.path.join(output_dir, rel_dir) if rel_dir else output_dir

        fname = os.path.splitext(os.path.basename(dat_path))[0]
        # 去除 _t / _h 后缀获取基础名
        base_name = fname
        for suffix in ('_t', '_h'):
            if base_name.endswith(suffix):
                base_name = base_name[:-len(suffix)]
                break

        # 检查是否已解密
        existing = glob.glob(os.path.join(out_subdir, f"{base_name}.*"))
        if existing:
            skipped += 1
            continue

        img_bytes, fmt = decrypt_dat(dat_path)
        if not img_bytes or fmt == 'bin':
            failed += 1
            continue

        os.makedirs(out_subdir, exist_ok=True)
        out_path = os.path.join(out_subdir, f"{base_name}.{fmt}")
        with open(out_path, 'wb') as f:
            f.write(img_bytes)
        success += 1

    print(f"完成: 共 {total} 个文件, 成功 {success}, 跳过(已存在) {skipped}, 失败 {failed}")
    print(f"输出: {output_dir}")


if __name__ == "__main__":
    main()
