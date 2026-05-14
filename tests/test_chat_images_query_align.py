"""测试 get_chat_images 新增的 offset / start_time / end_time 参数。"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mcp_server


def _img(local_id, create_time, md5=None, size=None):
    info = {'local_id': local_id, 'create_time': create_time, 'md5': md5}
    if size is not None:
        info['size'] = size
    return info


def _run_with(shard_images_map, **kwargs):
    """Helper: stub collaborators and call get_chat_images with new kwargs."""
    shards = [{'db_path': k, 'table_name': 'Msg_x'} for k in shard_images_map]
    captured = {'calls': []}

    def fake_list(db_path, table_name, username, limit=20, start_ts=None, end_ts=None):
        captured['calls'].append({
            'db_path': db_path, 'limit': limit, 'start_ts': start_ts, 'end_ts': end_ts,
        })
        return shard_images_map.get(db_path, [])

    with patch.object(mcp_server, 'resolve_username', return_value='wxid_demo'), \
         patch.object(mcp_server, 'get_contact_names', return_value={'wxid_demo': 'Demo'}), \
         patch.object(mcp_server, '_find_msg_tables_for_user', return_value=shards), \
         patch.object(mcp_server._image_resolver, 'list_chat_images', side_effect=fake_list):
        return mcp_server.get_chat_images('Demo', **kwargs), captured


def test_invalid_offset_returns_error():
    out, _ = _run_with({}, offset=-1)
    assert '错误' in out


def test_invalid_time_range_returns_error():
    """start_time 晚于 end_time 应报错。"""
    out, _ = _run_with({}, start_time='2026-05-10', end_time='2026-05-01')
    assert '错误' in out


def test_candidate_limit_includes_offset():
    """每 shard 拉 limit+offset 张候选, 保证全局分页能切到正确的页。"""
    _, captured = _run_with({'/a': [_img(1, 1000)]}, limit=5, offset=10)
    assert captured['calls'][0]['limit'] == 15


def test_start_end_ts_forwarded_to_shard_query():
    """start_time / end_time 解析为 unix 秒后透传给 shard 查询。"""
    _, captured = _run_with(
        {'/a': []},
        start_time='2026-05-01',
        end_time='2026-05-31',
    )
    call = captured['calls'][0]
    assert call['start_ts'] is not None
    assert call['end_ts'] is not None
    assert call['start_ts'] < call['end_ts']


def test_offset_slices_paged_window():
    """offset=2, limit=2 取全局排序后第 3-4 张图片。"""
    shard_a = [_img(1, 1100), _img(2, 1000)]
    shard_b = [_img(3, 1300), _img(4, 1200)]
    out, _ = _run_with({'/a': shard_a, '/b': shard_b}, limit=2, offset=2)
    # 全局排序后顺序: 1300, 1200, 1100, 1000 → 第 3-4 是 1100, 1000 → local_id 1, 2
    assert 'local_id=1' in out
    assert 'local_id=2' in out
    assert 'local_id=3' not in out
    assert 'local_id=4' not in out


def test_header_shows_time_range_when_given():
    shard_a = [_img(1, 1000, md5='abc')]
    out, _ = _run_with({'/a': shard_a}, start_time='2026-05-01')
    assert '时间范围' in out
    assert '2026-05-01' in out


def test_header_shows_offset_limit():
    shard_a = [_img(1, 1000, md5='abc')]
    out, _ = _run_with({'/a': shard_a}, limit=10, offset=20)
    assert 'offset=20' in out
    assert 'limit=10' in out


def test_default_behavior_unchanged():
    """不传新参数时行为与旧接口一致 — offset=0 切片就是 [:limit]。"""
    shard_a = [_img(1, 1100, md5='a1'), _img(2, 1000, md5='a2')]
    out, _ = _run_with({'/a': shard_a})
    assert 'local_id=1' in out
    assert 'local_id=2' in out
