"""macOS WeChat 4.x 图片 AES key 派生（无需读运行进程）。

通过 macOS 微信 4.x 在磁盘上的命名约定派生出 V2 .dat 图片解密所需的
(xor_key, aes_key)。解决 issue #23：macOS 用户无法用 C 版扫描器从运行
进程读取出有效的访问凭据（197K 候选全部失败）。

派生算法（共享核心）
--------------------
- xor_key = uin & 0xFF
- aes_key = MD5(str(uin) + cleaned_wxid).hex()[:16]   # ASCII 字符串
- 用 V2 _t.dat 文件 [0xF:0x1F] 16 字节做模板验证：派生出的 aes_key 把
  密文 AES-128-ECB 解出图像 magic（JPEG / PNG / GIF / WebP / wxgf）即视为命中
- 为防短 magic 偶然命中，要求多个不同模板都通过验证才视为成功

uin 来源（两条路径，dispatcher 自动 fallback）
----------------------------------------------
方案1（kvcomm 缓存文件名，主路径）：
  读 ~/.../app_data/net/kvcomm/key_<uin>_*.statistic 提 uin。
  优点：~毫秒级；缺点：依赖缓存文件，多账号下可能歧义。

方案2（wxid 后缀候选搜索，fallback 路径）：
  关键洞察：wxid 目录后 4 位 hex == md5(str(uin))[:4]。
  流程：从 V2 .dat 末字节投票反推 xor_key (假设 JPG EOI = 0xD9) →
  枚举 (uin & 0xff == xor_key) 的 2^24 个候选 → md5 前缀匹配
  得 ~256 个 uin 候选 → AES 模板验证唯一定位。
  优点：不依赖 kvcomm，多账号无歧义；缺点：~7 秒（单核 2^24 MD5）。

命中后写回 config.json 的 image_aes_key / image_xor_key，monitor_web.py
启动时自动加载，图片消息显示内联预览。

致谢
----
- 方案1（kvcomm 派生）算法源自 @hicccc77 在 issue #23 的评论，参考实现
  位于 https://github.com/hicccc77/WeFlow （CC BY-NC-SA 4.0）。
- 方案2（wxid 后缀候选搜索）思路源自 @H3CoF6 在 issue #68 的评论，
  提供了 "wxid 后 4 位 == md5(uin)[:4]" 这一关键结构性洞察。

本模块是独立的 Python 实现，未复制任何上游 TypeScript / C 源码；函数
边界与变量命名沿用算法的自然结构（regex / MD5 调用顺序 / magic 字节表
等不可避免地相同）。

用法
----
  python find_image_key_macos.py
"""
import hashlib
import json
import multiprocessing
import os
import platform
import queue as _queue
import re
import sys
import time
from collections import Counter

from Crypto.Cipher import AES

# V2 .dat 文件 magic（与 decode_image.py 中 V2_MAGIC_FULL 一致）
V2_MAGIC = bytes.fromhex("070856320807")

# kvcomm 文件名格式：key_<code>_<其他段>.statistic
# code 必须紧跟在 "key_" 之后（不能是 "key_reportnow_..." 这种带前缀的）
_KVCOMM_FILENAME_RE = re.compile(r"^key_(\d+)_.+\.statistic$", re.IGNORECASE)

# AES 解密结果允许的图像 magic
_IMAGE_MAGICS = (
    b"\xff\xd8\xff",      # JPEG
    b"\x89\x50\x4e\x47",  # PNG
    b"GIF",               # GIF
    b"RIFF",              # WebP container（首块只能看前 16B，全检需 [8:12]==b"WEBP"）
    b"wxgf",              # 微信 HEVC GIF / Live Photo
)


