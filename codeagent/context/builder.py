from __future__ import annotations

import json
from importlib.resources import files

from codeagent.session.store import SessionStore


class ContextBuilder:
    def __init__(self, session_store: SessionStore, max_events: int = 40) -> None:
        self.session_store = session_store
        self.max_events = max_events

    def build(self) -> list[dict]:
        messages = [
            {"role": "system", "content": self._read_prompt("system.md")},
            {"role": "system", "content": self._read_prompt("tool_policy.md")},
        ]
        known_tool_call_ids: set[str] = set()
        for event in self.session_store.read_events()[-self.max_events :]:
            if event.type == "user_message":
                messages.append({"role": "user", "content": event.payload.get("message", "")})
            elif event.type == "assistant_message":
                messages.append({"role": "assistant", "content": event.payload.get("message", "")})
            elif event.type == "assistant_tool_calls":
                tool_calls = []
                for call in event.payload.get("tool_calls", []):
                    call_id = call.get("call_id", "")
                    known_tool_call_ids.add(call_id)
                    tool_calls.append(
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": call.get("name", ""),
                                "arguments": json.dumps(call.get("arguments", {}), ensure_ascii=False),
                            },
                        }
                    )
                messages.append(
                    {
                        "role": "assistant",
                        "content": event.payload.get("message") or None,
                        "tool_calls": tool_calls,
                    }
                )
            elif event.type == "tool_result":
                call_id = event.payload.get("call_id", "")
                if call_id not in known_tool_call_ids:
                    continue
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": event.payload.get("content") or json.dumps(event.payload.get("result", {}), ensure_ascii=False),
                    }
                )
        return messages

    def _read_prompt(self, name: str) -> str:
        return files("codeagent.prompts").joinpath(name).read_text(encoding="utf-8")
