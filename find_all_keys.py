import functools
import platform
import sys


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
            "macOS 请先运行 C 版扫描器提取密钥：\n"
            "\n"
            "    sudo ./find_all_keys_macos\n"
            "\n"
            "    完成后再运行 python main.py decrypt"
        )
    raise RuntimeError(
        f"当前平台暂不支持通过 find_all_keys.py 提取密钥: {platform.system()}"
    )


def get_pids():
    return _load_impl().get_pids()


def main():
    return _load_impl().main()


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(f"\n[ERROR] {exc}")
        sys.exit(1)