def normalize_wxid(account_id):
    """归一化账号 ID。

    - wxid_<seg> 形式：保留 wxid_<seg>，丢弃后续下划线分段
    - <base>_<4 alnum> 形式：丢弃 _<4 alnum> 后缀（macOS 路径目录名常见）
    - 其他：原样返回
    """
    aid = (account_id or "").strip()
    if not aid:
        return ""
    if aid.lower().startswith("wxid_"):
        m = re.match(r"^(wxid_[^_]+)", aid, re.IGNORECASE)
        return m.group(1) if m else aid
    m = re.match(r"^(.+)_([a-zA-Z0-9]{4})$", aid)
    return m.group(1) if m else aid


def derive_image_keys(code, wxid):
    """从 (code, wxid) 派生 (xor_key, aes_key_ascii)。

    aes_key_ascii 是 16 字符 hex 字符串；调用方按 ASCII 编码取前 16 字节作为
    AES-128 密钥。本函数不做 wxid 归一化（由调用方枚举原值与归一化值）。
    """
    xor_key = int(code) & 0xFF
    aes_key = hashlib.md5(f"{code}{wxid}".encode("utf-8")).hexdigest()[:16]
    return xor_key, aes_key


def derive_kvcomm_dir_candidates(db_dir):
    """从 db_dir 推算所有可能的 kvcomm 缓存目录（按优先级排序）。

    微信 4.x 在不同版本 / 安装方式下 kvcomm 路径不固定，需要枚举多个候选。
    返回的列表里至少有一项被 os.path.isdir 确认存在时才算可用。
    """
    parts = db_dir.rstrip(os.sep).split(os.sep)
    candidates = []
    if "xwechat_files" in parts:
        idx = parts.index("xwechat_files")
        documents_root = os.sep.join(parts[:idx])
        # 1) 与 xwechat_files 兄弟目录的 app_data
        candidates.append(os.path.join(documents_root, "app_data", "net", "kvcomm"))
        # 2) 旧版可能放 xwechat 子目录
        candidates.append(os.path.join(documents_root, "xwechat", "net", "kvcomm"))
        # 3) 容器内 Application Support 路径（部分版本）
        if idx >= 1:
            container_root = os.sep.join(parts[:idx - 1])  # Documents 之上
            candidates.append(os.path.join(
                container_root, "Library", "Application Support",
                "com.tencent.xinWeChat", "xwechat", "net", "kvcomm"))
            candidates.append(os.path.join(
                container_root, "Library", "Application Support",
                "com.tencent.xinWeChat", "net", "kvcomm"))
    # 4) 兜底：HOME 下默认沙盒路径
    home = os.path.expanduser("~")
    candidates.append(os.path.join(
        home, "Library", "Containers", "com.tencent.xinWeChat", "Data",
        "Documents", "app_data", "net", "kvcomm"))
    # 去重，保留顺序
    seen = set()
    deduped = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            deduped.append(c)
    return deduped


def find_existing_kvcomm_dir(db_dir):
    """从候选路径中返回第一个存在的 kvcomm 目录；都不存在返回 None。"""
    for candidate in derive_kvcomm_dir_candidates(db_dir):
        if os.path.isdir(candidate):
            return candidate
    return None


def collect_kvcomm_codes(kvcomm_dir):
    """扫 kvcomm 目录，返回去重排序的 code 列表。"""
    if not kvcomm_dir or not os.path.isdir(kvcomm_dir):
        return []
    codes = set()
    try:
        names = os.listdir(kvcomm_dir)
    except OSError:
        return []
    for name in names:
        m = _KVCOMM_FILENAME_RE.match(name)
        if not m:
            continue
        try:
            code = int(m.group(1))
        except ValueError:
            continue
        if 0 < code <= 0xFFFFFFFF:
            codes.add(code)
    return sorted(codes)


