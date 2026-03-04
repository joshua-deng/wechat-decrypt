from collections.abc import Iterable, Mapping
from typing import Any


def _parse_count(row: Mapping[str, Any]) -> int:
    value = row.get("count")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid count in row: {row!r}") from None


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def build_open_tasks(rows: Iterable[Mapping[str, Any]]) -> str:
    normalized_rows: list[tuple[int, str, str]] = []
    for row in rows:
        count = _parse_count(row)
        chat_name = _as_text(row.get("chat_name", ""))
        focus_months = _as_text(row.get("focus_months", ""))
        normalized_rows.append((count, chat_name, focus_months))

    normalized_rows.sort(key=lambda item: (-item[0], item[1]))

    lines = [
        "# Open Tasks",
        "",
        "Prioritized by count (high to low).",
        "",
    ]

    for count, chat_name, focus_months in normalized_rows:
        lines.append(
            f"- [ ] chat_name: {chat_name} | focus_months: {focus_months} | count: {count}"
        )

    return "\n".join(lines)
