"""测试分页提示语 _pagination_hint() 的边界行为。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mcp_server


def test_no_hint_when_count_less_than_limit():
    """count < limit 表示已读完当前条件下全部结果，不提示。"""
    assert mcp_server._pagination_hint(count=10, limit=50, offset=0) == ""


def test_hint_when_count_equals_limit():
    """count == limit 时无法判断是否还有更多，提示下一页 offset。"""
    hint = mcp_server._pagination_hint(count=50, limit=50, offset=0)
    assert "可能还有更多" in hint
    assert "offset=50" in hint


def test_hint_advances_offset_by_limit():
    """连续翻页时 offset 累加。"""
    hint = mcp_server._pagination_hint(count=20, limit=20, offset=100)
    assert "offset=120" in hint


def test_no_hint_when_limit_zero():
    """limit=0 是非法分页 (上游有 _validate_pagination 兜底)；防御性返回空。"""
    assert mcp_server._pagination_hint(count=0, limit=0, offset=0) == ""


def test_no_hint_when_count_exceeds_limit():
    """理论上 count > limit 不该发生 (调用方已 limit), 但若发生仍要提示。"""
    hint = mcp_server._pagination_hint(count=51, limit=50, offset=0)
    assert "可能还有更多" in hint
