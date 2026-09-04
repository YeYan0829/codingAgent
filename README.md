# CodeAgent Runtime

CodeAgent Runtime 是一个本地运行、以不覆盖用户代码为首要约束的最小 Coding Agent Runtime。当前用户流程是：

```text
创建 Session → 读取 source → 首次受保护操作时审批并创建 Agent worktree
→ 搜索 / 编辑 / 受控验证 → 查看当前修改 → 采纳或丢弃
→ 保留同一 worktree，在同一 Session 中继续
```

当前支持 Linux/WSL2 和本地 Git 仓库。Git worktree 隔离当前修改，Bubblewrap 对每条命令强制主机资源边界。

第一次接管项目，请先读 [System Vision](docs/系统目标.md) 和[当前设计上下文](docs/PROJECT_GUIDE.md)。

## 当前能力

- 使用 Fake、DeepSeek 或 GLM provider 探索本地代码；
- 使用遵守 repository ignore 的 ripgrep literal/regex 搜索和文件查找，可显式包含非敏感 hidden 文件；
- 严格读取 UTF-8 文本，并通过固定只读 Git 工具查看 status、diff stat 和实际 diff；
- 首次需要编辑或受控验证时，经批准按需创建 Session 专属 detached worktree；
- 使用唯一的 `apply_workspace_edit` 原子创建、修改、删除和移动多个 UTF-8 文本文件；
- 通过单一 `run_command` 在 Bubblewrap 中执行 pipeline、redirect、命令链和项目自定义工具；
- 额外文件写入与 WSL host network 由 Agent 显式申请，Policy 决定允许、询问或拒绝；
- 验证结果绑定 exact command、Candidate revision、Git tree 和 Sandbox Policy，后续编辑或命令会使旧证据失效；
- 查看、采纳或丢弃当前 pending changes；
- accept 时使用 accepted baseline、Agent worktree 和当前 source 做 Git 三方合并；
- 非冲突 source 并发修改可以合并，真实冲突保留双方现场并拒绝写 source；
- accept 后 worktree HEAD 前移为内部 checkpoint，用户 source branch 不会被自动 commit；
- Atomic Edit 与沙盒命令产生的变化都是合法当前修改；只有命令结束后无法建立可信 workspace 边界才进入 `workspace_tainted`；
- resume 时恢复 source-only、active 或 tainted workspace，`recovery_required` 始终 fail closed。
- 使用官方 prepared `/testbed`、per-command Docker projection 和 official grader 运行 SWE-bench；
- 将 provider 返回的 input/output/cache/reasoning token 记录为可审计 Session Event。

## 快速使用

### 1. WSL2 系统依赖与项目安装

Ubuntu/WSL2：

```bash
sudo apt-get update
sudo apt-get install -y bubblewrap ripgrep python3-venv
```

确认 `bwrap` 的 namespace smoke test 能通过；版本字符串存在并不等于当前 WSL/kernel 允许 namespace：

```bash
bwrap --ro-bind / / --unshare-user --unshare-pid --unshare-net \
  --new-session --die-with-parent --proc /proc --dev /dev -- /bin/true
```

安装 CodeAgent 开发版本：

```bash
python -m pip install -e ".[dev]"
```

搜索工具要求 `rg`；命令工具要求 `bwrap`。缺失或 probe 失败时命令 fail closed，不会裸执行。

### 2. 配置 DeepSeek API

Runtime 当前读取环境变量 `DEEPSEEK_API_KEY`，不会读取仓库 `.env`，也不要把 Key 写入 Git、任务 prompt 或 Agent command。当前默认模型是 `deepseek-v4-flash`，API endpoint 是 `https://api.deepseek.com`；模型名应以 [DeepSeek 官方 Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing/) 为准。

只在当前 WSL shell 临时配置：

```bash
read -rsp "DeepSeek API Key: " DEEPSEEK_API_KEY
echo
export DEEPSEEK_API_KEY
test -n "$DEEPSEEK_API_KEY" && echo "DEEPSEEK_API_KEY is set"
```

关闭该 shell 后变量消失。若使用 shell profile、密码管理器或 `direnv` 持久配置，请确保密钥文件不在 repository 内、权限至少为 `0600`。不要在 issue、日志或截图中打印变量值。

模型 API 请求由 Runtime 宿主进程发起，不属于 Candidate 内的 `run_command`，因此不需要 Agent 申请 sandbox NETWORK。Candidate 命令仍默认断网。当前 DeepSeek adapter 明确关闭 thinking mode，以避免尚未持久化 `reasoning_content` 的多轮 tool-call 协议；本轮人工测试评估的是现有 non-thinking adapter。

可先做一次只读 API smoke test：

```bash
codeagent ask . "只读取 README，并用三句话概括项目，不要编辑或执行命令" \
  --provider deepseek \
  --model deepseek-v4-flash \
  --session-root ~/.codeagent/smoke-sessions
```

这会产生真实 API 费用。先用小任务观察 token 消耗，并在 DeepSeek 控制台设置合理余额；价格可能变化，请查看上述官方页面。

### 2.1 配置 GLM-5.2

GLM provider 使用 `GLM_API_KEY`，默认模型为 `glm-5.2`，默认使用智谱标准 OpenAI-compatible endpoint `https://open.bigmodel.cn/api/paas/v4`。Runtime 初始采用 128k context 实验上限，而不是复用 DeepSeek 的 32k/22k 配置；该值仍显著低于模型官方容量，用于控制尚无 semantic condensation 时的单次请求成本：

