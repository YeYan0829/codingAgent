"""核对真正送给模型的定义与处理器，不用未接入的参考资料代替接口验收。"""

from codeagent.tools.atomic_edit import build_edit_file_tool
from codeagent.tools.fs_read import build_fs_tools
from codeagent.workspace.workspace import Workspace
from test_atomic_edit import operation_service




def test_edit_prompt_is_loaded_by_the_live_tool(tmp_path):
    _, _, context, edits = operation_service(tmp_path)
    tool = build_edit_file_tool(edits).to_model_tool()
    assert 'old_text' in tool.description and '唯一定位' in tool.description
    result = edits.edit_file({'path': 'sort_utils.py', 'edits': [{'old_text': 'reverse=True', 'new_text': 'reverse=False'}]})
    assert result.ok and 'reverse=False' in (context.active_root / 'sort_utils.py').read_text()


def test_move_without_hash_allows_case_only_rename(tmp_path):
    _, _, context, edits = operation_service(tmp_path)
    result = edits.apply_workspace_edit({'operations': [{'op': 'move_file', 'source': 'sort_utils.py', 'destination': 'Sort_utils.py'}]})
    assert result.ok, result
    assert not (context.active_root / 'sort_utils.py').exists()
    assert (context.active_root / 'Sort_utils.py').exists()


def test_explicit_hash_still_detects_conflicting_file_content(tmp_path):
    _, _, context, edits = operation_service(tmp_path)
    before = (context.active_root / 'sort_utils.py').read_bytes()
    result = edits.apply_workspace_edit({'operations': [{'op': 'delete_file', 'path': 'sort_utils.py', 'expected_sha256': '0' * 64}]})
    assert not result.ok and result.error_code == 'edit_conflict'
    assert (context.active_root / 'sort_utils.py').read_bytes() == before


def test_read_large_range_returns_next_page_instead_of_rejecting_investigation(tmp_path):
    (tmp_path / 'large.py').write_text(''.join(f'line {i}\n' for i in range(1002)))
    read = next(t for t in build_fs_tools(Workspace(tmp_path).context) if t.name == 'read_file')
    first = read.handler({'path': 'large.py', 'end_line': 100000})
    assert first.ok and first.truncated
    assert first.metadata['returned_range'] == {'start_line': 1, 'end_line': 1000}
    assert first.metadata['next_start_line'] == 1001
    second = read.handler({'path': 'large.py', 'start_line': first.metadata['next_start_line']})
    assert second.ok and not second.metadata['has_more_after'] and second.metadata['next_start_line'] is None
    (tmp_path / 'empty.py').touch()
    empty = read.handler({'path': 'empty.py'})
    assert empty.metadata['returned_range'] is None and empty.metadata['next_start_line'] is None
