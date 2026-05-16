"""
从企业微信(WXWork)进程内存中提取所有数据库的缓存raw key

企业微信的本地数据库与个人微信不同。实测 Windows 版使用 wxSQLite3
AES-128-CBC 页面加密：16 字节 raw key，每页按 page index 派生 AES key
和 IV；页面没有 SQLCipher HMAC/reserve 区。
"""
import ctypes
import ctypes.wintypes as wt
import bisect
import functools
import hashlib
import hmac as hmac_mod
import json
import os
import re
import struct
import subprocess
import sys
import time

from key_scan_common import collect_db_files
from wxwork_crypto import (
    is_plain_sqlite_page,
    is_wxsqlite3_aes128_page1,
    verify_wxsqlite3_aes128_key,
)

print = functools.partial(print, flush=True)

# ── Windows 内存读取原语 ──────────────────────────────────────────────

kernel32 = ctypes.windll.kernel32
MEM_COMMIT = 0x1000
READABLE = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}


class MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_uint64), ("AllocationBase", ctypes.c_uint64),
        ("AllocationProtect", wt.DWORD), ("_pad1", wt.DWORD),
        ("RegionSize", ctypes.c_uint64), ("State", wt.DWORD),
        ("Protect", wt.DWORD), ("Type", wt.DWORD), ("_pad2", wt.DWORD),
    ]


def read_mem(h, addr, sz):
    buf = ctypes.create_string_buffer(sz)
    n = ctypes.c_size_t(0)
    if kernel32.ReadProcessMemory(h, ctypes.c_uint64(addr), buf, sz, ctypes.byref(n)):
        return buf.raw[:n.value]
    return None


def enum_regions(h):
    regs = []
    addr = 0
    mbi = MBI()
    while addr < 0x7FFFFFFFFFFF:
        if kernel32.VirtualQueryEx(h, ctypes.c_uint64(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)) == 0:
            break
        if mbi.State == MEM_COMMIT and mbi.Protect in READABLE and 0 < mbi.RegionSize < 500 * 1024 * 1024:
            regs.append((mbi.BaseAddress, mbi.RegionSize))
        nxt = mbi.BaseAddress + mbi.RegionSize
        if nxt <= addr:
            break
        addr = nxt
    return regs


# ── 常量 ─────────────────────────────────────────────────────────────

WXWORK_PROCESS = "WXWork.exe"

PAGE_SZ = 4096
SALT_SZ = 16

# 旧版本/其他平台可能回落到 SQLCipher 参数，保留作兼容验证。
# (key_sz, hmac_hash_name, hmac_sz, pbkdf2_iter, reserve_sz)
VERIFY_CONFIGS = [
    # WCDB optimized cipher with AES-128, HMAC-SHA512 (最可能)
    (16, "sha512", 64, 2, 80),
    # WCDB with AES-128, HMAC-SHA256
    (16, "sha256", 32, 2, 48),
    # SQLCipher 3 defaults with AES-128
    (16, "sha512", 64, 4000, 80),
    (16, "sha256", 32, 4000, 48),
    # AES-256 回退 (与个人微信相同参数)
    (32, "sha512", 64, 2, 80),
]


def verify_enc_key_wxwork(enc_key, db_page1):
    """尝试多种参数组合验证密钥，返回 (成功?, 使用的配置描述)"""
    if len(enc_key) == 16 and verify_wxsqlite3_aes128_key(enc_key, db_page1):
        return True, "wxSQLite3 AES-128-CBC, per-page MD5 key/IV, no HMAC"

    key_sz = len(enc_key)
    for cfg_key_sz, hmac_hash, hmac_sz, iterations, reserve_sz in VERIFY_CONFIGS:
        if key_sz != cfg_key_sz:
            continue
        salt = db_page1[:SALT_SZ]
        mac_salt = bytes(b ^ 0x3A for b in salt)
        mac_key = hashlib.pbkdf2_hmac(hmac_hash, enc_key, mac_salt, iterations, dklen=cfg_key_sz)
        hmac_data = db_page1[SALT_SZ: PAGE_SZ - reserve_sz + 16]
        stored_hmac = db_page1[PAGE_SZ - hmac_sz: PAGE_SZ]
        hash_fn = getattr(hashlib, hmac_hash)
        hm = hmac_mod.new(mac_key, hmac_data, hash_fn)
        hm.update(struct.pack("<I", 1))
        if hm.digest() == stored_hmac:
            desc = f"AES-{cfg_key_sz * 8}, HMAC-{hmac_hash.upper()}, iter={iterations}"
            return True, desc
    return False, ""


