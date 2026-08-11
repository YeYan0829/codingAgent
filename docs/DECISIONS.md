# 设计决策

本文记录当前已经采用的设计及其边界。未来方向放在 [ROADMAP.md](ROADMAP.md)，具体模块行为放在 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 1. 模型只有请求权，Runtime 持有执行权

真实 LLM 只返回文本或结构化 tool call，不能直接接触文件系统、子进程、Policy 或 Approval。完整工具定义保存在 `ToolSpec`，模型只看到轻量 `ModelTool`：

```text
ToolSpec → ModelTool → provider-specific schema
```

这样可以防止 provider adapter 获得 handler 或权限信息，也避免 Runtime 依赖 OpenAI/DeepSeek 的具体 schema。

## 2. 默认 provider 使用 FakeLLM

默认 fake 使项目在没有 API key 和网络时仍可运行测试与 demo。真实 provider 通过 `--provider deepseek` 显式启用，避免把本地开发和自动化测试绑定到外部服务。

## 3. Session 默认保存在用户级目录

Session 是用户运行记录，不是项目源码，因此默认写入：

```text
~/.codeagent/sessions/<workspace-key>/<session-id>/
```

而不是污染每个项目的 `.codeagent/`。`workspace-key` 使用目录名和路径 hash，避免同名项目冲突；metadata 仍记录真实 workspace 路径。

`CODEAGENT_SESSION_ROOT` 和 `--session-root` 用于选择统一 session 数据库位置。旧 workspace-local session 在已知 workspace 下保留 load fallback，但全局发现能力主要面向新目录结构。

## 4. Session title 不依赖额外模型调用

标题由第一条用户消息裁剪生成，保证 session 创建稳定、便宜、可测试。未来可以增加可选的 LLM title，但不能成为创建 session 的前置条件。

## 5. 敏感文件可以显示存在，但默认阻止内容读取

目录 listing 默认可以显示敏感文件名称，并标记 `content blocked`。目标是保护 secret value，而不是让 Agent 错误判断配置文件不存在。

这是保守的启发式边界，不是 secret scanner。敏感目录的父路径组件检查仍需加强，文件名关键词也可能误伤正常源码；后续应允许项目级 policy 配置。

## 6. v0.3 只支持 pytest

每一种命令都需要独立考虑 schema、argv 构造、路径规则、环境、timeout 和副作用。v0.3 先用 pytest 验证：

```text
CommandSpec → Policy → Approval → Executor → Artifact
```

而不是通过“白名单 shell”过早扩大攻击面。npm、pnpm、ruff、mypy 和依赖安装应分别设计，不接受模型提交任意 command string。

## 7. Execution 使用 detached worktree

测试可能创建缓存、临时文件或修改 tracked 文件。独立 worktree 可以把这些 Git 状态变化留在任务目录，并固定 `base_commit`，同时支持普通退出后的 resume。

Detached worktree 不绑定用户正式分支，也不会自动 merge、rebase 或同步回 source。它是代码状态隔离，不是主机访问隔离。

## 8. v0.3 的 execution 只接受 clean Git source

Worktree 从 source HEAD 创建。如果 source 含未提交修改，直接创建的 worktree 看不到用户当前磁盘内容，Agent 会分析和测试错误版本。

v0.3 因此选择 fail closed，不自动 stash、commit 或复制用户修改。这是阶段性限制，不是最终产品体验。未来 dirty source 支持需要记录：

```text
base commit
+ staged changes
+ unstaged changes
+ untracked files
= source snapshot
```

并保证最终候选只包含 Agent 相对于初始 snapshot 的增量。

## 9. Resume 恢复原现场，不做自动同步

当前一个 execution session 绑定一个 worktree 和一个 `base_commit`。Resume 检查 source/worktree/HEAD/dirty/registration 状态，只在 `ready` 时恢复执行能力。

Source 变化后旧 session 标记 stale 或 source_dirty，不自动刷新。自动同步会让旧对话、测试和 artifacts 突然对应另一份代码，破坏审计语义。

近期如果需要基于最新 source 继续，应创建派生任务并记录 parent session；长期再评估逻辑 session 下的多个 workspace revision。

## 10. 普通退出保留 worktree，cleanup 保守执行

