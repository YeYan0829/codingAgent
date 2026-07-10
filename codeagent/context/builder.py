from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

from codeagent.session.store import SessionStore


class ContextBuilder:
    def __init__(self, session_store: SessionStore, max_events: int = 40) -> None:
        self.session_store = session_store
        self.max_events = max_events

    def build(self, observations: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        messages = [
            {"role": "system", "content": self._read_prompt("system.md")},
            {"role": "system", "content": self._read_prompt("tool_policy.md")},
        ]
        for event in self.session_store.read_events()[-self.max_events :]:
            if event.type == "user_message":
                messages.append({"role": "user", "content": event.payload.get("message", "")})
            elif event.type == "assistant_message":
                messages.append({"role": "assistant", "content": event.payload.get("message", "")})
        for observation in observations or []:
            messages.append(
                {
                    "role": "tool",
                    "name": observation["name"],
                    "tool_call_id": observation["call_id"],
                    "content": json.dumps(observation["result"], ensure_ascii=False),
                }
            )
        return messages

    def _read_prompt(self, name: str) -> str:
        return files("codeagent.prompts").joinpath(name).read_text(encoding="utf-8")
