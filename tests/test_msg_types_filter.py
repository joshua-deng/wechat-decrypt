"""测试 _resolve_msg_types 和 _build_message_filters 的 type_filter 路径。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mcp_server


def test_resolve_none_returns_no_filter():
    assert mcp_server._resolve_msg_types(None) == (None, None)


def test_resolve_empty_returns_no_filter():
    assert mcp_server._resolve_msg_types([]) == (None, None)


def test_resolve_single_text_type():
    type_filter, err = mcp_server._resolve_msg_types(['text'])
    assert err is None
    assert type_filter == [1]


def test_resolve_multiple_types():
    type_filter, err = mcp_server._resolve_msg_types(['image', 'voice', 'video'])
    assert err is None
    assert sorted(type_filter) == [3, 34, 43]


def test_file_alias_maps_to_app():
    """'file' 是常见叫法, 实际是 type=49 (app message)。"""
    type_filter, err = mcp_server._resolve_msg_types(['file'])
    assert err is None
    assert type_filter == [49]


def test_case_insensitive_and_strip():
    type_filter, err = mcp_server._resolve_msg_types(['  Text  ', 'IMAGE'])
    assert err is None
    assert sorted(type_filter) == [1, 3]


def test_unknown_type_returns_error():
    type_filter, err = mcp_server._resolve_msg_types(['unknown'])
    assert type_filter is None
    assert err is not None
    assert 'unknown' in err
    assert 'text' in err  # 错误提示列出可选值


def test_partial_unknown_aborts_whole():
    """混入一个未知类型时整体失败, 不偷偷过滤合法的。"""
    type_filter, err = mcp_server._resolve_msg_types(['text', 'invalid_type'])
    assert type_filter is None
    assert 'invalid_type' in err


def test_build_filters_without_type_filter():
    """type_filter=None 时 SQL 不包含 local_type 子句。"""
    clauses, params = mcp_server._build_message_filters()
    assert not any('local_type' in c for c in clauses)


def test_build_filters_with_single_type():
    clauses, params = mcp_server._build_message_filters(type_filter=[1])
    assert any('local_type IN (?)' == c for c in clauses)
    assert 1 in params


def test_build_filters_with_multiple_types():
    clauses, params = mcp_server._build_message_filters(type_filter=[1, 3, 34])
    type_clause = [c for c in clauses if 'local_type' in c][0]
    assert type_clause == 'local_type IN (?,?,?)'
    assert params == [1, 3, 34]


def test_build_filters_combines_with_time_and_keyword():
    clauses, params = mcp_server._build_message_filters(
        start_ts=1000, end_ts=2000, keyword='hello', type_filter=[1]
    )
    assert 'create_time >= ?' in clauses
    assert 'create_time <= ?' in clauses
    assert 'message_content LIKE ?' in clauses
    assert any('local_type' in c for c in clauses)
    assert params == [1000, 2000, '%hello%', 1]