def collect_wxid_candidates(db_dir):
    """从 db_dir 提取候选 wxid（含原值和归一化值）。"""
    parts = db_dir.rstrip(os.sep).split(os.sep)
    if "xwechat_files" not in parts:
        return []
    idx = parts.index("xwechat_files")
    if idx + 1 >= len(parts):
        return []
    raw = parts[idx + 1]
    candidates = [raw]
    normalized = normalize_wxid(raw)
    if normalized and normalized != raw:
        candidates.append(normalized)
    return candidates


def find_v2_template_ciphertexts(attach_dir, max_templates=3, max_files=64):
    """在 attach_dir 下找 V2 .dat 文件的模板密文（[0xF:0x1F] 16 字节）。

    优先 _t.dat（缩略图小、读得快），找不到再降级用任意 .dat。
    返回最多 max_templates 个**不同**的密文，用于交叉验证防止短 magic 偶然命中。
    """
    if not attach_dir or not os.path.isdir(attach_dir):
        return []

    def _scan(suffix):
        # 出口条件只看是否凑够 max_templates 个**不同**密文；不因为
        # examined 达到 max_files 提前退出 —— 否则若前 64 个文件都是同一
        # 张图的副本，结果只有 1 个 template，交叉验证就退化成单模板。
        out, seen = [], set()
        examined = 0
        for root, _, files in os.walk(attach_dir):
            for f in files:
                if not f.endswith(suffix):
                    continue
                examined += 1
                try:
                    with open(os.path.join(root, f), "rb") as fp:
                        data = fp.read(0x20)
                except OSError:
                    continue
                if len(data) >= 0x1F and data[:6] == V2_MAGIC:
                    ct = data[0xF:0x1F]
                    if ct not in seen:
                        seen.add(ct)
                        out.append(ct)
                        if len(out) >= max_templates:
                            return out
                # 兜底：扫了 max_files 个文件还凑不齐 max_templates 个不同的，
                # 提前停止以免在巨型 attach 目录里跑很久（只在 out 不空时才能停）
                if examined >= max_files and out:
                    return out
        return out

    return _scan("_t.dat") or _scan(".dat")


def verify_aes_key(aes_key_ascii, template_ct):
    """AES-128-ECB 解 template_ct（16 字节），检查头部是否是图像 magic。"""
    if not aes_key_ascii or not template_ct or len(template_ct) != 16:
        return False
    key_bytes = aes_key_ascii.encode("ascii", errors="ignore")[:16]
    if len(key_bytes) < 16:
        return False
    try:
        cipher = AES.new(key_bytes, AES.MODE_ECB)
        decrypted = cipher.decrypt(template_ct)
    except (ValueError, KeyError):
        return False
    return any(decrypted.startswith(m) for m in _IMAGE_MAGICS)


def verify_aes_key_against_all(aes_key_ascii, templates):
    """在多个模板上交叉验证 aes_key。全部通过才算命中（防短 magic 偶然碰撞）。"""
    if not templates:
        return False
    return all(verify_aes_key(aes_key_ascii, ct) for ct in templates)


# ---------- 方案2 (wxid 后缀候选搜索, fallback) ---------- #

# md5 hex 后缀只可能是 [0-9a-f]; 严格匹配避免误吃非 hex 字符的 wxid 后缀
# (microsoft 改方案 / 异常路径) 后悄悄返回空候选误导用户。
_WXID_HEX_SUFFIX_RE = re.compile(r"^(.+)_([0-9a-fA-F]{4})$")


def extract_wxid_parts(db_dir):
    """从 db_dir 提取 (wxid_full, wxid_norm, suffix)。

    db_dir 形如 .../xwechat_files/<wxid>_<4hex>/db_storage
    返回 ('your_wxid_a1b2', 'your_wxid', 'a1b2') 或 None（不匹配 _<4 hex> 后缀）。

    suffix 是 4 位小写 hex（macOS 路径目录名固定格式 = md5(str(uin))[:4]），
    用作方案2 中候选搜索的 md5 前缀目标。
    """
    wxid_candidates = collect_wxid_candidates(db_dir)
    if not wxid_candidates:
        return None
    wxid_full = wxid_candidates[0]  # raw 总是第一个
    m = _WXID_HEX_SUFFIX_RE.match(wxid_full)
    if not m:
        return None
    return wxid_full, m.group(1), m.group(2).lower()


