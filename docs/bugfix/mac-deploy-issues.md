# macOS 部署问题记录

> 环境：macOS (Apple Silicon / Intel), 微信 4.x, Python 3.14 (Homebrew)
> 日期：2026-05-12

---

## 问题 1: `task_for_pid failed: 5` — 微信进程内存读取被拒绝

### 现象

```bash
sudo ./find_all_keys_macos
# 输出:
# WeChat PID: 12276
# task_for_pid failed: 5
# Make sure: (1) running as root, (2) WeChat is ad-hoc signed
```

以 root 运行扫描器，但仍无法读取微信进程内存。

### 原因

微信 App 使用了 Apple **Hardened Runtime**（`flags=0x10000(runtime)`），即使以 root 身份运行，macOS 也会阻止对带有此标志的进程进行 `task_for_pid` 调用。

验证方法：

```bash
codesign -dvvv /Applications/WeChat.app 2>&1 | grep flags
# 输出: flags=0x10000(runtime)  ← 问题所在
```

### 修复

1. **退出微信**（重签名需要进程不在运行）

   ```bash
   killall WeChat
   ```

2. **执行 ad-hoc 重签名**（移除 Hardened Runtime 标志）

   ```bash
   sudo codesign --force --deep --sign - /Applications/WeChat.app
   ```

3. **验证签名已变更**

   ```bash
   codesign -dvvv /Applications/WeChat.app 2>&1 | grep -E "flags|Authority"
   # 正确输出应类似: flags=0x2
   # 不应再出现 flags=0x10000(runtime) 或 Authority=Developer ID
   ```

4. **重新打开微信并登录**，再运行扫描器

   ```bash
   sudo ./find_all_keys_macos
   ```

### 注意事项

- 微信**每次更新**后签名会恢复为原始状态，需重新执行上述步骤
- `--deep` 参数确保签名覆盖 App Bundle 内所有嵌套二进制文件
- 重签名后必须重启微信，否则进程仍使用旧的签名凭证

---

## 问题 2: 自动检测微信数据目录失败

### 现象

```bash
.venv/bin/python3 decrypt_db.py
# 输出:
# [!] 未能自动检测微信数据目录
#     请手动编辑 config.json 中的 db_dir 字段
```

或

```bash
.venv/bin/python3 main.py
# 输出:
# [!] 未能自动检测微信数据目录
```

### 原因

`config.py` 中的 `auto_detect_db_dir()` 函数仅实现了 Windows 和 Linux 的自动检测逻辑，macOS 分支直接返回 `None`：

```python
def auto_detect_db_dir():
    if _SYSTEM == "windows":
        return _auto_detect_db_dir_windows()
    if _SYSTEM == "linux":
        return _auto_detect_db_dir_linux()
    return None  # ← macOS 未实现
```

macOS 微信数据目录位于 `~/Library/Containers/com.tencent.xinWeChat/...`，路径中包含随机 hash，需要搜索才能定位。

### 修复

已在 `config.py` 中实现 macOS 自动检测，同时改进了检测失败时的提示信息。

#### 代码改动

1. **新增 `_auto_detect_db_dir_macos()` 函数**（`config.py`）

   ```python
   def _auto_detect_db_dir_macos():
       """自动检测 macOS 微信 db_storage 路径。"""
       base = os.path.expanduser(
           "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
       )
       if not os.path.isdir(base):
           return None

       seen = set()
       candidates = []
       pattern = os.path.join(base, "*", "db_storage")
       for match in glob.glob(pattern):
           normalized = os.path.normcase(os.path.normpath(match))
           if os.path.isdir(match) and normalized not in seen:
               seen.add(normalized)
               candidates.append(match)

       # 优先使用最近活跃账号：按 message 目录 mtime 降序
       def _mtime(path):
           msg_dir = os.path.join(path, "message")
           target = msg_dir if os.path.isdir(msg_dir) else path
           try:
               return os.path.getmtime(target)
           except OSError:
               return 0

       candidates.sort(key=_mtime, reverse=True)
       return _choose_candidate(candidates)
   ```

2. **在 `auto_detect_db_dir()` 中接入 macOS 分支**

   ```python
   def auto_detect_db_dir():
       if _SYSTEM == "windows":
           return _auto_detect_db_dir_windows()
       if _SYSTEM == "linux":
           return _auto_detect_db_dir_linux()
       if _SYSTEM == "darwin":
           return _auto_detect_db_dir_macos()  # ← 新增
       return None
   ```

3. **改进检测失败时的提示**：macOS 提示正确的默认路径格式

   ```
   [!] 未能自动检测微信数据目录
       请手动编辑 config.json 中的 db_dir 字段
       macOS 默认路径类似: ~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<wxid>/db_storage
   ```

#### 临时解决方案（如自动检测仍失败）

手动查找并配置 `db_dir`：

```bash
find ~/Library/Containers/com.tencent.xinWeChat -type d -name "db_storage" 2>/dev/null
```

如有多个账号，按修改时间判断当前活跃账号：

```bash
stat -f "%m %N" /path/to/account1/db_storage /path/to/account2/db_storage
```

然后编辑 `config.json` 填入路径。

---

## 问题 3: Homebrew Python 拒绝全局 pip 安装

### 现象

```bash
pip3 install -r requirements.txt
# 报错: error: externally-managed-environment
# 提示: PEP 668 — 不能直接向系统 Python 安装包
```

### 原因

Homebrew 的 Python 3.14 遵循 [PEP 668](https://peps.python.org/pep-0668/)，禁止 `pip install` 直接写入系统 Python 环境，防止破坏包管理器的依赖关系。

### 修复

使用虚拟环境：

```bash
cd /Users/drulu/Documents/GitHub/wechat-decrypt

# 创建虚拟环境
python3 -m venv .venv

# 激活并安装依赖
source .venv/bin/activate
pip install -r requirements.txt

# 后续运行脚本时使用 .venv 中的 Python
.venv/bin/python3 main.py
.venv/bin/python3 decrypt_db.py
```

或使用 Makefile（已配置 `.venv/bin/python3`）：

```bash
make decrypt  # 等价于 .venv/bin/python3 main.py decrypt
make web      # 等价于 .venv/bin/python3 main.py
```

---

## 完整部署流程（macOS）

将以上修复整合为正确的部署顺序：

```bash
# 1. 安装 Xcode CLI 工具
xcode-select --install

# 2. 创建虚拟环境并安装依赖
cd ~/Documents/GitHub/wechat-decrypt
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. 退出微信 → 重签名 → 重启微信
killall WeChat
sudo codesign --force --deep --sign - /Applications/WeChat.app
# 然后手动打开微信并登录

# 4. 编译 C 扫描器
cc -O2 -o find_all_keys_macos find_all_keys_macos.c -framework Foundation

# 5. 提取密钥
sudo ./find_all_keys_macos

# 6. 启动 Web UI（db_dir 已自动检测，无需手动配置）
.venv/bin/python3 main.py          # 启动 Web UI → http://localhost:5678
.venv/bin/python3 decrypt_db.py    # 或仅全量解密
```

## 修复状态汇总

| 问题 | 代码修复 | 说明 |
|------|---------|------|
| `task_for_pid failed: 5` | ❌ 无法代码修复 | 系统级限制，需手动重签名微信 |
| 自动检测 `db_dir` 失败 | ✅ 已修复 | `config.py` 新增 `_auto_detect_db_dir_macos()`，自动搜索 `~/Library/Containers/` |
| Homebrew Python 拒绝 pip 安装 | ❌ 无法代码修复 | 环境限制，需使用虚拟环境 |
