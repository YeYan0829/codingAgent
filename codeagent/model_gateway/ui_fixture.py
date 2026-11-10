"""显式选择 fake/ui-fixture 的免费 UI 样本模型；所有工具仍走普通 Runtime。"""
from __future__ import annotations

import hashlib
import json
import time

from codeagent.model_gateway.base import LLMResponse, LLMToolCall
from codeagent.model_gateway.fake import FakeLLM


UI_EDITS = {
    "src/orders/totals.py": ("    return total / count", "    return 0 if count == 0 else total / count"),
    "tests/test_totals.py": (
        "    # UI_DEMO_EMPTY_CASE",
        "    def test_empty_orders(self):\n        self.assertEqual(average_order(120, 0), 0)",
    ),
    "docs/order-rules.md": ("订单数必须大于零。", "订单数为零时返回 0，其他情况保持正常除法行为。"),
}


class UIAcceptanceFixture(FakeLLM):
    def complete(self, request):
        if request.purpose == "context_condenser":
            return super().complete(request)
        users = [m for m in request.messages if m.get("role") == "user"]
        anchor = max((i for i, m in enumerate(request.messages) if m.get("role") == "user"), default=-1)
        observations = {m["tool_call_id"]: json.loads(m["content"])
                        for m in request.messages[anchor + 1:] if m.get("role") == "tool"}
        content = str(users[-1]["content"]) if users else ""
        if request.purpose in {"readonly_child", "worker_child"}:
            goal = json.loads(content)["goal"]
            return self._child(goal, observations)
        prefix = "ui-" + hashlib.sha256(json.dumps(users, sort_keys=True).encode()).hexdigest()[:12] + "-"

        def call(name, tool, arguments, text=None):
            return self._call(prefix + name, tool, arguments, text)

        def observed(name):
            return observations.get(prefix + name)

        def task(name, role, goal, description):
            return call(name, "task", {"subagent_type": role, "prompt": goal, "description": description},
                        "UI 免费演练：" + description + "。主 Agent 将等待子任务返回。")

        if "ui:" in content:
            if not observed("demo-marker"):
                return call("demo-marker", "read_file", {"path": "UI_DEMO.json"})
            try:
                marker = json.loads(observed("demo-marker").get("content") or "{}")
            except ValueError:
                marker = {}
            if not observed("demo-marker").get("ok") or marker.get("kind") != "codeagent-ui-demo":
                return LLMResponse(text="当前项目缺少 UI_DEMO.json 验收标记，未启动演练工具。请打开生成的 demo 仓库。")

        if "ui:markdown" in content:
            return LLMResponse(text=MARKDOWN_SAMPLE)
        if "ui:wait" in content:
            if not observed("wait"):
                return task("wait", "code-explorer", "ui-child:wait", "等待子模型响应（可点击 Stop）")
            return LLMResponse(text="等待演练已结束；请以实际子任务状态和回收记录为准。")
        if "ui:approval" in content:
            if not observed("approval"):
                return task("approval", "bash-runner", "ui-child:approval", "一次性权限请求（命令不访问网络）")
            return self._report(observed("approval"), "批准流程已结束")
        if "ui:failure" in content:
            if not observed("command-failure"):
                return task("command-failure", "bash-runner", "ui-child:command-failure", "执行预期失败的离线检查")
            if not observed("model-failure"):
                return task("model-failure", "code-explorer", "ui-child:model-failure", "演练子模型故障")
            return LLMResponse(text="## 演练失败记录\n\n离线检查故意以退出码 2 结束，子模型故障也已保留。"
                               "这两个场景用于检查错误、输出和子任务未完成状态，没有执行修复。")
        if "ui:edit" in content:
            if not observed("explore"):
                return task("explore", "code-explorer", "ui-child:explore", "定位订单计算与测试入口")
            if not observed("implement"):
                return task("implement", "general-purpose", "ui-child:edit", "修复空订单并同步测试与说明")
            if not observed("check"):
                return task("check", "bash-runner", "ui-child:check", "运行离线验证脚本")
            if not observed("review"):
                report = json.loads(observed("explore").get("content") or "{}")
                if report.get("task_id"):
                    return call("review", "task", {"resume": report["task_id"], "prompt": "ui-child:review",
                                                   "description": "复核当前项目中的修复"})
            return self._report(observed("implement"), "已完成多文件修改演练", observed("check"))
        if "ui:explore" in content or "ui:followup" in content:
            if not observed("explore"):
                return task("explore", "code-explorer", "ui-child:explore", "定位订单模块与测试")
            if not observed("followup"):
                report = json.loads(observed("explore").get("content") or "{}")
                if report.get("task_id"):
                    return call("followup", "task", {"resume": report["task_id"], "prompt": "ui-child:review",
                        "description": "保留子会话历史，继续核对订单实现"})
            return LLMResponse(text="已完成只读探索及同一子会话续问。\n\n"
                               "- 实现：`src/orders/totals.py`\n- 验证：`python3 verify.py`\n"
                               "- 规则：`docs/order-rules.md`\n\n没有执行文件编辑。")
        return LLMResponse(text="这是免费的 UI 验收模型。可发送 `ui:explore`、`ui:edit`、`ui:markdown`、"
                           "`ui:failure`、`ui:wait` 或 `ui:approval`。不会调用外部模型。")

    @staticmethod
    def _call(name, tool, arguments, text=None):
        return LLMResponse(text=text, tool_calls=[LLMToolCall(call_id=name, name=tool, arguments=arguments)])

    def _child(self, goal, observations):
        key = goal.removeprefix("ui-child:")
        if key == "wait":
            # Runtime 在子进程中请求模型；正常 Stop 会取消该等待并回收子进程。
            time.sleep(300)
            return LLMResponse(text="演练等待自然结束。")
        if key == "model-failure":
            raise RuntimeError("UI demo: deterministic child model failure")
        if key in {"check", "command-failure", "approval"}:
            if "ui-child-command" not in observations:
                arguments = {"command": "python3 tools/fail_check.py" if key == "command-failure"
                             else "python3 tools/permission_probe.py" if key == "approval" else "python3 verify.py",
                             "purpose": "utility" if key == "approval" else "validation", "timeout_seconds": 30}
                if key == "approval":
                    arguments["permissions"] = [{"capability": "network", "scope": "once",
                        "reason": "UI 一次性批准演练；实际命令仅打印标记，不发起网络连接。"}]
                return self._call("ui-child-command", "run_command", arguments, "准备执行离线演练命令。")
            result = observations["ui-child-command"]
            return LLMResponse(text="命令工具实际返回：\n```json\n" + json.dumps(result, ensure_ascii=False)[:10000] + "\n```")
        paths = list(UI_EDITS) if key == "edit" else ["README.md", "src/orders/totals.py", "docs/order-rules.md"]
        for index, path in enumerate(paths):
            if "ui-child-read-" + str(index) not in observations:
                return self._call("ui-child-read-" + str(index), "read_file", {"path": path}, "查看 `" + path + "`。")
        if key == "edit" and "ui-child-edit" not in observations:
            operations = []
            for index, (path, (old, new)) in enumerate(UI_EDITS.items()):
                result = observations["ui-child-read-" + str(index)]
                if not result.get("ok"):
                    return LLMResponse(text="读取失败，未执行编辑：" + path)
                if old in result.get("content", ""):
                    operations.append({"op": "replace_text", "path": path, "old_text": old, "new_text": new,
                                       "expected_sha256": result["metadata"]["sha256"]})
            if operations:
                return self._call("ui-child-edit", "apply_workspace_edit", {"operations": operations},
                                  "通过受控编辑同时更新实现、测试和规则。")
            return LLMResponse(text="文件已经包含演练修复，没有重复修改。")
        if key == "edit":
            return self._report(observations.get("ui-child-edit"), "受控编辑结果")
        return LLMResponse(text="已读取当前共享目录中的实现与规则。\n\n"
                           + "\n\n".join("### " + path + "\n```text\n" + str(observations["ui-child-read-" + str(i)].get("content", ""))[:1200] + "\n```"
                                         for i, path in enumerate(paths)))

    @staticmethod
    def _report(result, heading, check=None):
        lines = ["## " + heading, "", "这是固定免费模型的演练记录。"]
        for label, value in [("执行", result), ("检查", check)]:
            if value is not None:
                status = "工具返回成功" if value.get("ok") else "工具返回失败"
                try:
                    payload = json.loads(value.get("content") or "{}")
                    if isinstance(payload, dict) and payload.get("status"):
                        status += "，子任务状态 `" + str(payload["status"]) + "`"
                except ValueError:
                    pass
                lines.append("- " + label + "：" + status)
        lines.extend(["", "可展开子任务检查实际工具输入和输出。有修改时，从文件审查区打开 Diff 或撤销本轮。"])
        return LLMResponse(text="\n".join(lines))


MARKDOWN_SAMPLE = """# 订单模块验收样本

这是 **免费固定响应**，用于检查插件的真实 Markdown 渲染与复制。

| 场景 | 入口 | 观察重点 |
| --- | --- | --- |
| 子任务 | 输入框中的子任务徽标 | 角色、耗时、返回关系 |
| 修改审查 | 文件行的 Diff | 实现、测试与说明 |
| 停止 | 方形 Stop 按钮 | 等待与回收 |

## 示例代码

```python
def average_order(total, count):
    return 0 if count == 0 else total / count
```

1. 打开历史，搜索“订单”。
2. 展开工具与子任务。
   - 检查只读完整过程。
   - 返回后检查草稿和展开状态。

> 长路径与代码块只在局部滚动，侧栏应保持单列。

长路径：`src/checkout/international_orders/fulfilment_and_reconciliation/formatting/order_summary_label.py`。

安全渲染样本：<img src=x onerror="alert('UI demo')">，应作为不执行的文本显示。

```text
很长的日志字段：0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ
```
"""