# ── WXWork 进程发现 ──────────────────────────────────────────────────

def get_wxwork_pids():
    """返回所有 WXWork.exe 进程的 (pid, mem_kb) 列表，按内存降序"""
    r = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {WXWORK_PROCESS}", "/FO", "CSV", "/NH"],
        capture_output=True, text=True,
    )
    pids = []
    for line in r.stdout.strip().split('\n'):
        if not line.strip():
            continue
        p = line.strip('"').split('","')
        if len(p) >= 5:
            pid = int(p[1])
            mem = int(p[4].replace(',', '').replace(' K', '').strip() or '0')
            pids.append((pid, mem))
    if not pids:
        raise RuntimeError(f"{WXWORK_PROCESS} 未运行")
    pids.sort(key=lambda x: x[1], reverse=True)
    for pid, mem in pids:
        print(f"[+] {WXWORK_PROCESS} PID={pid} ({mem // 1024}MB)")
    return pids


# ── WXWork 数据目录自动检测 ──────────────────────────────────────────

def auto_detect_wxwork_db_dir():
    """扫描 %USERPROFILE%\\Documents\\WXWork\\*\\Data 寻找包含加密DB的目录"""
    docs = os.path.join(os.environ.get("USERPROFILE", ""), "Documents", "WXWork")
    if not os.path.isdir(docs):
        return None

    candidates = []
    for name in os.listdir(docs):
        data_dir = os.path.join(docs, name, "Data")
        if not os.path.isdir(data_dir):
            continue
        has_encrypted = False
        for fname in os.listdir(data_dir):
            if not fname.endswith(".db"):
                continue
            fpath = os.path.join(data_dir, fname)
            if os.path.getsize(fpath) < PAGE_SZ:
                continue
            with open(fpath, "rb") as f:
                header = f.read(16)
            if header != b"SQLite format 3\x00":
                has_encrypted = True
                break
        if has_encrypted:
            candidates.append(data_dir)

    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    if not sys.stdin.isatty():
        return candidates[0]
    print("[!] 检测到多个企业微信数据目录:")
    for i, c in enumerate(candidates, 1):
        print(f"    {i}. {c}")
    print("    0. 跳过，稍后手动配置")
    try:
        while True:
            choice = input(f"请选择 [0-{len(candidates)}]: ").strip()
            if choice == "0":
                return None
            if choice.isdigit() and 1 <= int(choice) <= len(candidates):
                return candidates[int(choice) - 1]
            print("    无效输入，请重新选择")
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def filter_encrypted_dbs(db_files, salt_to_dbs):
    """过滤掉未加密的数据库。"""
    filtered_files = [
        entry for entry in db_files if not is_plain_sqlite_page(entry[4])
    ]
    filtered_salts = {
        s: dbs for s, dbs in salt_to_dbs.items()
        if any(entry[3] == s and not is_plain_sqlite_page(entry[4]) for entry in db_files)
    }
    removed = len(db_files) - len(filtered_files)
    if removed:
        print(f"[*] 跳过 {removed} 个未加密数据库")
    wxsqlite3_count = sum(1 for entry in filtered_files if is_wxsqlite3_aes128_page1(entry[4]))
    if wxsqlite3_count:
        print(f"[*] 检测到 {wxsqlite3_count} 个 wxSQLite3 AES-128 格式数据库")
    return filtered_files, filtered_salts


# ── 企业微信内存扫描 ─────────────────────────────────────────────────

