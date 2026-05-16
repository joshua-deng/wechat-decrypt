import hashlib
import os
import sqlite3
import struct

from Crypto.Cipher import AES


PAGE_SZ = 4096
SQLITE_HDR = b"SQLite format 3\x00"
WXSQLITE3_SALT = b"sAlT"


def _modmult(a, b, c, m, s):
    q = s // a
    s = b * (s - a * q) - c * q
    if s < 0:
        s += m
    return s


def generate_initial_vector(page_no):
    """Match SQLite3MultipleCiphers sqlite3mcGenerateInitialVector()."""
    z = page_no + 1
    initkey = bytearray(16)
    for idx in range(4):
        z = _modmult(52774, 40692, 3791, 2147483399, z)
        initkey[idx * 4 : idx * 4 + 4] = struct.pack("<I", z & 0xFFFFFFFF)
    return hashlib.md5(initkey).digest()


def derive_wxsqlite3_aes128_page_key(raw_key, page_no):
    """Derive the per-page AES-128 key used by wxSQLite3 AES-128-CBC."""
    if len(raw_key) != 16:
        raise ValueError("wxSQLite3 AES-128 raw key must be 16 bytes")
    material = raw_key + struct.pack("<I", page_no) + WXSQLITE3_SALT
    return hashlib.md5(material).digest()


def is_plain_sqlite_page(page):
    return page[: len(SQLITE_HDR)] == SQLITE_HDR


def has_wxsqlite3_plain_header_fragment(page):
    """New wxSQLite3 AES mode keeps SQLite header bytes 16..23 in plaintext."""
    if len(page) < 24:
        return False
    header = page[16:24]
    page_size = (header[0] << 8) | header[1]
    if page_size == 1:
        page_size = 65536
    return (
        page_size >= 512
        and page_size <= 65536
        and (page_size & (page_size - 1)) == 0
        and header[5] == 0x40
        and header[6] == 0x20
        and header[7] == 0x20
    )


def is_wxsqlite3_aes128_page1(page):
    return not is_plain_sqlite_page(page) and has_wxsqlite3_plain_header_fragment(page)


def _decrypt_aes128_cbc(raw_key, page_no, data):
    page_key = derive_wxsqlite3_aes128_page_key(raw_key, page_no)
    iv = generate_initial_vector(page_no)
    return AES.new(page_key, AES.MODE_CBC, iv).decrypt(data)


def decrypt_wxsqlite3_aes128_page(raw_key, page_data, page_no):
    """Decrypt one wxSQLite3 AES-128-CBC page to a normal SQLite page."""
    if len(page_data) != PAGE_SZ:
        raise ValueError(f"page must be exactly {PAGE_SZ} bytes")

    data = bytearray(page_data)
    if page_no == 1 and has_wxsqlite3_plain_header_fragment(data):
        db_header_fragment = bytes(data[16:24])
        data[16:24] = data[8:16]
        decrypted_tail = _decrypt_aes128_cbc(raw_key, page_no, bytes(data[16:]))
        data[16:] = decrypted_tail
        if bytes(data[16:24]) != db_header_fragment:
            raise ValueError("wxSQLite3 AES-128 key validation failed")
        data[:16] = SQLITE_HDR
        return bytes(data)

    return _decrypt_aes128_cbc(raw_key, page_no, bytes(data))


def looks_like_sqlite_page1(page):
    if page[: len(SQLITE_HDR)] != SQLITE_HDR:
        return False
    if len(page) < 108:
        return False
    btree_page_type = page[100]
    return btree_page_type in (0x02, 0x05, 0x0A, 0x0D)


def verify_wxsqlite3_aes128_key(raw_key, page1):
    if len(raw_key) != 16 or len(page1) < PAGE_SZ:
        return False
    try:
        decrypted = decrypt_wxsqlite3_aes128_page(raw_key, page1[:PAGE_SZ], 1)
    except (ValueError, KeyError):
        return False
    return looks_like_sqlite_page1(decrypted)


def decrypt_wxwork_database(db_path, out_path, raw_key):
    size = os.path.getsize(db_path)
    total_pages = (size + PAGE_SZ - 1) // PAGE_SZ
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    with open(db_path, "rb") as fin, open(out_path, "wb") as fout:
        for page_no in range(1, total_pages + 1):
            page = fin.read(PAGE_SZ)
            if not page:
                break
            if len(page) < PAGE_SZ:
                page += b"\x00" * (PAGE_SZ - len(page))
            fout.write(decrypt_wxsqlite3_aes128_page(raw_key, page, page_no))


def verify_sqlite_file(path):
    conn = sqlite3.connect(path)
    try:
        return [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()]
    finally:
        conn.close()
