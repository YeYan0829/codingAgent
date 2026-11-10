"""生成可由正式 VS Code 插件加载的本地 UI 验收仓库与持久样本会话。"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

from codeagent.product.service import ProductApplicationService
from codeagent.runtime.bubblewrap import BubblewrapBackend


FILES = {
    ".gitignore": "__pycache__/\n*.pyc\n.project-env/\n",
    "UI_DEMO.json": '{"kind":"codeagent-ui-demo","version":1,"model":"fake/ui-fixture"}\n',
    "src/orders/__init__.py": "",
    "src/orders/totals.py": "def average_order(total, count):\n    return total / count\n",
    "tests/test_totals.py": (
        "import unittest\nfrom src.orders.totals import average_order\n\n\n"
        "class OrderTotalsTest(unittest.TestCase):\n"
        "    def test_normal_orders(self):\n        self.assertEqual(average_order(120, 3), 40)\n\n"
        "    # UI_DEMO_EMPTY_CASE\n"
    ),
    "docs/order-rules.md": "# 订单计算规则\n\n订单数必须大于零。\n\n验证命令：`python3 verify.py`。\n",
    "verify.py": (
        "import unittest\nfrom src.orders.totals import average_order\n\n"
        "suite = unittest.defaultTestLoader.discover('tests')\n"
        "result = unittest.TextTestRunner(verbosity=2).run(suite)\n"
        "assert average_order(120, 0) == 0\n"
        "if not result.wasSuccessful():\n    raise SystemExit(1)\n"
        "print('UI demo: offline verification passed')\n"
    ),
    "tools/fail_check.py": (
        "import sys\nprint('UI demo: expected failure scenario')\n"
        "print('AssertionError: this check intentionally exits with code 2', file=sys.stderr)\nraise SystemExit(2)\n"
    ),
    "tools/permission_probe.py": "print('UI demo: permission probe completed; no network request was made')\n",
    "src/checkout/international_orders/fulfilment_and_reconciliation/formatting/order_summary_label.py":
        'def order_label(order_id):\n    return f"Order #{order_id}"\n',
    "README.md": """# CodeAgent UI 验收仓库

这是由当前 Runtime 生成的独立演练 Git 仓库，使用免费的 `fake/ui-fixture`。
源代码、测试和规则中预置空订单问题；样本 01 已通过受控编辑修复三个文件。

## 在正式插件中查看

打开上级 `ui-demo.code-workspace`，从 CodeAgent 标题栏的历史按钮选择 `[01]` 至 `[08]` 样本。
本目录的 `.vscode/settings.json` 已指向同包的独立 Session Root 和本机 Runtime。
若尚未安装当前源码版插件，在 Runtime 源仓库按 F5，选 `Run CodeAgent UI Demo`。

| 样本 | 观察重点 |
| --- | --- |
| 01 多角色与多文件修改 | 三种子角色、同一子会话续问、真实 Diff、原生撤销确认 |
| 02 只读探索与两轮追问 | 历史、角色、完整过程和跨轮次内容 |
| 03 Markdown 与长内容 | 表格、嵌套列表、代码复制、长路径、HTML 不执行 |
| 04 命令与子模型失败 | 退出码 2、stderr、失败子任务及正常收尾 |
| 05 已停止的子任务 | 持久停止记录和子资源回收 |
| 06 恢复检查 | 明确标注的合成恢复现场，受控写入/撤销禁用 |
| 07 实时等待与 Stop | 发送 `ui:wait`，等待子模型时点击方形 Stop |
| 08 实时批准 | 发送 `ui:approval`，检查主方等待、申请者和批准/拒绝 |

运行与批准不能作为活跃 Python 栈永久保存；07、08 打开后由你发送命令启动。
权限演练命令只打印标记，不联网；请求一次性网络权限仅用于验收真实 Approval 流程。
可拒绝，也可点击 Stop；不会自动批准。关闭宿主后不会保留批准。

修改直接写入当前 demo，撤销受控编辑可能使 `python3 verify.py` 再次失败，这是预置问题恢复后的预期。
修改冲突可在 01 的文件中手动加一行，再尝试撤销；外部修改应保留且整次拒绝。
样本不是外部真实模型成绩，也不使用真实项目或真实 API Key。