def scan_memory_for_wxwork_keys(data, hex_re, db_files, salt_to_dbs, key_map,
                                remaining_salts, base_addr, pid, print_fn):
    """扫描内存，匹配 hex 模式并用企业微信参数验证密钥。

    企业微信 key=16字节(32 hex), salt=16字节(32 hex)
    可能的缓存格式:
      - x'<32hex_key><32hex_salt>' = 64 hex total
      - x'<32hex_key>'            = 32 hex (key only)
      - x'<64hex_key><32hex_salt>' = 96 hex (AES-256 回退)
    """
    matches = 0
    for m in hex_re.finditer(data):
        hex_str = m.group(1).decode()
        addr = base_addr + m.start()
        matches += 1
        hex_len = len(hex_str)

        # 尝试不同的解释方式
        candidates = []

        if hex_len == 32:
            # 纯 16字节 key
            candidates.append((hex_str, None))

        elif hex_len == 64:
            # 优先: 32hex key + 32hex salt (WeCom AES-128)
            candidates.append((hex_str[:32], hex_str[32:]))
            # 回退: 64hex = 32字节 key (personal WeChat AES-256)
            candidates.append((hex_str, None))

        elif hex_len == 96:
            # 优先: 64hex key + 32hex salt (personal WeChat AES-256)
            candidates.append((hex_str[:64], hex_str[64:]))
            # 也尝试: 32hex key + ... + 32hex salt
            candidates.append((hex_str[:32], hex_str[-32:]))

        elif hex_len > 96 and hex_len % 2 == 0:
            candidates.append((hex_str[:64], hex_str[-32:]))
            candidates.append((hex_str[:32], hex_str[-32:]))

        for enc_key_hex, salt_hex in candidates:
            if len(enc_key_hex) not in (32, 64):
                continue
            enc_key = bytes.fromhex(enc_key_hex)

            if salt_hex and salt_hex in remaining_salts:
                # salt 匹配已知数据库
                for rel, path, sz, s, page1 in db_files:
                    if s == salt_hex:
                        ok, desc = verify_enc_key_wxwork(enc_key, page1)
                        if ok:
                            key_map[salt_hex] = enc_key_hex
                            remaining_salts.discard(salt_hex)
                            dbs = salt_to_dbs[salt_hex]
                            print_fn(f"\n  [FOUND] salt={salt_hex}")
                            print_fn(f"    enc_key={enc_key_hex}")
                            print_fn(f"    params: {desc}")
                            print_fn(f"    PID={pid} 地址: 0x{addr:016X}")
                            print_fn(f"    数据库: {', '.join(dbs)}")
                            break
            elif not salt_hex and remaining_salts:
                # 没有 salt，暴力尝试所有未匹配的数据库
                for rel, path, sz, salt_hex_db, page1 in db_files:
                    if salt_hex_db in remaining_salts:
                        ok, desc = verify_enc_key_wxwork(enc_key, page1)
                        if ok:
                            key_map[salt_hex_db] = enc_key_hex
                            remaining_salts.discard(salt_hex_db)
                            dbs = salt_to_dbs[salt_hex_db]
                            print_fn(f"\n  [FOUND] salt={salt_hex_db}")
                            print_fn(f"    enc_key={enc_key_hex}")
                            print_fn(f"    params: {desc}")
                            print_fn(f"    PID={pid} 地址: 0x{addr:016X}")
                            print_fn(f"    数据库: {', '.join(dbs)}")
                            break

            if not remaining_salts:
                break

    return matches


def _find_region(memory_regions, starts, addr, length=4):
    idx = bisect.bisect_right(starts, addr) - 1
    if idx < 0:
        return None
    base, end, data = memory_regions[idx]
    if base <= addr and addr + length <= end:
        return base, end, data
    return None


def _read_u32(memory_regions, starts, addr):
    region = _find_region(memory_regions, starts, addr, 4)
    if not region:
        return None
    base, _end, data = region
    return struct.unpack_from("<I", data, addr - base)[0]


def _valid_ptr(memory_regions, starts, addr, length=4):
    return _find_region(memory_regions, starts, addr, length) is not None


