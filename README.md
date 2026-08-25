# CodeAgent Runtime

CodeAgent Runtime 是一个本地运行、以不覆盖用户代码为首要约束的最小 Coding Agent Runtime。v0.4.0 已实现完整候选修改闭环：

```text
描述任务 → 读取代码 → pytest 基线 → 在隔离工作区编辑 → 再次验证
→ 冻结 Candidate → 用户审查 diff → 拒绝或批准进入 source
```

当前仍是功能分支上的 L2 任务闭环实现，尚未合并到 `master`，也没有创建 release/tag。它适用于用户信任的本地 Git 仓库；Git worktree 保护代码状态，但不是主机安全沙箱。

原 Repository Understanding、Project Model 和 Shared Project View 方案已停止推进并归档。当前保留 v0.4 Runtime 作为已实现底座，下一实践方向等待重新调研和规划；简单绘图 MCP 服务或插件目前只是待验证假设。

第一次接管项目，请先读 [System Vision](docs/系统目标.md) 和[当前设计上下文](docs/PROJECT_GUIDE.md)。如果要运行或排查现有 v0.4 Runtime，再查阅当前实现和测试文档；历史目标架构和未实现设计统一保存在 `docs/archive/`。

## v0.4 用户能力

- 使用 Fake 或 DeepSeek provider 探索本地代码。
- 在独立 task worktree 中运行受控 pytest，不接受任意 shell。
- 使用唯一的受控文本 patch 工具修改 UTF-8 文件，不能直接写 source。
- 测试通过后冻结包含固定 diff、hash、changed files 和测试结果的 Candidate。
- 用户可以查看、拒绝或批准 Candidate。
- Apply 前后检查 source HEAD 和 clean 状态；source 变化时拒绝，不自动 merge/rebase。
- 退出后可以恢复包含候选修改的 worktree；dirty worktree 不会被 cleanup 静默删除。

## 快速使用

安装开发版本：

```bash
python -m pip install -e ".[dev]"
```

只读探索：

```bash
codeagent start . --provider deepseek --model deepseek-v4-flash
```

执行完整 Coding Agent 任务：

```bash
codeagent start . --mode execution --provider deepseek --model deepseek-v4-flash
```

Candidate 审查与处理：

```bash
codeagent show-candidate <session-id-or-candidate-id>
codeagent apply-candidate <session-id-or-candidate-id>
codeagent reject-candidate <session-id-or-candidate-id>
```

Session 恢复和保守清理：

```bash
codeagent list-sessions
codeagent resume <session-id>
codeagent cleanup <session-id>
```

如果创建 session 时使用了 `--session-root`，后续命令目前也必须传入同一个值。也可以统一设置：

```powershell
$env:CODEAGENT_SESSION_ROOT="D:\codeagent-sessions"
```

## 为什么现在需要 readonly / execution

这是当前实现的能力边界，不是理想产品交互：

- `readonly` 直接读取 source，不创建 worktree，也不注册测试和编辑工具。
- `execution` 要求 clean Git source，预先创建 detached worktree，再注册 pytest、编辑和 Candidate 工具。

这样做让 v0.4 的权限边界容易验证，但迫使用户在任务开始前理解内部 mode。成熟体验应该只有“开始一个任务”：Agent 先读取；真正需要测试或编辑时，Runtime 再申请权限并创建隔离工作区。`readonly` / `execution` 应逐步退回内部 capability，而不是长期作为用户必须选择的产品概念。

## 为什么保存这么多 artifacts

真实 Coding Agent 通常也会保存部分对话、tool trace、diff、测试结果和审批记录，企业环境往往保存得更多；区别是这些内容通常被折叠在任务时间线或诊断页面中，而不是要求用户直接理解目录和 receipt。

v0.4 采用 audit-first 实现，为验证安全不变量，当前会保留每次命令的 request/result、stdout/stderr、workspace diff，每次编辑的 journal，以及 Candidate/apply receipt。它们默认位于用户级 session root，不写入项目 source，但目前没有自动 retention，因此确实比正常个人 Coding Agent 体验更重。

长期应分成三层：

| 层级 | 内容 | 目标生命周期 |
| --- | --- | --- |
| 交付证据 | Candidate patch/hash、changed files、最终测试摘要、apply receipt | 长期保留，用户可见 |
| 任务历史 | 对话、关键 tool timeline、失败原因 | 随 session 保留，可删除或导出 |
| 诊断副产物 | 完整 stdout/stderr、逐次 edit journal、runtime HOME/TEMP、命令中间 diff | 默认隐藏，按容量/时间自动清理 |

当前实现完成了证据采集，但尚未完成分层展示、压缩和 retention。这是明确的 UX/生命周期待办，不应被描述为最终用户必须承担的操作方式。

## 安全与功能边界

- 只支持 pytest，不支持 npm、pnpm、ruff、mypy、依赖安装或任意 shell。
- 文本 patch 不支持删除、移动/重命名、非 UTF-8、二进制、symlink、敏感路径、超大文件或自动建目录。
- 不支持 dirty source snapshot、自动 merge/rebase 或冲突解决。
- 不自动发现项目 `.venv`，pytest 使用运行 CodeAgent 的 Python。
- 没有网络、CPU、内存、磁盘、进程数量或系统调用 sandbox。
- pytest 仍以当前用户权限运行，只应对可信仓库启用 execution capability。

## 文档职责

- [System Vision](docs/系统目标.md)：项目为什么存在、目标、原则、非目标和成功标准。
- [当前设计上下文](docs/PROJECT_GUIDE.md)：当前有效边界、已暂停方向和下一轮待调研问题。
- [当前 Runtime 实现](docs/ARCHITECTURE.md)：v0.4 已实现模块、状态和运行边界，主要用于开发与排障。
- [测试说明](docs/TESTING.md)：自动化覆盖与不能证明的边界。
- [人工验收](docs/MANUAL_TEST.md)：当前版本唯一的真实终端验收清单。

旧 v0.3、原 v0.5 VS Code 路线、已暂停的 Target Architecture、Repository Understanding 方案、阶段性 Roadmap、历史决策和原始产品思考保存在 `docs/archive/`，不再作为当前实现或规划入口。