def derive_xor_key_from_v2_dat(attach_dir, sample=10, min_samples=3):
    """扫多个 V2 .dat 末字节投票反推 xor_key（假设 JPG EOI = 0xD9）。

    macOS 缩略图 _t.dat 几乎都是 JPG，末字节 = 0xD9 ^ xor_key 反推稳定。
    投票多数一致才信；分歧大说明假设破灭（不全是 JPG）。

    Args:
        attach_dir: 微信 attach 目录
        sample: 扫到 N 个 V2 .dat 即停止（性能上限）
        min_samples: 至少 N 个样本才视为"投票可信"。低于此返回 None,
            避免 1-2 个样本时一旦撞到非 JPG 就 lock 错 xor_key。
    Returns:
        (xor_key, votes, total) 或 None (样本不足 / 找不到 V2 .dat)。
        votes < total 时调用方应警告 (假设可能破灭)。
    """
    if not attach_dir or not os.path.isdir(attach_dir):
        return None
    last_bytes = []
    for root, _, files in os.walk(attach_dir):
        for f in files:
            if not f.endswith(".dat"):
                continue
            path = os.path.join(root, f)
            try:
                if os.path.getsize(path) < 0x20:
                    continue
                with open(path, "rb") as fp:
                    head = fp.read(6)
                    if head != V2_MAGIC:
                        continue
                    fp.seek(-1, 2)
                    last = fp.read(1)[0]
                last_bytes.append(last ^ 0xD9)
                if len(last_bytes) >= sample:
                    break
            except OSError:
                continue
        if len(last_bytes) >= sample:
            break
    if len(last_bytes) < min_samples:
        return None
    top, votes = Counter(last_bytes).most_common(1)[0]
    return top, votes, len(last_bytes)


def bruteforce_uin_candidates(xor_key, wxid_suffix):
    """枚举 0~2^32 中 (uin & 0xff == xor_key) 且 md5(str(uin))[:4] == suffix 的 uin。

    单核 ~7-8 秒（2^24 = 16M MD5）。期望命中数 ~256（2^24 / 16^4）。

    注意 uin 上限假设为 2^32（4 字节无符号整数）。函数命名沿用密码学
    候选搜索的 brute-force 术语；中文 prose 用 "枚举 / 候选搜索" 表述。

    本函数是单进程 + hex 比较版本, 主要用作算法金标准 (测试) 与
    parallel 路径不可用时的 fallback。生产 dispatcher 走 parallel
    版本 (见 `_bruteforce_with_aes_parallel`)。
    """
    target = wxid_suffix.lower()
    out = []
    for uin in range(xor_key, 2 ** 32, 256):
        if hashlib.md5(str(uin).encode()).hexdigest()[:4] == target:
            out.append(uin)
    return out


def _aes_template_match(aes_bytes, ciphertext):
    """worker 进程内: AES-128-ECB 解 ciphertext 并检查图像 magic。

    放模块顶层是为了 multiprocessing pickle (worker 函数必须可 import).
    比 verify_aes_key 更紧凑 (省去 try-except 默认通过短路) — 在百万次
    调用循环里这点开销有意义。
    """
    try:
        decrypted = AES.new(aes_bytes, AES.MODE_ECB).decrypt(ciphertext)
    except (ValueError, KeyError):
        return False
    return any(decrypted.startswith(m) for m in _IMAGE_MAGICS)


