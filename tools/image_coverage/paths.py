from datetime import datetime
from pathlib import Path


def build_round_dir(root_dir: Path, *, now: datetime | None = None) -> Path:
    round_start = now or datetime.now()
    return root_dir / f"round-{round_start.strftime('%Y%m%d-%H%M')}"
