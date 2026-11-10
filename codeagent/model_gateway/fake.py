from __future__ import annotations

import json
from typing import Any

from codeagent.model_gateway.base import BaseModelClient, LLMResponse, LLMToolCall, ModelRequest


class FakeLLM(BaseModelClient):
    """用于本地验证 runtime loop 的确定性假模型。"""

    def complete(self, request: ModelRequest) -> LLMResponse:
        if request.purpose == "context_condenser":
            source = request.messages[-1].get("content", "") if request.messages else ""
            return LLMResponse(
                text=("LAST_KNOWN_STATE:\n已压缩较旧的可见执行历史。\n"
                      "IMPORTANT_EVIDENCE:\n" + str(source)[-1200:] + "\nPENDING:\n继续当前任务。"),
                finish_reason="stop",
            )
        messages = request.messages
        observations = [m for m in messages if m.get("role") == "tool"]
        if (request.purpose == "main_agent" and any(t.name == "task" for t in request.tools)
                and any("委派" in str(m.get("content", "")) for m in messages if m.get("role") == "user")):
            if not observations:
                return LLMResponse(tool_calls=[LLMToolCall(call_id="fake-task", name="task", arguments={
                    "subagent_type": "explore", "prompt": "读取项目README并报告路径依据。",
                })])
            if len(observations) == 1:
                result = self._parse_content(observations[0]["content"])
                report = self._parse_content(result.get("content", "{}"))
                if report.get("task_id"):
                    return LLMResponse(tool_calls=[LLMToolCall(call_id="fake-task-resume", name="task", arguments={
                        "resume": report["task_id"], "prompt": "基于你自己的历史，再读取README核实证据。",
                    })])
            if len(observations) == 2:
                return LLMResponse(tool_calls=[LLMToolCall(call_id="fake-task-parent-read", name="list_dir", arguments={})])
            return LLMResponse(text="免费工程夹具：同步委派、同一子会话续问，主Agent收到结果后继续读取并汇总。", finish_reason="stop")
        if (request.purpose == "main_agent" and any(t.name == "spawn_agent" for t in request.tools)
                and any("委派" in str(m.get("content", "")) for m in messages if m.get("role") == "user")):
            if not observations:
                return LLMResponse(tool_calls=[LLMToolCall(call_id="graph-spawn", name="spawn_agent", arguments={
                    "spec": "explore", "goal": "读取项目 README 并给出证据", "done_when": "有路径依据",
                    "request_key": "fake-graph-spawn-v1",
                })])
            if len(observations) == 1:
                report = self._parse_content(self._parse_content(observations[0]["content"]).get("content", "{}"))
                if "agent_id" in report:
                    return LLMResponse(tool_calls=[LLMToolCall(call_id="graph-send", name="send_agent", arguments={
                        "agent_id": report["agent_id"], "expected_last_run_id": report["run_id"],
                        "message": "基于你自己的历史，再用工具核实 README", "done_when": "重新读取并引用证据",
                        "revision_mode": "pinned", "request_key": "fake-graph-send-v1",
                    })])
            if len(observations) == 2:
                return LLMResponse(tool_calls=[LLMToolCall(call_id="graph-parent-read", name="list_dir", arguments={})])
            return LLMResponse(text="免费工程夹具：主图动态委派、同一子会话第二 run 继续工具循环，主 Agent 读取后汇总。", finish_reason="stop")
        if request.purpose == "readonly_child":
            # 当前 run 必须继续真实工具调查，旧 run 的工具结果保留在输入中但不代替本次执行。
            anchor_index = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
            observations = [m for m in messages[anchor_index + 1:] if m.get("role") == "tool"]
        # 免费、由当前请求决定的委派工程夹具；不代表真实模型任务质量。
        if (request.purpose == "main_agent" and any(t.name == "delegate_readonly" for t in request.tools)
                and any("委派" in str(m.get("content", "")) for m in messages if m.get("role") == "user")):
            delegated = next((m for m in observations if self._tool_name_for_call(messages, m.get("tool_call_id"))
                              == "delegate_readonly"), None)
            if delegated is None:
                return LLMResponse(tool_calls=[LLMToolCall(
                    call_id="fake-delegate", name="delegate_readonly", arguments={
                        "request_key": "fake-readonly-v1", "tasks": [
                            {"goal": "定位项目入口", "done_when": "读取 README 并给出路径证据"},
                            {"goal": "独立审查项目说明", "done_when": "读取 README 并报告可验证信息与限制"},
                        ],
                    },
                )])
            if not any(self._tool_name_for_call(messages, m.get("tool_call_id")) == "list_dir" for m in observations):
                return LLMResponse(tool_calls=[LLMToolCall(call_id="fake-after-delegate", name="list_dir", arguments={})])
            return LLMResponse(text="免费工程夹具：已取得两个只读子 Agent 的结果，主 Agent 继续调用工具后汇总。\n"
                               + delegated.get("content", "")[:26000])
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