def _bruteforce_worker_chunk(start, end, xor_key, suffix_bytes, wxid_bytes,
                              templates, result_queue):
    """worker: 扫候选区间, 命中 (md5 前缀 + 全模板 AES) 推入 queue 即返回。

    内联做 md5 + AES 验证 (不分两 pass) 让早停在 worker 内有效。
    suffix 用 binary 比 (digest()[:2] vs hexdigest()[:4]), 节省 hex 转换。
    """
    for i in range(start, end):
        uin = (i << 8) | xor_key
        uin_bytes = str(uin).encode("ascii")
        if hashlib.md5(uin_bytes).digest()[:2] == suffix_bytes:
            aes_hex = hashlib.md5(uin_bytes + wxid_bytes).hexdigest()[:16]
            aes_bytes = aes_hex.encode("ascii")
            if all(_aes_template_match(aes_bytes, ct) for ct in templates):
                result_queue.put((uin, aes_hex))
                return


def _bruteforce_with_aes_parallel(xor_key, suffix_hex, wxid_norm, templates,
                                   workers=None, timeout=60):
    """方案2 多进程实现 — 加速思路借鉴自 @H3CoF6 PR #69.

    与单进程版本的差异:
    - cpu_count 个 worker 并行扫 0~2^32 候选 (~5-8x 加速)
    - 二进制 md5 digest()[:2] 替代 hexdigest()[:4] (省 hex 转换)
    - 内联多模板 AES 验证 (无两 pass; PR #69 是单模板, 本实现保留多模板
      交叉验证防短 magic 偶然命中)
    - 任一 worker 命中即推 queue, 主进程 terminate 其他 (早停)

    Returns:
        (uin, aes_key_hex) 或 None (timeout / 全 worker 跑完未命中)
    """
    suffix_bytes = bytes.fromhex(suffix_hex)
    wxid_bytes = wxid_norm.encode("ascii")
    if workers is None:
        workers = max(1, multiprocessing.cpu_count())
    total = 1 << 24
    chunk = total // workers

    queue = multiprocessing.Queue()
    procs = []
    for i in range(workers):
        start_i = i * chunk
        end_i = (i + 1) * chunk if i != workers - 1 else total
        p = multiprocessing.Process(
            target=_bruteforce_worker_chunk,
            args=(start_i, end_i, xor_key, suffix_bytes, wxid_bytes,
                  templates, queue),
            daemon=True,
        )
        p.start()
        procs.append(p)

    found = None
    deadline = time.time() + timeout
    try:
        while any(p.is_alive() for p in procs) and time.time() < deadline:
            try:
                found = queue.get(timeout=0.1)
                break
            except _queue.Empty:
                continue
        # 所有 worker 死亡后 queue 仍可能有最后入队的数据
        if not found:
            try:
                found = queue.get_nowait()
            except _queue.Empty:
                pass
    finally:
        for p in procs:
            if p.is_alive():
                p.terminate()
        for p in procs:
            p.join(timeout=1)
    return found


# ---------- Dispatcher + 两条路径 ---------- #

def _find_via_kvcomm(db_dir, templates):
    """方案1：从 kvcomm 缓存文件名提 uin 候选。

    要求：~/.../app_data/net/kvcomm/key_<uin>_*.statistic 存在。
    返回 (xor_key, aes_key) 或 None（kvcomm 缺失 / 无 code / wxid 提不出 /
    所有组合都验证失败）。
    """
    kvcomm_dir = find_existing_kvcomm_dir(db_dir)
    if not kvcomm_dir:
        print("[!] 方案1: 找不到 kvcomm 缓存目录，已尝试以下候选:", flush=True)
        for c in derive_kvcomm_dir_candidates(db_dir):
            print(f"      {c}", flush=True)
        return None
    print(f"[+] 方案1: 使用 kvcomm 目录 {kvcomm_dir}", flush=True)

    codes = collect_kvcomm_codes(kvcomm_dir)
    if not codes:
        print("[!] 方案1: kvcomm 目录无 key_*.statistic 文件", flush=True)
        return None
    print(f"[+] 方案1: 找到 {len(codes)} 个 uin 候选", flush=True)

    wxid_candidates = collect_wxid_candidates(db_dir)
    if not wxid_candidates:
        print("[!] 方案1: 无法从 db_dir 提取 wxid", flush=True)
        return None
    print(f"[+] 方案1: wxid 候选 {wxid_candidates}", flush=True)

    # 穷举顺序：wxid 外、uin 内。多账号系统下当前账号的所有 uin 优先尝试。
    for wxid in wxid_candidates:
        for code in codes:
            xor_key, aes_key = derive_image_keys(code, wxid)
            if verify_aes_key_against_all(aes_key, templates):
                print()
                print("[OK] 方案1 验证成功（所有模板均通过）:", flush=True)
                print(f"    uin      = {code}", flush=True)
                print(f"    wxid     = {wxid}", flush=True)
                print(f"    xor_key  = 0x{xor_key:02x}", flush=True)
                print(f"    aes_key  = {aes_key}", flush=True)
                return xor_key, aes_key

    print("[!] 方案1: 所有 (wxid × uin) 组合都未通过交叉验证", flush=True)
    return None


