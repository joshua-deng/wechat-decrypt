from datetime import datetime
from pathlib import Path

from tools.image_coverage.paths import build_round_dir


def test_build_round_dir_format() -> None:
    root_dir = Path("work/image_coverage")
    round_start = datetime(2026, 3, 4, 9, 5)

    assert build_round_dir(root_dir, now=round_start) == root_dir / "round-20260304-0905"
