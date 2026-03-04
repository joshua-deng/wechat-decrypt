from pathlib import Path

from .paths import build_round_dir


def default_round_dir() -> Path:
    return build_round_dir(Path("work/image_coverage"))