def _find_via_bruteforce(db_dir, attach_dir, templates):
    """方案2 (fallback)：从 wxid 后缀候选搜索 uin（不依赖 kvcomm）。

    流程：wxid 后缀 + V2 .dat 末字节投票反推 xor_key → 枚举 2^24 个 uin
    候选 → 用 templates 跑 AES 验证唯一定位。
    """
    parts = extract_wxid_parts(db_dir)
    if not parts:
        print("[!] 方案2: wxid 路径不含 _<4 hex> 后缀，无法应用方案2", flush=True)
        return None
    wxid_full, wxid_norm, suffix = parts
    print(f"[+] 方案2: wxid_full={wxid_full}, suffix={suffix}", flush=True)

    xres = derive_xor_key_from_v2_dat(attach_dir)
    if not xres:
        print("[!] 方案2: V2 .dat 样本不足 (需 >= 3 个), 无法投票反推 xor_key",
              flush=True)
        print("    请先在微信中再看 1-2 张图片，让微信生成更多 V2 .dat 文件",
              flush=True)
        return None
    xor_key, votes, total = xres
    if votes == total:
        print(f"[+] 方案2: xor_key=0x{xor_key:02x} ({votes}/{total} 一致, 假设 JPG)",
              flush=True)
    else:
        print(f"[!] 方案2: xor_key 投票分歧 {votes}/{total}, 取多数 0x{xor_key:02x} "
              f"(可能 attach 不全是 JPG)", flush=True)

    workers = max(1, multiprocessing.cpu_count())
    print(f"[*] 方案2: 多进程枚举 (workers={workers}, 预计 ~1-2 秒)...",
          flush=True)

    # 同时试 wxid_full 和 wxid_norm（normalize_wxid 可能去掉后缀）
    wxid_tries = [wxid_norm]
    if wxid_full != wxid_norm:
        wxid_tries.append(wxid_full)

    t0 = time.time()
    for wxid_try in wxid_tries:
        result = _bruteforce_with_aes_parallel(
            xor_key, suffix, wxid_try, templates, workers=workers
        )
        if result:
            uin, aes_key = result
            elapsed = time.time() - t0
            print()
            print(f"[OK] 方案2 (fallback) 验证成功 (耗时 {elapsed:.1f}s):",
                  flush=True)
            print(f"    uin      = {uin}", flush=True)
            print(f"    wxid     = {wxid_try}", flush=True)
            print(f"    xor_key  = 0x{xor_key:02x}", flush=True)
            print(f"    aes_key  = {aes_key}", flush=True)
            return xor_key, aes_key

    elapsed = time.time() - t0
    print(f"[!] 方案2: 所有 uin 候选都未通过 AES 验证 (耗时 {elapsed:.1f}s)",
          flush=True)
    return None