```bash
read -rsp "GLM API Key: " GLM_API_KEY
echo
export GLM_API_KEY

codeagent start . --provider glm --model glm-5.2
```

如果使用 GLM Coding Plan Key，需要改用套餐专属 endpoint：

```bash
export GLM_BASE_URL=https://open.bigmodel.cn/api/coding/paas/v4
```

标准 API Key 与 Coding Plan Key/额度不可混用。Runtime 不读取仓库 `.env`，不要把 Key 写入 Git。当前 adapter 关闭 thinking mode，因为 Runtime 尚未持久化 reasoning content；本轮对比重点是 non-thinking tool-use 的行动收敛性。接口参数以[智谱 OpenAI API 兼容文档](https://docs.bigmodel.cn/cn/guide/develop/openai/introduction)和 [Coding Plan 接入说明](https://docs.bigmodel.cn/cn/coding-plan/tool/others)为准。

### 3. 创建交互 Session

创建 Session：

```bash
codeagent start . --provider deepseek --model deepseek-v4-flash
```

不再需要 `--mode readonly/execution`。Agent 初始直接读取 source，首次调用编辑或受控命令时 Runtime 才请求权限并创建 worktree。

查看和处理当前修改：

```bash
codeagent show-changes <session-id>
codeagent accept-changes <session-id>
codeagent discard-changes <session-id>
```

Session 与诊断数据：

```bash
codeagent list-sessions
codeagent resume <session-id>
codeagent cleanup <session-id>
```

如果创建 Session 时使用了 `--session-root`，后续命令也需要传入相同值，或统一设置 `CODEAGENT_SESSION_ROOT`。

### 4. 真实仓库人工任务

`examples/eval-repos` 是没有嵌套 `.git` 的上游快照，不能直接作为 execution workspace。已经准备了三个可重复任务、环境脚本和 workspace 外 oracle：

```bash
examples/eval-tasks/prepare.sh itsdangerous-strict-base64
sed -n '1,200p' examples/eval-tasks/tasks/itsdangerous-strict-base64.md

codeagent start ~/codeagent-evals/workspaces/itsdangerous-strict-base64 \
  --provider deepseek \
  --model deepseek-v4-flash \
  --session-root ~/.codeagent/eval-sessions
```

完成后运行独立验收：

```bash
examples/eval-tasks/verify.sh itsdangerous-strict-base64 \
  <session-id> ~/.codeagent/eval-sessions
```

任务列表、推荐顺序和人工记录项见 [真实 LLM 人工评估任务](examples/eval-tasks/README.md)。

## Session、worktree 与当前修改

- Session 固定绑定用户 source workspace，不建立额外 Task 实体；
- Session 初始为 `source_only`，不创建 worktree；
- worktree 创建后，读取、搜索、编辑和验证都切换到该 active workspace；
- worktree HEAD 表示最近 accepted baseline；`git diff HEAD` 表示当前 pending changes；
- accept 只同步 pending changes到 source，不结束 Session 或 worktree；
- discard 只把 pending changes恢复到 baseline，不修改 source；
- frozen patch 只在一次 accept 审查窗口临时存在。

## 持久化

正常 Session 主要保存：

```text
session.json       当前 Session/workspace 状态
events.jsonl       对话、审批和关键结果摘要
Git worktree       当前代码和 accepted baseline
```

成功 Atomic Edit 不永久保存 request、plan、patch 和 rollback backup。成功且没有 workspace side effect 的命令会清理完整 stdout/stderr 和 runtime HOME/TMP。失败、timeout、workspace side effect、未完成事务或 `recovery_required` 才在 `diagnostics/` 保留必要材料。项目不再同时维护 transcript/events 或 worktree/current.patch 两份权威事实。

## 安全与功能边界

- `run_command` 接受任意 shell string，但默认无网络、host root 只读，额外资源必须显式申请；
- 原子编辑不支持非 UTF-8、二进制、symlink、敏感路径、mode 变更、copy、case-only rename 或目录删除；
- Git 三方合并冲突不会由 Agent 自动解决；
- Runtime 不判断 validation command 是否充分；`purpose=validation` 且成功只形成“存在当前验证证据”；
- 当前没有 cgroup CPU/内存/进程数限制、domain/port 网络 ACL 或通用 secret 文件名扫描；
- bind mask 只能遮罩 sandbox 构造时已经存在的确定敏感路径，不能保证未来才出现的同名路径。

## 文档职责

- [System Vision](docs/系统目标.md)：长期目标、原则和非目标；
- [当前设计上下文](docs/PROJECT_GUIDE.md)：当前状态、下一步和文档导航；
- [近期实践计划](docs/NEXT_PHASE_PLAN.md)：实施顺序和验收层次；
- [当前 Runtime 实现](docs/ARCHITECTURE.md)：已经实现的数据流和安全边界；
- [Benchmark 说明](docs/BENCHMARKING.md)：SWE-bench 边界、结果语义、token 审计和当前基线；
- [当前有效决策](docs/DECISIONS.md)：跨阶段产品级约束；
- [测试说明](docs/TESTING.md) 与 [CLI Dogfood](docs/MANUAL_TEST.md)：自动化和正式 CLI 验收。

历史路线和已归档调研位于 `docs/archive/`，不作为当前实现依据。