def _wxwork_page_size_chain(memory_regions, starts, cipher_addr):
    """Validate the AES cipher object by following the page-size pointer chain.

    In WXWork 5.x's inlined wxSQLite3 AES-128 code, the decrypt path uses:
      raw_key = cipher + 0x08
      aes_ctx = *(cipher + 0x2c)
      page_size = *(*(*(cipher + 0x30) + 0x04) + 0x24)
    """
    page_size_holder = _read_u32(memory_regions, starts, cipher_addr + 0x30)
    if page_size_holder is None or not _valid_ptr(memory_regions, starts, page_size_holder, 8):
        return None
    page_size_obj = _read_u32(memory_regions, starts, page_size_holder + 4)
    if page_size_obj is None or not _valid_ptr(memory_regions, starts, page_size_obj + 0x24, 4):
        return None
    return _read_u32(memory_regions, starts, page_size_obj + 0x24)


def _record_candidate_key(enc_key, db_files, salt_to_dbs, key_map,
                          remaining_salts, pid, addr, desc, print_fn):
    matched = []
    params_desc = desc
    for rel, path, sz, salt_hex, page1 in db_files:
        if salt_hex not in remaining_salts:
            continue
        ok, verified_desc = verify_enc_key_wxwork(enc_key, page1)
        if ok:
            key_map[salt_hex] = enc_key.hex()
            remaining_salts.discard(salt_hex)
            params_desc = verified_desc or params_desc
            matched.extend(salt_to_dbs[salt_hex])

    if matched:
        print_fn(f"\n  [FOUND-STRUCT] enc_key={enc_key.hex()}")
        print_fn(f"    params: {params_desc}")
        print_fn(f"    PID={pid} cipher对象地址: 0x{addr:08X}")
        print_fn(f"    数据库: {', '.join(sorted(set(matched)))}")
    return bool(matched)


def scan_memory_for_wxwork_cipher_structs(h, regions, db_files, salt_to_dbs,
                                          key_map, remaining_salts, pid,
                                          print_fn, max_seconds=120):
    """Scan WXWork heap objects for the in-memory wxSQLite3 AES-128 cipher.

    This is intentionally targeted: instead of brute-forcing every 16-byte
    window as a key, it looks for the cipher object layout used by WXWork 5.x.
    """
    t0 = time.time()
    memory_regions = []
    total_bytes = 0
    for base, size in regions:
        data = read_mem(h, base, size)
        if data:
            memory_regions.append((int(base), int(base) + len(data), data))
            total_bytes += len(data)

    memory_regions.sort(key=lambda item: item[0])
    starts = [item[0] for item in memory_regions]
    print_fn(f"[*] 结构体扫描内存: {total_bytes / 1024 / 1024:.0f}MB, {len(memory_regions)} 区域")

    checked = 0
    ptr_hits = 0
    chain_hits = 0
    key_tests = 0
    page_sizes = {512, 1024, 2048, 4096, 8192, 16384, 32768, 65536}

    for base, end, data in memory_regions:
        max_off = len(data) - 0x40
        off = 0
        while off >= 0 and off < max_off:
            if time.time() - t0 > max_seconds:
                print_fn(
                    f"[WARN] 结构体扫描超时: checked={checked}, "
                    f"ptr_hits={ptr_hits}, chain_hits={chain_hits}, key_tests={key_tests}"
                )
                return key_tests

            # The AES-128 decrypt branch checks two non-zero flags at +0 and +4.
            flag0, flag4 = struct.unpack_from("<II", data, off)
            if flag0 in (1, 2) and flag4 in (1, 2, 4096, 8192, 16384):
                cipher_addr = base + off
                aes_ctx = struct.unpack_from("<I", data, off + 0x2C)[0]
                if _valid_ptr(memory_regions, starts, aes_ctx, 0x40):
                    ptr_hits += 1
                    page_size = _wxwork_page_size_chain(memory_regions, starts, cipher_addr)
                    if page_size in page_sizes:
                        chain_hits += 1
                        enc_key = data[off + 8 : off + 24]
                        if enc_key != b"\x00" * 16 and len(set(enc_key)) >= 6:
                            key_tests += 1
                            if _record_candidate_key(
                                enc_key, db_files, salt_to_dbs, key_map,
                                remaining_salts, pid, cipher_addr,
                                f"wxSQLite3 AES-128-CBC, page_size={page_size}",
                                print_fn,
                            ):
                                if not remaining_salts:
                                    return key_tests

            checked += 1
            off += 4

    print_fn(
        f"[*] 结构体扫描完成: checked={checked}, ptr_hits={ptr_hits}, "
        f"chain_hits={chain_hits}, key_tests={key_tests}"
    )
    return key_tests


