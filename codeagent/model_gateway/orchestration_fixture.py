"""显式选择 fake/c3-fixture 的免费产品验收模型；不用于效果评测。"""
import hashlib
import json
import time

from codeagent.model_gateway.fake import FakeLLM
from codeagent.model_gateway.base import LLMResponse, LLMToolCall


class OrchestrationFixture(FakeLLM):
    def complete(self, request):
        users = [m for m in request.messages if m.get("role") == "user"]
        if request.purpose == "context_condenser":
            return super().complete(request)
        if request.purpose == "readonly_child":
            goal = json.loads(users[-1]["content"])["goal"]
            if goal in {"fixture slow", "演练：等待响应"}:
                time.sleep(120)
            if goal in {"fixture fail", "演练：模拟调查失败"}:
                raise RuntimeError("C3 deterministic child failure")
            anchor = max(i for i, message in enumerate(request.messages) if message.get("role") == "user")
            observations = [m for m in request.messages[anchor + 1:] if m.get("role") == "tool"]
            path = "calculator.py" if "calculator.py" in goal else "verify.py" if "verify.py" in goal else "README.md"
            if not observations:
                call_id = "read-" + hashlib.sha256(json.dumps(users, sort_keys=True).encode()).hexdigest()[:12]
                return LLMResponse(tool_calls=[LLMToolCall(call_id=call_id, name="read_file", arguments={"path": path})])
            result = json.loads(observations[-1]["content"])
            if not result.get("ok"):
                raise RuntimeError("fixture child read failed")
            return LLMResponse(text="已读取 " + path + "：\n" + str(result.get("content") or "")[:1500])
        if not any(t.name == "task" for t in request.tools):
            return super().complete(request)
        # Runtime 将迟到报告作为 user-role 观察投递；它不是新的用户任务，不能改变夹具动作身份。
        def task_message(message):
            try:
                content = json.loads(message["content"])
            except (ValueError, TypeError):
                return True
            return not isinstance(content, dict) or content.get("kind") != "child_report_observation"
        users = [m for m in users if task_message(m)]
        prompt = str(users[-1]["content"])
        prefix = "c3-" + hashlib.sha256(json.dumps(users, sort_keys=True).encode()).hexdigest()[:10] + "-"
        observations = {m["tool_call_id"]: json.loads(m["content"]) for m in request.messages if m.get("role") == "tool"}

        def observed(name):
            return observations.get(prefix + name)

        def value(name):
            result = observed(name)
            if not result or not result.get("ok"):
                raise RuntimeError("fixture command failed: " + name)
            return json.loads(result["content"])

        def call(name, tool, args, text=None):
            return LLMResponse(text=text, tool_calls=[LLMToolCall(call_id=prefix + name, name=tool, arguments=args)])

        if not observed("spawn"):
            goal = "演练：等待响应" if "stop" in prompt else "演练：模拟调查失败" if "failure" in prompt else "确认 README 中的验证入口"
            return call("spawn", "task", {"subagent_type": "review", "prompt": goal},
                "我先核对项目说明，再检查实现与验证方式。" if "stop" not in prompt else "我先启动调查任务，你可以在它等待响应时点击 Stop。")
        first = json.loads(observed("spawn")["content"])
        if first["status"] != "completed":
            return LLMResponse(text="调查未完成，已记录这项限制。" if "tolerate" in prompt else "调查子任务执行失败，本次调查未完成。")
        if not observed("parent-read"):
            return call("parent-read", "list_dir", {})
        if not observed("follow"):
            return call("follow", "task", {"resume": first["task_id"],
                "prompt": "核对 verify.py 中的正常与零计数用例"},
                "接下来检查实现，并让子任务核对验证脚本。")
        follow = value("follow")
        if "edit" in prompt:
            if not observed("read-code"):
                return call("read-code", "read_file", {"path": "calculator.py"})
            if not observed("edit"):
                read = observed("read-code")
                if not read["ok"]:
                    return LLMResponse(text="未找到演示文件 calculator.py，请在准备好的演示目录中运行。")
                return call("edit", "apply_workspace_edit", {"operations": [{"op": "replace_text", "path": "calculator.py",
                    "expected_sha256": read["metadata"]["sha256"], "old_text": "    return total / count",
                    "new_text": "    return 0 if count == 0 else total / count"}]},
                    "我会在计数为零时返回 0，保留正常除法行为。")
            if not observed("edit").get("ok"):
                return LLMResponse(text="未能修改 calculator.py，错误已保留在工作记录中。")
            if not observed("validate"):
                return call("validate", "run_command", {"command": "python3 verify.py", "purpose": "validation"},
                            "修改已写入当前项目。现在运行 `python3 verify.py`。")
            if not observed("validate").get("ok"):
                return LLMResponse(text="已生成修改，但验证未通过。")
        if not observed("refresh"):
            return call("refresh", "task", {"resume": first["task_id"],
                "prompt": "读取当前 calculator.py，复核零计数分支"},
                "验证命令已通过，再让子任务核对修改后的实现。" if "edit" in prompt else "再核对当前版本的实现。")
        if "edit" in prompt:
            if not observed("edit").get("ok"):
                return LLMResponse(text="未能修改 calculator.py，错误已保留在工作记录中。")
            if not observed("validate").get("ok"):
                return LLMResponse(text="已生成修改，但验证未通过。")
            return LLMResponse(text="已修复除零情况，`python3 verify.py` 验证通过。")
        return LLMResponse(text="已读取项目说明。")