完整会话 ID、来源和状态在上级 `samples.json`；验收步骤在上级 `SAMPLES.md`。
重新生成请使用一个新目录，不删除或覆盖旧样本与日志。
""",
}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _wait(predicate, *, timeout=120, description="session"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    raise RuntimeError("UI demo timed out: " + description)


def create_ui_demo(root: Path | str) -> dict:
    root = Path(root).expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise RuntimeError(f"demo directory must be empty; use a new directory: {root}")
    # 创建前验证真实沙箱；不以宿主 shell 替身填造命令成功记录。
    BubblewrapBackend().probe(root / "workspace")
    workspace, session_root = root / "workspace", root / "sessions"
    workspace.mkdir(parents=True, exist_ok=True)
    for relative, text in FILES.items():
        _write(workspace / relative, text)
    executable = root / "run-rpc"
    _write(executable, "#!/usr/bin/env python3\nimport os, sys\n"
           + f"python = {sys.executable!r}\nos.execv(python, [python, '-m', 'codeagent.product.rpc', *sys.argv[1:]])\n")
    executable.chmod(0o755)
    settings = {"codeagent.rpcCommand": str(executable), "codeagent.sessionRoot": str(session_root)}
    _write(workspace / ".vscode/settings.json", json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
    _write(root / "ui-demo.code-workspace", json.dumps({"folders": [{"path": "workspace", "name": "UI 验收 · 订单示例"}],
                                                       "settings": settings}, ensure_ascii=False, indent=2) + "\n")
    for arguments in [("init",), ("config", "user.name", "CodeAgent UI Demo"),
                      ("config", "user.email", "ui-demo@codeagent.invalid"), ("add", "."),
                      ("commit", "-m", "UI demo baseline")]:
        subprocess.run(["git", *arguments], cwd=workspace, capture_output=True, check=True, timeout=20)
    service = ProductApplicationService(session_root)
    service.save_repository_access(workspace, [], "0")
    samples = []

    def sample(number, title, kind, messages=(), command=None):
        detail = service.create_session(workspace, provider="fake", model="ui-fixture", delegation_enabled=True,
                                        title=f"[{number:02d}] {title}")
        record = {"number": number, "sessionId": detail["sessionId"], "title": detail["title"], "kind": kind,
                  "messages": list(messages), "command": command}
        samples.append(record)
        print("生成 " + detail["title"], file=sys.stderr, flush=True)
        for message in messages:
            service.run_session(detail["sessionId"], message)
            _wait(lambda: service.supervisor.state(detail["sessionId"]) == "idle", description=detail["title"])
            events = service._load(detail["sessionId"]).read_events()
            if events[-1].type == "execution_job_finished" and events[-1].payload.get("error_type"):
                raise RuntimeError("UI sample Runtime failed: " + detail["title"])
        return detail["sessionId"]

    try:
        sample(2, "只读探索与两轮追问", "runtime", ("ui:explore 定位订单计算与验证入口", "ui:followup 继续核对订单说明"))
        sample(3, "Markdown、表格与长内容", "runtime", ("ui:markdown 检查内容呈现与代码复制",))
        sample(4, "命令失败与子模型故障", "runtime", ("ui:failure 演练预期失败",))
        sid = sample(5, "已停止的子任务与资源回收", "runtime")
        service.run_session(sid, "ui:wait 演练 Stop 后保存的历史")
        _wait(lambda: any(e.type == "model_request_intent" and e.payload.get("agent_id") not in {None, "main"}
                          for e in service._load(sid).read_events()), description="child request before Stop")
        service.stop_session(sid)
        _wait(lambda: service.supervisor.state(sid) == "idle", description="Stop acknowledgement")
        sid = sample(6, "恢复检查（合成现场，未执行未知命令）", "synthetic_recovery")
        store = service._load(sid)
        store.append_event("user_message", {"message": "UI 恢复场景样本：检查操作禁用与恢复说明。"})
        store.append_event("assistant_tool_calls", {"message": "这是明确标注的合成恢复样本，没有执行未知命令。", "tool_calls": []})
        store.mark_recovery_required("UI 合成现场：用来验收恢复提示与写操作禁用；没有执行结果未知的真实命令。")
        sample(7, "实时等待与 Stop：发送 ui:wait", "live_ready", command="ui:wait")
        sample(8, "实时批准：发送 ui:approval", "live_ready", command="ui:approval")
        # 最后写入01，使正式插件首次打开默认选择可审查的三文件修改。
        sample(1, "多角色子任务与多文件修改", "runtime", ("ui:edit 修复空订单，同步测试和规则并验证",))
    finally:
        service.supervisor.shutdown()
    manifest = {"version": 1, "workspace": str(workspace), "sessionRoot": str(session_root),
                "model": "fake/ui-fixture", "samples": sorted(samples, key=lambda s: s["number"]),
                "origin": "普通Runtime/持久SessionStore；仅06为显式合成恢复标记。07/08需要插件中发送命令。"}
    _write(root / "samples.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    _write(root / "SAMPLES.md", _instructions(root, manifest))
    return check_ui_demo(root)


def check_ui_demo(root: Path | str) -> dict:
    root = Path(root).expanduser().resolve()
    manifest = json.loads((root / "samples.json").read_text())
    if manifest["workspace"] != str(root / "workspace") or manifest["sessionRoot"] != str(root / "sessions"):
        raise RuntimeError("demo moved: regenerate in a new directory; do not rewrite original session paths")
    service = ProductApplicationService(root / "sessions")
    summaries = service.list_sessions(root / "workspace")
    loaded = {}
    for sample in manifest["samples"]:
        detail = service.get_session(sample["sessionId"])
        if detail["provider"] != "fake" or detail["model"] != "ui-fixture" or detail["executionState"] != "idle":
            raise RuntimeError("unexpected UI sample identity/state: " + sample["title"])
        loaded[sample["number"]] = detail
    edits = loaded[1]["workspaceEdits"]
    if not edits["available"] or edits["fileCount"] != 3 or not loaded[1]["availableActions"]["canUndo"]:
        raise RuntimeError("sample 01 has been changed or undone; generate a new demo for the original acceptance state")
    for path in edits["files"]:
        change = service.get_edit_file(loaded[1]["sessionId"], path)
        if change["before"] == change["after"]:
            raise RuntimeError("sample Diff is empty: " + path)
    runs = loaded[1]["orchestration"]["tasks"][-1]["runs"]
    if {r["role"] for r in runs} != {"code-explorer", "general-purpose", "bash-runner"} or not all(r["terminalAck"] for r in runs):
        raise RuntimeError("sample 01 delegation is incomplete")
    commands = [e.payload for e in service._load(loaded[1]["sessionId"]).read_events() if e.type == "command_completed"]
    if not any(e.get("exit_code") == 0 for e in commands):
        raise RuntimeError("sample verification did not complete in the real sandbox")
    result = {"root": str(root), "workspace": str(root / "workspace"), "workspaceFile": str(root / "ui-demo.code-workspace"),
              "sessionRoot": str(root / "sessions"), "sampleCount": len(loaded), "discoveredCount": len(summaries),
              "diffFiles": edits["files"], "subagentRoles": sorted({r["role"] for r in runs})}
    _write(root / "verification.json", json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def _instructions(root, manifest):
    lines = ["# 正式插件 UI 验收", "", "先打开 `ui-demo.code-workspace`，从 CodeAgent 历史选择样本。", "",
             "如使用当前源码，在 Runtime 源仓库按 F5 → `Run CodeAgent UI Demo`。", "",
             "| 样本 | 会话 ID | 来源 | 实时启动命令 |", "| --- | --- | --- | --- |"]
    for sample in manifest["samples"]:
        lines.append("| " + sample["title"] + " | `" + sample["sessionId"] + "` | " + sample["kind"]
                     + " | " + ("`" + sample["command"] + "`" if sample["command"] else "—") + " |")
    lines.extend(["", "1. 01：展开三种角色和完整过程；逐个打开文件 Diff；取消一次撤销，确认文件保持；需要时再确认撤销。",
                  "2. 02/03：搜索历史、跨会话保留草稿；检查两轮内容、表格、代码复制、长路径、HTML 不执行。",
                  "3. 04/05/06：分别检查故意失败、已停止并回收、合成恢复现场；不能显示为全部成功或仍在运行。",
                  "4. 07：发送 `ui:wait`，在模型请求等待时查看子任务与主方等待，点击 Stop 并等到回收完成。",
                  "5. 08：发送 `ui:approval`，查看 bash-runner 申请者与一次性范围，分别演练拒绝/允许；命令没有任何网络请求。",
                  "6. 使用真实侧栏改变宽度与主题；Diff/确认框应是 VS Code 原生界面，非浏览器预览。", "",
                  "运行中/批准状态不通过JSON伪装，也不保存旧批准。重复验收可继续相应会话或生成新的demo。",
                  "本包包含路径相关的Runtime会话。搬动后请重新生成，不能只复制session.json修补路径。",
                  "生成器拒绝非空目录，不覆盖旧会话与日志。", "", "复检当前包：", "", "```bash",
                  f"{shlex.quote(sys.executable)} -m codeagent.product.ui_demo --check --root {shlex.quote(str(root))}", "```", ""])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="生成正式VS Code插件可加载的免费UI验收仓库与样本会话")
    parser.add_argument("--root", type=Path, default=Path(".codeagent/ui-demo"))
    parser.add_argument("--check", action="store_true", help="复检已生成的样本，不启动模型或命令")
    args = parser.parse_args()
    try:
        result = check_ui_demo(args.root) if args.check else create_ui_demo(args.root)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
