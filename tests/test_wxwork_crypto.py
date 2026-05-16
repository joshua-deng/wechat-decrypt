import os

from Crypto.Cipher import AES

from wxwork_crypto import (
    PAGE_SZ,
    SQLITE_HDR,
    decrypt_wxsqlite3_aes128_page,
    derive_wxsqlite3_aes128_page_key,
    generate_initial_vector,
    is_wxsqlite3_aes128_page1,
    verify_wxsqlite3_aes128_key,
)


def _encrypt_block(raw_key, page_no, data):
    page_key = derive_wxsqlite3_aes128_page_key(raw_key, page_no)
    iv = generate_initial_vector(page_no)
    return AES.new(page_key, AES.MODE_CBC, iv).encrypt(data)


def _encrypt_page1_new_scheme(raw_key, plain_page):
    data = bytearray(plain_page)
    db_header = bytes(data[16:24])
    data[:16] = _encrypt_block(raw_key, 1, bytes(data[:16]))
    data[16:] = _encrypt_block(raw_key, 1, bytes(data[16:]))
    data[8:16] = data[16:24]
    data[16:24] = db_header
    return bytes(data)


def _plain_sqlite_page1():
    page = bytearray(PAGE_SZ)
    page[:16] = SQLITE_HDR
    page[16:24] = bytes.fromhex("1000020200402020")
    page[100] = 0x0D
    return bytes(page)


def test_wxsqlite3_aes128_page1_roundtrip():
    raw_key = bytes.fromhex("00112233445566778899aabbccddeeff")
    plain = _plain_sqlite_page1()
    encrypted = _encrypt_page1_new_scheme(raw_key, plain)

    assert is_wxsqlite3_aes128_page1(encrypted)
    assert verify_wxsqlite3_aes128_key(raw_key, encrypted)
    assert not verify_wxsqlite3_aes128_key(os.urandom(16), encrypted)
    assert decrypt_wxsqlite3_aes128_page(raw_key, encrypted, 1) == plain
