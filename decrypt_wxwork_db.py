"""
Decrypt WXWork databases encrypted with wxSQLite3 AES-128-CBC.

This handles the database page format. A 16-byte raw key is still required,
either from wxwork_keys.json or via --key.
"""
import argparse
import json
import os
import shutil
import sys

from key_utils import get_key_info, strip_key_metadata
from wxwork_crypto import (
    decrypt_wxwork_database,
    is_plain_sqlite_page,
    is_wxsqlite3_aes128_page1,
    verify_sqlite_file,
    verify_wxsqlite3_aes128_key,
)


def _app_paths():
    from config import _app_base_dir, _config_file_path

    return _app_base_dir(), _config_file_path()


def _load_config():
    base, config_file = _app_paths()
    cfg = {}
    if os.path.exists(config_file):
        with open(config_file, encoding="utf-8") as f:
            cfg = json.load(f)

    db_dir = cfg.get("wxwork_db_dir", "")
    if not db_dir or not os.path.isdir(db_dir):
        from find_wxwork_keys import auto_detect_wxwork_db_dir

        detected = auto_detect_wxwork_db_dir()
        if detected:
            db_dir = detected
        else:
            raise RuntimeError("wxwork_db_dir is not configured")

    keys_file = cfg.get("wxwork_keys_file", "wxwork_keys.json")
    if not os.path.isabs(keys_file):
        keys_file = os.path.join(base, keys_file)

    out_dir = cfg.get("wxwork_decrypted_dir", "wxwork_decrypted")
    if not os.path.isabs(out_dir):
        out_dir = os.path.join(base, out_dir)

    return {
        "db_dir": db_dir,
        "keys_file": keys_file,
        "out_dir": out_dir,
        "global_key": cfg.get("wxwork_db_key", ""),
    }


def _parse_key_hex(value):
    value = (value or "").strip()
    if value.startswith("x'") and value.endswith("'"):
        value = value[2:-1]
    if len(value) != 32:
        raise ValueError("WXWork wxSQLite3 AES-128 key must be 32 hex chars")
    return bytes.fromhex(value)


def _load_keys(keys_file):
    if not os.path.exists(keys_file):
        return {}
    with open(keys_file, encoding="utf-8") as f:
        return strip_key_metadata(json.load(f))


def _iter_db_files(db_dir):
    for root, dirs, files in os.walk(db_dir):
        dirs[:] = [d for d in dirs if d not in ("-journal",)]
        for name in files:
            if not name.endswith(".db") or name.endswith("-wal") or name.endswith("-shm"):
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, db_dir)
            yield rel, path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Decrypt WXWork wxSQLite3 AES-128 databases")
    parser.add_argument("--key", help="16-byte raw key as 32 hex chars")
    args = parser.parse_args(argv)

    cfg = _load_config()
    db_dir = cfg["db_dir"]
    out_dir = cfg["out_dir"]
    keys_file = cfg["keys_file"]
    keys = _load_keys(keys_file)

    global_key = None
    key_arg = args.key or cfg.get("global_key")
    if key_arg:
        global_key = _parse_key_hex(key_arg)

    print("=" * 60)
    print("  WXWork Database Decryptor")
    print("=" * 60)
    print(f"DB dir: {db_dir}")
    print(f"Output: {out_dir}")
    if keys:
        print(f"Loaded {len(keys)} per-DB keys from {keys_file}")
    elif global_key:
        print("Using global key from argument/config")
    else:
        print(f"No key available. Run find_wxwork_keys.py or pass --key.")
        return 1

    os.makedirs(out_dir, exist_ok=True)

    success = 0
    copied = 0
    failed = 0
    for rel, path in sorted(_iter_db_files(db_dir)):
        out_path = os.path.join(out_dir, rel)
        with open(path, "rb") as f:
            page1 = f.read(4096)

        if is_plain_sqlite_page(page1):
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            shutil.copy2(path, out_path)
            copied += 1
            print(f"COPY: {rel} (plain SQLite)")
            continue

        if not is_wxsqlite3_aes128_page1(page1):
            failed += 1
            print(f"SKIP: {rel} (unknown encrypted format)")
            continue

        key = global_key
        key_info = get_key_info(keys, rel) if keys else None
        if key_info:
            try:
                key = _parse_key_hex(key_info["enc_key"])
            except (KeyError, ValueError) as exc:
                failed += 1
                print(f"FAIL: {rel} (bad key entry: {exc})")
                continue

        if key is None:
            failed += 1
            print(f"SKIP: {rel} (no key)")
            continue

        if not verify_wxsqlite3_aes128_key(key, page1):
            failed += 1
            print(f"FAIL: {rel} (key validation failed)")
            continue

        try:
            decrypt_wxwork_database(path, out_path, key)
            tables = verify_sqlite_file(out_path)
            success += 1
            table_preview = ", ".join(tables[:5])
            suffix = f" tables: {table_preview}" if table_preview else " no tables"
            print(f"OK: {rel} ({suffix})")
        except Exception as exc:
            failed += 1
            print(f"FAIL: {rel} ({exc})")

    print(f"\nResult: {success} decrypted, {copied} copied, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
