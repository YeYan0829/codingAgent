# 真实 LLM 人工评估任务

这里把 `examples/eval-repos` 的上游仓库快照整理为可重复的人工任务。快照本身没有 `.git`，不能直接作为 CodeAgent workspace；先用 `prepare.sh` 复制、初始化 Git，并准备 Candidate 外的测试环境。

首轮依赖版本固定在 `prepare.sh` 中。依赖只在环境准备阶段下载；Agent 任务明确禁止临时安装或联网。

## 推荐顺序

| Task ID | 仓库 | 类型 | 难度 | 主要观察点 |
| --- | --- | --- | --- | --- |
| `itsdangerous-strict-base64` | ItsDangerous | bugfix | 小 | 搜索、错误语义、针对性测试 |
| `click-short-help-sentences` | Click | behavior change | 中 | 现有算法理解、边界测试、最小修改 |
| `httpx-no-proxy-cidr` | HTTPX | configuration bugfix | 中偏大 | 多分支配置、IPv4/IPv6、回归范围 |

第一次真实 API dogfood 建议先跑前两个。HTTPX 文件更多、上下文和调用轮次通常更高，适合确认基础流程稳定后再跑。

## 准备 workspace

从项目根目录运行：

```bash
examples/eval-tasks/prepare.sh itsdangerous-strict-base64
examples/eval-tasks/prepare.sh click-short-help-sentences
examples/eval-tasks/prepare.sh httpx-no-proxy-cidr
```

默认产生：

```text
~/codeagent-evals/workspaces/<task-id>   独立、clean 的 Git workspace
~/.cache/codeagent-evals/<task-id>       预装依赖的 venv
```

可把 workspace 根作为第二参数覆盖：

```bash
examples/eval-tasks/prepare.sh click-short-help-sentences /data/codeagent-evals
```

脚本发现目标已存在时拒绝覆盖。重跑前请人工删除不再需要的 disposable workspace；不要对有结果的目录直接重建。
缓存根可通过 `CODEAGENT_EVAL_CACHE` 覆盖；如果覆盖，Agent prompt 中的验证命令也要换成实际 venv 路径。

## 运行与验收

打开对应任务文档，复制“给 Agent 的请求”：

```bash
test -n "${DEEPSEEK_API_KEY:-}" || echo "请先按项目 README 配置 DeepSeek Key"
codeagent start ~/codeagent-evals/workspaces/<task-id> \
  --provider deepseek \
  --model deepseek-v4-flash \
  --session-root ~/.codeagent/eval-sessions
```

完成后先查看修改，不要立即采纳：

```bash
codeagent show-changes <session-id> --session-root ~/.codeagent/eval-sessions
```

在另一个终端通过 Session ID 运行独立 oracle。脚本读取 `session.json` 中的 active worktree，因此验证的是尚未 accept 的 Candidate，而不是原始 source。Oracle 位于目标 workspace 外，不会被复制进去：

```bash
examples/eval-tasks/verify.sh <task-id> <session-id> ~/.codeagent/eval-sessions
```

人工检查 patch、Agent 实际运行的 validation command、是否申请了不必要权限，以及 oracle 结果。满意后再运行 `accept-changes`；不满意使用 `discard-changes`。第二参数也可以直接传一个 worktree 路径，便于调试脚本本身。

## 记录建议

每个任务至少记录：Session ID、模型、是否一次完成、调用轮次、使用过的工具、权限请求、Agent validation、oracle 结果、最终 patch 是否采纳。不要把 API Key 写入任务记录、prompt、命令字符串或仓库文件。
