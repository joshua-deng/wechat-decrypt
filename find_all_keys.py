import functools
import platform
import sys
import os
import glob
import json
import hashlib
import multiprocessing
import time
from config import load_config
from Crypto.Cipher import AES


def find_v2_ciphertext(attach_dir):
    v2_magic = b'\x07\x08V2\x08\x07'
    pattern = os.path.join(attach_dir, "*", "*", "Img", "*_t.dat")
    dat_files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)

    for f in dat_files[:100]:
        try:
            with open(f, 'rb') as fp:
                header = fp.read(31)
            if header[:6] == v2_magic and len(header) >= 31:
                return header[15:31], os.path.basename(f)
        except Exception:
            continue
    return None, None


def find_xor_key(attach_dir):
    v2_magic = b'\x07\x08V2\x08\x07'
    pattern = os.path.join(attach_dir, "*", "*", "Img", "*_t.dat")
    dat_files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)

    tail_counts = {}
    for f in dat_files[:32]:
        try:
            sz = os.path.getsize(f)
            with open(f, 'rb') as fp:
                head = fp.read(6)
                fp.seek(sz - 2)
                tail = fp.read(2)
            if head == v2_magic and len(tail) == 2:
                key = (tail[0], tail[1])
                tail_counts[key] = tail_counts.get(key, 0) + 1
        except Exception:
            continue

    if not tail_counts:
        return None

    most_common = max(tail_counts, key=tail_counts.get)
    x, y = most_common
    xor_key = x ^ 0xFF
    if (y ^ 0xD9) == xor_key:
        return xor_key
    return None


def try_key(key_bytes, ciphertext):
    try:
        cipher = AES.new(key_bytes, AES.MODE_ECB)
        dec = cipher.decrypt(ciphertext)
        if dec[:3] == b'\xFF\xD8\xFF': return 'JPEG'
        if dec[:4] == b'\x89PNG': return 'PNG'
        if dec[:4] == b'RIFF': return 'WEBP'
        if dec[:4] == b'wxgf': return 'WXGF'
        if dec[:3] == b'GIF': return 'GIF'
    except Exception:
        pass
    return None


def _brute_worker(start_i, end_i, xor_key, bin_suffix, base_wxid_bytes, ciphertext_16, result_queue):
    for i in range(start_i, end_i):
        uin = (i << 8) | xor_key
        uin_bytes = str(uin).encode('ascii')
        
        if hashlib.md5(uin_bytes).digest()[:2] == bin_suffix:
            h_aes = hashlib.md5(uin_bytes + base_wxid_bytes).hexdigest()
            aes_key_16 = h_aes[:16].encode('ascii')
            
            if try_key(aes_key_16, ciphertext_16):
                result_queue.put((uin, aes_key_16.decode('ascii')))
                return


def find_image_key_offline(cfg):
    print("\n" + "=" * 60)
    print("  尝试提取图片 AES 密钥")
    print("=" * 60)

    db_dir = cfg.get("db_dir", "")
    if not db_dir:
        print("未配置 db_dir")
        return
        
    base_dir = os.path.dirname(db_dir)
    attach_dir = os.path.join(base_dir, 'msg', 'attach')
    
    folder = os.path.basename(base_dir)
    base_wxid, suffix = "", ""
    if '_' in folder:
        parts = folder.rsplit('_', 1)
        if len(parts) == 2 and len(parts[1]) == 4:
            base_wxid, suffix = parts
            
    if not base_wxid or not suffix:
        print(f"[!] 目录名不符合 wxid_..._suffix 格式: {folder}，跳过爆破")
        return
        
    print(f"[*] 解析到 wxid={base_wxid}, suffix={suffix}")
    
    xor_key = find_xor_key(attach_dir)
    if xor_key is None:
        print("[!] 找不到足够的 _t.dat 文件推导 XOR key，跳过爆破")
        print("    请先在微信中查看 2-3 张图片，让缩略图缓存到本地后再重试。")
        return
    print(f"[*] 找到 XOR key: 0x{xor_key:02x}")

    ciphertext, ct_file = find_v2_ciphertext(attach_dir)
    if not ciphertext:
        print("[!] 找不到 V2 加密的图片文件，跳过爆破")
        print("    请先在微信中查看 2-3 张图片，让缩略图缓存到本地后再重试。")
        return
        
    print(f"[*] 启动多进程 UIN 空间爆破...")
    t0 = time.time()
    
    bin_suffix = bytes.fromhex(suffix)
    base_wxid_bytes = base_wxid.encode('ascii')
    
    cpu_count = multiprocessing.cpu_count()
    total = 1 << 24
    chunk = total // cpu_count
    
    result_queue = multiprocessing.Queue()
    processes = []
    
    for i in range(cpu_count):
        start, end = i * chunk, (i + 1) * chunk if i != cpu_count - 1 else total
        p = multiprocessing.Process(
            target=_brute_worker,
            args=(start, end, xor_key, bin_suffix, base_wxid_bytes, ciphertext, result_queue)
        )
        p.start()
        processes.append(p)
    
    found = None
    try:
        while any(p.is_alive() for p in processes):
            if not result_queue.empty():
                found = result_queue.get()
                break
            time.sleep(0.1)
    finally:
        for p in processes:
            p.terminate()
        for p in processes:
            p.join(timeout=1)

    elapsed = time.time() - t0
    if found:
        print(f"[+] 爆破成功! UIN={found[0]}, 耗时={elapsed:.1f}s")
        aes_key = found[1]
        print(f"    image_aes_key = {aes_key}")
        
        cfg['image_aes_key'] = aes_key
        cfg['image_xor_key'] = xor_key
        config_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        with open(config_file, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
        print(f"[+] 已保存到 config.json")
    else:
        print(f"[-] 未能在 UIN 空间找到有效密钥 (耗时={elapsed:.1f}s)")
        print("    可能原因: 目录名被重命名过，或者不是标准账号目录。")


@functools.lru_cache(maxsize=1)
def _load_impl():
    system = platform.system().lower()
    if system == "windows":
        import find_all_keys_windows as impl
        return impl
    if system == "linux":
        import find_all_keys_linux as impl
        return impl
    if system == "darwin":
        raise RuntimeError(
            "macOS 请先运行 C 版扫描器提取数据库密钥：\n"
            "\n"
            "    sudo ./find_all_keys_macos\n"
            "\n"
            "    完成后再运行 python main.py decrypt"
        )
    raise RuntimeError(
        f"当前平台暂不支持通过 find_all_keys.py 提取内存数据库密钥: {platform.system()}"
    )


def get_pids():
    return _load_impl().get_pids()


def main():
    cfg = load_config()

    find_image_key_offline(cfg)

    return _load_impl().main()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        main()
    except RuntimeError as exc:
        print(f"\n[ERROR] {exc}")
        sys.exit(1)
