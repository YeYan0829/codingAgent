# VS Code 产品验收（Phase 5）

Product Acceptance 与付费 Release Evaluation 分开。本文验证本地产品主链路，不声称模型质量或固定评测成绩。

离线 demo 自带 `python3 verify.py` 确定性验证入口，不依赖 pytest、CodeAgent Runtime `.venv` 或网络。
环境来源、Agent 自主探索与网络专项 dogfood 边界见
[Command Environment Contract](COMMAND_ENVIRONMENT_CONTRACT.md)。

## 1. 安装与演示仓库

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/codeagent-product-demo --root /tmp/codeagent-product-demo
```

命令只创建指定的空目录；目录非空时拒绝覆盖。随后在 Extension Development Host 打开该 demo 目录。

## 2. Quick Start 与产品验收链路

该离线除零任务用于稳定复现产品交互和安全交付边界，不承担真实仓库理解或模型能力证明。
GitHub 首页使用它截取产品主图、Approval 和 History。短 GIF 只补充会话切换等状态变化。
素材范围见 [GitHub 展示素材清单](../examples/showcase/SHOT_LIST.md)。

1. 在齿轮配置页选择 DeepSeek 或 GLM，保存 API Key、模型和预算。
2. New task，发送：`Fix the zero-count behavior, run the repository focused verification, and summarize the change.`
3. Agent 请求创建隔离 worktree 时检查工具、路径摘要，选择 Allow；Reject 应保持 source 不变。
4. 执行中点击 Stop，确认状态依次为 stopping、idle，且不再启动新 step；重新 New task 可重跑完整链路。
5. 完成修改和验证后，Current pending changes 显示文件、增删行与 current validation。
6. 点击文件，在中央 Editor 打开 VS Code 原生 Diff；关闭 Diff 不影响 Session。
7. 点击 Accept 并确认，source 中修复生效；或点击 Discard，source 保持 baseline。
8. 重载窗口并打开 History：Session/Event、changes/validation 事实仍可恢复，旧审批不成为授权。
   History 默认显示当前 workspace，可切换 All workspaces，并可按摘要文字和状态筛选；其他 workspace 的条目
   显示 repository，但不能在当前窗口打开或继续。
9. 使用低于任务所需的 UserTurn step 上限触发 budget waiting：确认内部 slice 自动续跑，UI 不要求反复点击
   Continue；达到总上限后没有新的模型请求。扩额输入框必须为空，分别验证 `+N` 追加步数和 N 设置新总上限，
   扩额后仍属于原 UserTurn；取消、错误或过期输入不执行。新 UserTurn 恢复 Session 默认上限。

## 3. Fail-closed 检查

- Approval 等待期间 History/Refresh/Changes 读取仍响应。
- Reject、Stop 或 Runtime 重启都不留下授权；后续敏感操作重新申请。
- budget waiting 不自动从 48 扩到 96；只有用户明确提交追加步数或新总上限才继续，且扩额不增加任何权限。
- validation 缺失或 stale 时 Accept 禁用；tainted/recovery 不允许危险操作。
- API Key 不出现在 `settings.json`、Session metadata、events、Output 或 Diff document 中。
- Accept/Discard 只在 execution idle 时可用，冲突或 identity 变化保留现场并返回错误。
- 发送非空消息后输入框立即清空；空消息不可发送，`Shift+Enter` 保留换行，输入法候选确认不会误发送。若
  `session/run` 被 Runtime 拒绝，原消息恢复到输入框，避免丢失。
- 用户消息和 Agent Markdown 的复制按钮可由鼠标与键盘访问，复制结果是原始纯文本；失败时有可访问的状态反馈。
- `workspace_upgrade` 显示“Create isolated workspace”，网络等 `command_permissions` 显示 command、reason、scope；
  `once` 授权明确显示“Allow once”。

## 4. Environment / Network 专项 dogfood

该场景与离线主链路分开，预期初始目标验证因缺少已声明依赖而失败：

```bash
.venv/bin/codeagent-product-demo \
  --scenario environment-network \
  --root /tmp/codeagent-environment-network-demo
```

在 Extension Development Host 打开该目录，原样发送生成器输出的任务。该任务禁止修改、vendoring 或重新实现
受跟踪源码及依赖，要求在 `.project-env` 中安装精确依赖，并运行 `.project-env/bin/python verify.py`。确认安装
命令携带 `network once` 并显示 Approval，分别使用全新 Session 记录一次 Reject 和一次 Allow。只有精确验证
真实返回 exit 0，才能记录 Allow 通过；替代断言不算。Reject 后安装命令不得执行、不得留下 grant，Agent 必须
停止并如实报告验证未运行。两条路径都记录 command、PATH、launch cwd、effective network、exit code 和
validation identity。

2026-09-06 人工 dogfood 已覆盖两条路径：Allow 在 Candidate 的 ignored `.project-env` 安装依赖并完成精确验证，
未同步到 source；Reject 保留未安装依赖的虚拟环境骨架，tracked diff 与 source 均为空，没有 validation evidence，
Agent 如实停止。该记录是本次开发工作区证据；正式发布仍应绑定冻结 commit 重跑。

当前网络是 host network，不是域名/端口白名单；应在隔离的测试环境中执行并审查完整安装命令。

## 5. Release Evaluation

正式固定 SWE-bench 运行必须在版本冻结后单独执行，记录 commit、selection、provider/model/config、价格快照、
manifest、逐题结果、cost、duration、tool calls 和 failure categories。它会产生真实 API 与 Docker 成本，不是
Product Acceptance 的通过条件，也不得为了结果临时修改 prompt 或能力。