def cross_verify_wxwork_keys(db_files, salt_to_dbs, key_map, print_fn):
    """用已找到的 key 交叉验证未匹配的 salt。"""
    missing_salts = set(salt_to_dbs.keys()) - set(key_map.keys())
    if not missing_salts or not key_map:
        return
    print_fn(f"\n还有 {len(missing_salts)} 个 salt 未匹配，尝试交叉验证...")
    for salt_hex in list(missing_salts):
        for rel, path, sz, s, page1 in db_files:
            if s == salt_hex:
                for known_salt, known_key_hex in key_map.items():
                    enc_key = bytes.fromhex(known_key_hex)
                    ok, desc = verify_enc_key_wxwork(enc_key, page1)
                    if ok:
                        key_map[salt_hex] = known_key_hex
                        print_fn(f"  [CROSS] salt={salt_hex} 可用 key from salt={known_salt}")
                        missing_salts.discard(salt_hex)
                break


def save_wxwork_results(db_files, salt_to_dbs, key_map, db_dir, out_file, print_fn):
    """输出扫描结果并保存 JSON。"""
    print_fn(f"\n{'=' * 60}")
    print_fn(f"结果: {len(key_map)}/{len(salt_to_dbs)} salts 找到密钥")

    result = {}
    for rel, path, sz, salt_hex, page1 in db_files:
        if salt_hex in key_map:
            result[rel] = {
                "enc_key": key_map[salt_hex],
                "salt": salt_hex,
                "size_mb": round(sz / 1024 / 1024, 1)
            }
            print_fn(f"  OK: {rel} ({sz / 1024 / 1024:.1f}MB)")
        else:
            print_fn(f"  MISSING: {rel} (salt={salt_hex})")

    if not result:
        print_fn(f"\n[!] 未提取到任何密钥，保留已有的 {out_file}（如存在）")
        raise RuntimeError("未能从任何企业微信进程中提取到密钥")

    result["_db_dir"] = db_dir
    with open(out_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print_fn(f"\n密钥保存到: {out_file}")

    missing = [rel for rel, path, sz, salt_hex, page1 in db_files if salt_hex not in key_map]
    if missing:
        print_fn(f"\n未找到密钥的数据库:")
        for rel in missing:
            print_fn(f"  {rel}")


# ── 配置加载 ─────────────────────────────────────────────────────────

def _load_wxwork_config():
    """从 config.json 加载企业微信配置，必要时自动检测"""
    from config import _config_file_path, _app_base_dir

    config_file = _config_file_path()
    cfg = {}
    if os.path.exists(config_file):
        try:
            with open(config_file, encoding="utf-8") as f:
                cfg = json.load(f)
        except json.JSONDecodeError:
            cfg = {}

    db_dir = cfg.get("wxwork_db_dir", "")
    if not db_dir or not os.path.isdir(db_dir):
        detected = auto_detect_wxwork_db_dir()
        if detected:
            print(f"[+] 自动检测到企业微信数据目录: {detected}")
            cfg["wxwork_db_dir"] = detected
            with open(config_file, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=4, ensure_ascii=False)
            print(f"[+] 已保存到: {config_file}")
            db_dir = detected
        else:
            print("[!] 未能自动检测企业微信数据目录")
            print(f"    请在 {config_file} 中设置 wxwork_db_dir 字段")
            print("    路径格式: C:\\Users\\<用户>\\Documents\\WXWork\\<account_id>\\Data")
            sys.exit(1)

    keys_file = cfg.get("wxwork_keys_file", "wxwork_keys.json")
    base = _app_base_dir()
    if not os.path.isabs(keys_file):
        keys_file = os.path.join(base, keys_file)

    return {"wxwork_db_dir": db_dir, "wxwork_keys_file": keys_file}


# ── 主流程 ───────────────────────────────────────────────────────────

def main():
    cfg = _load_wxwork_config()
    db_dir = cfg["wxwork_db_dir"]
    out_file = cfg["wxwork_keys_file"]

    print("=" * 60)
    print("  提取所有企业微信数据库密钥")
    print("=" * 60)

    # 1. 收集所有DB文件及其salt
    db_files, salt_to_dbs = collect_db_files(db_dir)
    db_files, salt_to_dbs = filter_encrypted_dbs(db_files, salt_to_dbs)

    print(f"\n找到 {len(db_files)} 个加密数据库, {len(salt_to_dbs)} 个不同的salt")
    for salt_hex, dbs in sorted(salt_to_dbs.items(), key=lambda x: len(x[1]), reverse=True):
        print(f"  salt {salt_hex}: {', '.join(dbs)}")

    # 2. 打开所有企业微信进程
    pids = get_wxwork_pids()

    # Some versions do not keep the key as SQL literal x'...'. Bare ASCII
    # hex scanning is much slower, so keep it behind an explicit switch.
    hex_re = re.compile(b"x'([0-9a-fA-F]{32,192})'")
    scan_bare_hex = "--scan-bare-hex" in sys.argv
    bare_hex_re = re.compile(b"(?<![0-9a-fA-F])([0-9a-fA-F]{32})(?![0-9a-fA-F])")
    if scan_bare_hex:
        print("[*] 已启用裸 32-hex key 扫描，速度会明显变慢")
    key_map = {}
    remaining_salts = set(salt_to_dbs.keys())
    all_hex_matches = 0
    all_bare_hex_matches = 0
    t0 = time.time()

    for pid, mem_kb in pids:
        h = kernel32.OpenProcess(0x0010 | 0x0400, False, pid)
        if not h:
            print(f"[WARN] 无法打开进程 PID={pid}，跳过")
            continue

        try:
            regions = enum_regions(h)
            total_bytes = sum(s for _, s in regions)
            total_mb = total_bytes / 1024 / 1024
            print(f"\n[*] 扫描 PID={pid} ({total_mb:.0f}MB, {len(regions)} 区域)")

            scanned_bytes = 0
            for reg_idx, (base, size) in enumerate(regions):
                data = read_mem(h, base, size)
                scanned_bytes += size
                if not data:
                    continue

                all_hex_matches += scan_memory_for_wxwork_keys(
                    data, hex_re, db_files, salt_to_dbs,
                    key_map, remaining_salts, base, pid, print,
                )
                if scan_bare_hex and remaining_salts:
                    all_bare_hex_matches += scan_memory_for_wxwork_keys(
                        data, bare_hex_re, db_files, salt_to_dbs,
                        key_map, remaining_salts, base, pid, print,
                    )

                if (reg_idx + 1) % 200 == 0:
                    elapsed = time.time() - t0
                    progress = scanned_bytes / total_bytes * 100 if total_bytes else 100
                    print(
                        f"  [{progress:.1f}%] {len(key_map)}/{len(salt_to_dbs)} salts matched, "
                        f"{all_hex_matches} x'...' patterns, "
                        f"{all_bare_hex_matches} bare hex patterns, {elapsed:.1f}s"
                    )

            if remaining_salts:
                print("\n[*] 未找到 x'...' 形式 key，尝试 WXWork 5.x cipher 结构体扫描...")
                scan_memory_for_wxwork_cipher_structs(
                    h, regions, db_files, salt_to_dbs,
                    key_map, remaining_salts, pid, print,
                )
        finally:
            kernel32.CloseHandle(h)

        if not remaining_salts:
            print(f"\n[+] 所有密钥已找到，跳过剩余进程")
            break

    elapsed = time.time() - t0
    print(
        f"\n扫描完成: {elapsed:.1f}s, {len(pids)} 个进程, "
        f"{all_hex_matches} x'...' 模式, {all_bare_hex_matches} bare hex 模式"
    )

    cross_verify_wxwork_keys(db_files, salt_to_dbs, key_map, print)
    save_wxwork_results(db_files, salt_to_dbs, key_map, db_dir, out_file, print)


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as e:
        print(f"\n[ERROR] {e}")
        sys.exit(1)
