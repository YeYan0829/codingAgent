# 测试说明

本页说明每组测试能够证明什么，以及哪些行为仍需要真实系统或人工检查。

Python 测试使用 pytest。VS Code Extension 测试使用 Node 内置 test runner。

## 安装测试依赖

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

## 默认测试

运行 Python 测试：

```bash
.venv/bin/python -m pytest -q
```

运行 Extension 测试：

```bash
cd vscode-extension
npm test
```

Extension 当前没有运行时 npm 依赖。`npm test` 直接执行 `node --test test/*.test.js`。

## 各组测试证明什么

| 测试范围 | 主要证明 | 不能证明 |
| --- | --- | --- |
| 搜索、读取和权限 | 工具参数受到检查，禁止的路径和权限会被拒绝 | 真实模型不会提出危险操作 |
| 文件编辑和 Git worktree | 编辑失败可以回滚；源仓库不会被普通 Agent 修改直接覆盖 | 所有 Git 并发情况都能自动合并 |
| 命令服务 | 权限、超时、文件变化和失败原因会被记录 | 当前主机一定能运行 Bubblewrap |
| 上下文和步骤预算 | 历史会被裁剪；工具调用保持配对；任务会在预算用尽时等待 | 任意长任务都不会超过模型上限 |
| 产品和 JSON-RPC | 会话、审批、Stop、Diff、Accept 和 Discard 的状态规则 | 真实 VS Code 界面布局和交互体验 |
| 模型适配器 | 请求格式、响应解析和 token 用量转换 | 真实 API 当前可用或模型质量达标 |
| Extension | 扩展配置、HTML 生成和界面消息处理 | VSIX 可以在真实 VS Code 中完整运行 |
| SWE-bench | 环境准备、grader 结果、批次恢复和用量汇总 | 正式模型成绩或 Docker daemon 可用 |
| 示例仓库 | 示例可以重复创建，并保留预期失败基线 | 真实模型一定能修复示例 |

大多数测试使用 Fake 模型、临时 Git 仓库或假的命令执行器。
这样可以稳定验证 Runtime 状态，但不能替代真实系统集成。

相关测试文件：

- 工具与权限：`test_policy.py`、`test_permissions.py`、`test_path_guard.py`
- 编辑与 worktree：`test_atomic_edit.py`、`test_candidate_loop.py`、`test_session_workspace_lifecycle.py`
- 命令执行：`test_command_sandbox_service.py`、`test_sandbox_executor.py`
- 上下文：`test_context_management.py`、`test_context_tool_call_history.py`、`test_execution_slices.py`
- 产品与 RPC：`test_product_execution.py`、`test_product_rpc.py`、`test_product_changes.py`
- 模型适配器：`test_deepseek_client.py`、`test_glm_client.py`、`test_model_factory.py`
- Extension：`test_vscode_extension_manifest.py`、`vscode-extension/test/render.test.js`
- SWE-bench：`test_swebench_*.py`、`test_benchmark_accounting.py`、`test_trajectory_analyzer.py`

默认测试不会调用付费模型，也不会证明 Agent 的修复质量。

## 真实 Bubblewrap 测试

默认测试会跳过需要真实 Bubblewrap 的用例。确认 `bwrap` 可用后运行：

```bash
CODEAGENT_TEST_BWRAP=/usr/bin/bwrap \
  .venv/bin/python -m pytest -q tests/test_bubblewrap_integration.py
```

环境变量的值是 `bwrap` 的可执行文件路径，不是布尔开关。

这组测试会实际检查以下行为：

- Agent worktree 可以写入。
- 源仓库和 Git metadata 保持只读。
- 宿主根目录默认保持只读。
- 网络默认关闭。
- 项目工具可以在 sandbox 中运行。
- 每条命令使用新的 shell。
- 用户批准后可以使用宿主网络。
- 命令超时后，后台子进程不会继续写文件。
- 每条命令使用的临时 HOME 会被清理。
- 找不到 `bwrap` 时，命令不会脱离 sandbox 运行。

private `/tmp` 和敏感路径遮罩主要由单元测试检查。
这组测试不证明资源配额、复杂 seccomp 或域名白名单。

## 真实 SWE-bench Docker 测试

这组测试需要当前 Linux 或 WSL 环境能够连接 Docker daemon。先确认：

```bash
docker info
```

然后运行：

```bash
CODEAGENT_RUN_SWEBENCH_DOCKER=1 \
  .venv/bin/python -m pytest -q tests/test_swebench_docker_integration.py
```

测试可能拉取一个官方 SymPy 镜像。它会检查代码修改能否在 Docker `/testbed` 中执行并返回宿主 worktree。

这个测试不会运行 official grader，也不会调用付费模型。

固定题集生成测试需要本地存在指定版本的任务仓库。缺少该仓库时，对应用例会跳过。

## 人工产品验收

以下行为需要真实 VS Code 窗口：

- 安装后的 VSIX 能找到独立安装的 Runtime。
- CodeAgent 默认显示在右侧，中央可以打开原生 Diff。
- Approval、Stop 和预算扩额在界面中符合预期。
- History 在重载和切换仓库后符合预期。
- Accept 和 Discard 对源仓库产生正确结果。
- API Key 不会出现在本次会话记录和 Output 中。

执行步骤和证据表见[发布验收](RELEASE_VALIDATION.md)。

测试产生的新记录应使用临时 Session Root。不得删除已有 `.codeagent/sessions/` 数据。

## CI

`.github/workflows/ci.yml` 使用 Ubuntu 和 Python 3.12。它会运行默认 pytest。

CI 还会运行 Extension 的 Node 测试和 `git diff --check`。

真实 Bubblewrap、Docker、模型 API 和 GUI 测试默认不会在 CI 中运行。