保留 worktree 可以支持 resume，也能保存失败测试和副作用现场。只有用户显式 cleanup 才尝试移除，并且不使用 `--force`。Dirty worktree 始终保留。

## 11. Worktree、Approval 和 Sandbox 互不替代

- Worktree 保护 source 的 Git 状态。
- Policy 判断系统规则是否允许请求。
- Approval 表达用户是否同意一次固定操作。
- Audit 记录实际发生的行为。
- Host sandbox 限制进程可访问的宿主资源。

v0.3 没有 host sandbox，因此只适用于可信本地仓库。不能因为命令经过 approval 或运行在 worktree 中，就声称它对恶意代码安全。

## 12. 长期交付模型采用候选修改，而不是 Agent 直接写 source

完整 Coding Agent 的目标体验是：

```text
source snapshot
→ Agent 在独立任务工作区编辑
→ 在候选代码上运行测试
→ 冻结 candidate 和测试证据
→ 用户审查
→ apply 前重新检查 source
→ 用户批准后才进入正式项目
```

因此写工具应先只操作 task workspace。正式 apply 是独立 Runtime 操作，需要绑定固定 candidate hash、目标 source 和预期 source snapshot，不能作为普通 `write_file` 的延伸。

## 13. 用户不应被迫理解内部 mode、worktree 和锁

`readonly` / `execution` 是 v0.3 为验证能力边界而暴露的 CLI 模式。成熟产品应让用户描述任务，由 Runtime 按需申请读取、测试、候选编辑和正式应用权限。

Worktree ownership、session lock、base commit 和 apply lock 属于内部一致性机制。默认 UX 只应要求用户决定：

- 是否信任一次有风险的执行；
- 是否接受最终候选修改；
- source 变化或语义冲突时希望保留什么。

高级诊断界面仍可展示内部状态，但不能把理解 Git worktree 作为普通使用前提。

## 14. v0.4 只提供一种受控文本 patch

Execution session 注册 `apply_text_patch(path, old_text, new_text)`，不同时提供 `write_file`、`edit_file` 和通用 unified patch 三套重叠接口。已有文件要求唯一上下文匹配；新增文件要求空 `old_text` 和既存父目录。第一版拒绝删除、symlink、敏感路径、二进制、非 UTF-8 和超限内容。

工具只绑定 `WorkspaceContext.active_root`，并再次要求独立 Git worktree。Readonly registry 不包含该工具，普通 `PermissionLevel.WRITE` 仍保持全局拒绝；受控能力使用单独的 `CANDIDATE_WRITE` 语义。

## 15. Candidate 是不可变交付物

Candidate 的 manifest 和 patch artifact 在冻结时固定，读取时重新验证 SHA-256。状态变化写入独立 `status.json`，不原地改写已展示的 manifest/patch。成功 pytest 必须有真实 command receipt，并且发生在最近编辑之后。

正式 apply 不从当前 worktree 重建 diff，只读取固定 artifact bytes。因此冻结后的继续编辑不能偷换旧 Candidate。

## 16. Apply 是用户控制的独立 Runtime 操作

Agent 不获得 source 写权限。`apply-candidate` 在批准前和批准后分别检查 source HEAD/clean 与 patch preflight，随后应用已展示 hash 对应的完全相同 bytes。拒绝、source dirty、HEAD 改变或并发变化均 fail closed；不自动 stash、reset、merge、rebase 或覆盖用户现场。

## 17. 审计证据不等于用户界面

v0.4 为验证安全不变量，选择保存完整 command、edit、Candidate 和 apply 证据。这回答“模型请求了什么、实际执行了什么、测试产生了什么变化、最终应用的是否为同一 patch”，但不意味着普通用户应直接浏览所有 JSON、日志和目录。

产品默认视图应只呈现任务进度、changed files、diff、测试结论、风险和最终审批。原始 trace 是按需展开的诊断信息。Candidate/hash/apply receipt 属于稳定交付记录；完整 stdout/stderr、逐次 edit journal 和 runtime HOME/TEMP 应有 retention，而不是永久无限保存。

同理，`readonly` / `execution` 是当前 capability 装配方式，不是长期用户心智模型。未来应由任务意图和实际工具需求触发能力升级，同时继续保留显式 Policy/Approval 和 workspace identity。
