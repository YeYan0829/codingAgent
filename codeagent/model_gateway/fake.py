from __future__ import annotations

import json
from typing import Any

from codeagent.model_gateway.base import BaseModelClient, LLMResponse, LLMToolCall, ModelRequest


class FakeLLM(BaseModelClient):
    """用于本地验证 runtime loop 的确定性假模型。"""

    def complete(self, request: ModelRequest) -> LLMResponse:
        messages = request.messages
        observations = [m for m in messages if m.get("role") == "tool"]
        if not observations:
            return LLMResponse(
                tool_calls=[
                    LLMToolCall(call_id="fake-list-dir-1", name="list_dir", arguments={"path": "."})
                ]
            )

        last = observations[-1]
        tool_name = last.get("name") or self._tool_name_for_call(messages, last.get("tool_call_id"))
        payload = self._parse_content(last.get("content", ""))

        if tool_name == "list_dir":
            content = payload.get("content", "")
            for readme in ("README.md", "README.rst", "README.txt"):
                if readme in content:
                    return LLMResponse(
                        tool_calls=[
                            LLMToolCall(
                                call_id="fake-read-readme-1",
                                name="read_file",
                                arguments={"path": readme, "start_line": 1, "end_line": 80},
                            )
                        ]
                    )
            return LLMResponse(text=self._summarize_without_readme(content))

        if tool_name == "read_file":
            readme = payload.get("content", "")
            top_level = self._previous_list_dir(messages)
            return LLMResponse(text=self._summarize_with_readme(top_level, readme))

        return LLMResponse(text="我已经完成了当前只读探索步骤。")

    def _parse_content(self, content: str) -> dict[str, Any]:
        try:
            data = json.loads(content)
            return data if isinstance(data, dict) else {"content": content}
        except json.JSONDecodeError:
            return {"content": content}

    def _previous_list_dir(self, messages: list[dict[str, Any]]) -> str:
        for msg in reversed(messages):
            if msg.get("role") == "tool" and (msg.get("name") == "list_dir" or self._tool_name_for_call(messages, msg.get("tool_call_id")) == "list_dir"):
                return self._parse_content(msg.get("content", "")).get("content", "")
        return ""

    def _tool_name_for_call(self, messages: list[dict[str, Any]], call_id: str | None) -> str | None:
        if not call_id:
            return None
        for msg in reversed(messages):
            for call in msg.get("tool_calls", []) or []:
                if call.get("id") == call_id:
                    return (call.get("function") or {}).get("name")
        return None

    def _summarize_without_readme(self, listing: str) -> str:
        return (
            "我查看了项目顶层目录，当前没有发现 README 文件。\n\n"
            f"顶层内容包括：\n{listing}\n\n"
            "下一步可以继续查看配置文件、测试目录或主要源码目录，以判断项目结构。"
        )

    def _summarize_with_readme(self, listing: str, readme: str) -> str:
        snippet = readme[:1200]
        return (
            "我完成了一轮只读探索。\n\n"
            f"看到的顶层文件或目录：\n{listing}\n\n"
            f"README 前部大致说明：\n{snippet}\n\n"
            "下一步可以继续探索 pyproject.toml、tests/、docs/ 或核心包目录，确认入口、测试和模块边界。"
        )
