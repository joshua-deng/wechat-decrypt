import pytest

from tools.image_coverage.generate_open_tasks import build_open_tasks


def test_build_open_tasks_ranks_highest_first() -> None:
    rows = [
        {"chat_name": "low-priority", "focus_months": "2026-01", "count": 1},
        {"chat_name": "top-priority", "focus_months": "2025-11,2025-12", "count": 9},
        {"chat_name": "mid-priority", "focus_months": "2026-02", "count": 4},
    ]

    markdown = build_open_tasks(rows)
    task_lines = [line for line in markdown.splitlines() if line.startswith("- [ ]")]

    assert task_lines == [
        "- [ ] chat_name: top-priority | focus_months: 2025-11,2025-12 | count: 9",
        "- [ ] chat_name: mid-priority | focus_months: 2026-02 | count: 4",
        "- [ ] chat_name: low-priority | focus_months: 2026-01 | count: 1",
    ]
    assert all("chat_name:" in line and "focus_months:" in line for line in task_lines)


def test_build_open_tasks_tie_breaks_by_chat_name_when_count_equal() -> None:
    rows = [
        {"chat_name": "charlie", "focus_months": "2026-03", "count": 5},
        {"chat_name": "alpha", "focus_months": "2026-01", "count": 5},
        {"chat_name": "bravo", "focus_months": "2026-02", "count": 5},
    ]

    markdown = build_open_tasks(rows)
    task_lines = [line for line in markdown.splitlines() if line.startswith("- [ ]")]

    assert task_lines == [
        "- [ ] chat_name: alpha | focus_months: 2026-01 | count: 5",
        "- [ ] chat_name: bravo | focus_months: 2026-02 | count: 5",
        "- [ ] chat_name: charlie | focus_months: 2026-03 | count: 5",
    ]


def test_build_open_tasks_raises_value_error_for_invalid_count() -> None:
    rows = [{"chat_name": "broken", "focus_months": "2026-01", "count": "oops"}]

    with pytest.raises(ValueError, match="broken"):
        build_open_tasks(rows)


def test_build_open_tasks_renders_none_fields_as_empty_string() -> None:
    rows = [
        {"chat_name": None, "focus_months": None, "count": 1},
        {"count": 0},
    ]

    markdown = build_open_tasks(rows)
    task_lines = [line for line in markdown.splitlines() if line.startswith("- [ ]")]

    assert task_lines == [
        "- [ ] chat_name:  | focus_months:  | count: 1",
        "- [ ] chat_name:  | focus_months:  | count: 0",
    ]
