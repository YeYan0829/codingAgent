from codeagent.context.builder import ContextBuilder
from codeagent.context.projector import project_context_events
from codeagent.session.store import SessionStore


def test_context_rebuilds_assistant_tool_call_and_tool_result_pair(tmp_path):
    store = SessionStore(tmp_path).create()
    store.append_event("user_message", {"message": "inspect"})
    store.append_event("assistant_tool_calls", {"tool_calls": [{"call_id": "call_1", "name": "list_dir", "arguments": {"path": "."}}]})
    store.append_event("tool_result", {"call_id": "call_1", "name": "list_dir", "content": '{"ok": true, "content": "README.md"}'})

    messages = ContextBuilder(store).build()

    assistant = next(message for message in messages if message.get("role") == "assistant" and message.get("tool_calls"))
    tool = next(message for message in messages if message.get("role") == "tool")
    assert assistant["tool_calls"][0]["id"] == "call_1"
    assert assistant["tool_calls"][0]["function"]["name"] == "list_dir"
    assert tool["tool_call_id"] == "call_1"


def test_context_keeps_reasoning_with_latest_mandatory_tool_atom(tmp_path):
    store = SessionStore(tmp_path).create()
    store.append_event("user_message", {"message": "inspect"})
    store.append_event("assistant_tool_calls", {
        "message": "read it", "reasoning_content": "reason exactly",
        "tool_calls": [{"call_id": "call_1", "name": "list_dir", "arguments": {"path": "."}}],
    })
    store.append_event("tool_result", {
        "call_id": "call_1", "name": "list_dir", "content": '{"ok": true, "content": "README.md"}',
    })

    result = ContextBuilder(store).manager.build()
    messages = result.messages

    assistant = next(message for message in messages if message.get("tool_calls"))
    assert assistant["content"] == "read it"
    assert assistant["reasoning_content"] == "reason exactly"
    assert messages[messages.index(assistant) + 1]["tool_call_id"] == "call_1"


def test_context_keeps_completed_tool_history_raw_until_trigger(tmp_path):
    store = SessionStore(tmp_path).create()
    store.append_event("user_message", {"message": "运行精确命令 --required-flag"})
    for index in range(25):
        call_id = f"old-{index}"
        store.append_event("assistant_tool_calls", {"tool_calls": [{"call_id": call_id, "name": "read_file", "arguments": {"path": f"file-{index}.py"}}]})
        store.append_event("tool_result", {"call_id": call_id, "name": "read_file", "content": '{"ok": true}'})
    store.append_event("assistant_message", {"message": "上一轮尚未完成验证。"})
    store.append_event("user_message", {"message": "继续并使用我指定的命令"})

    messages = ContextBuilder(store).build()

    user_messages = [message["content"] for message in messages if message["role"] == "user"]
    assert user_messages == ["运行精确命令 --required-flag", "继续并使用我指定的命令"]
    assert sum(bool(message.get("tool_calls")) for message in messages) == 25
    assert len(project_context_events(store.read_events()).items) == 53
