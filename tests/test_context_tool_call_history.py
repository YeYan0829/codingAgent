from codeagent.context.builder import ContextBuilder
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