def find_image_key_macos(db_dir):
    """在 macOS 上派生并交叉验证 V2 图片密钥。

    Dispatcher：先尝试方案1 (kvcomm)，失败 fallback 到方案2 (候选搜索)。
    两条路径都需要 V2 .dat 模板做 AES 验证 — 模板缺失就直接失败。

    Returns:
        (xor_key, aes_key_ascii) on success；失败返回 None 并打印诊断信息。
    """
    base_dir = os.path.dirname(db_dir)  # 去掉 db_storage
    attach_dir = os.path.join(base_dir, "msg", "attach")
    templates = find_v2_template_ciphertexts(attach_dir)
    if not templates:
        print(f"[!] 在 {attach_dir} 下找不到 V2 模板文件", flush=True)
        print("    请先在微信中查看 1-2 张图片，让微信生成 V2 .dat 文件",
              flush=True)
        return None
    print(f"[+] 找到 {len(templates)} 个不同模板用于交叉验证", flush=True)

    # 方案1 (主路径): kvcomm 缓存
    result = _find_via_kvcomm(db_dir, templates)
    if result is not None:
        return result

    # 方案2 (fallback): wxid 后缀候选搜索
    print()
    print("[*] 方案1 失败, 尝试方案2 (wxid 后缀候选搜索, fallback)", flush=True)
    return _find_via_bruteforce(db_dir, attach_dir, templates)


def _save_config_atomic(config_path, config):
    """原子写 config.json：tmp + os.replace 防止中断留下半截文件。

    若 json.dump 或 os.replace 抛错，向上抛出（让 main 给出 stacktrace
    而不是默默写坏 config）；同时清理可能残留的 .tmp 文件。
    """
    tmp_path = config_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, config_path)
    finally:
        # 失败路径上 .tmp 可能残留；成功路径上 os.replace 已经把 tmp 移走了
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def main(config_path=None):
    """CLI 入口。`config_path` 默认是脚本同目录下的 config.json，
    暴露此参数主要为方便单元测试注入隔离的临时配置。"""
    if platform.system().lower() != "darwin":
        print("此脚本只在 macOS 上工作。其他平台请用 find_image_key.py（内存扫描）。",
              file=sys.stderr, flush=True)
        sys.exit(1)

    if config_path is None:
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "config.json")
    try:
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[!] 读取 {config_path} 失败: {e}", file=sys.stderr, flush=True)
        sys.exit(1)

    db_dir = config.get("db_dir", "")
    if not db_dir:
        print("[!] config.json 中未配置 db_dir", file=sys.stderr, flush=True)
        sys.exit(1)
    db_dir = os.path.expanduser(os.path.expandvars(db_dir))
    print(f"[*] db_dir = {db_dir}", flush=True)

    # 短路：如果已有 image_aes_key 且仍能在所有模板上验证通过，直接退出
    # （沿用 find_image_key.py 的 UX 约定，避免无谓重写 config.json）
    existing_aes = config.get("image_aes_key")
    if existing_aes:
        base_dir = os.path.dirname(db_dir)
        attach_dir = os.path.join(base_dir, "msg", "attach")
        templates = find_v2_template_ciphertexts(attach_dir)
        if templates and verify_aes_key_against_all(existing_aes, templates):
            print(f"[+] 已有 image_aes_key={existing_aes} 在 "
                  f"{len(templates)} 个模板上仍然有效，无需重新派生", flush=True)
            return

    result = find_image_key_macos(db_dir)
    if result is None:
        sys.exit(1)

    xor_key, aes_key = result
    config["image_aes_key"] = aes_key
    config["image_xor_key"] = xor_key
    _save_config_atomic(config_path, config)
    print()
    print(f"[+] 已写入 {config_path}", flush=True)
    print("    下次启动 monitor_web.py 时会自动加载新密钥，图片消息显示内联预览",
          flush=True)


if __name__ == "__main__":
    main()
