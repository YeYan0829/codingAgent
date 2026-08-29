# v0.4 Coding Agent Capability Inventory

> 文档性质：2026-08 的能力与招聘信号调研快照。它解释优先级来源，不是当前能力权威清单、实施计划或完成状态；当前事实见 [ARCHITECTURE.md](../ARCHITECTURE.md)，下一步见 [NEXT_PHASE_PLAN.md](../NEXT_PHASE_PLAN.md)。

本文起源于 2026-08-25 的 v0.4 capability inventory，并同步记录 Stage 2 首批 Search/Read 与 Atomic Edit 已完成能力。

本文是调研与优先级分析，不是 Roadmap 或 Technical Design。成本只是单人开发、包含测试和文档的粗略量级，不代表承诺排期。

## 1. 结论

v0.4 更准确的定位是：

> 带审计、隔离工作区和受控交付能力的 Coding Agent Runtime 原型。

它还不是功能完整的 Coding Agent。当前实现的安全、worktree、Candidate 和审计链，明显强于实际编码能力；仓库搜索、代码编辑、项目命令、上下文控制和任务级评估仍不足以支撑日常开发。

下一阶段应先让基础 Coding Agent **能用、能测、能展示**，再决定是否引入绘图 MCP、RAG、长期项目知识库或更复杂的 Repository Understanding 设计。

## 2. 审计口径

### 2.1 本地代码证据

本次审计检查了：

- 文件和 Git 读取工具；
- AgentRunner、ContextBuilder 和 Model Gateway；
- 文本编辑、受控 pytest、worktree、Candidate 和 Apply；
- Policy、Approval、敏感文件保护和 command artifact；
- CLI、session resume、测试结构和示例仓库。

2026-08-27 在 Linux/WSL 当前工作区执行完整 `pytest -q`，结果为 `139 passed`。该结果证明现有 Runtime 组件行为受到测试，但不能证明 Agent 能稳定完成真实 Coding Task。

### 2.2 外部样本

外部调研包括：

- Cursor、Claude Code、Aider 的官方能力文档；
- 7 个仍可访问的 Coding Agent / Agent Infrastructure 相关招聘 JD。

JD 是为了识别反复出现的工程能力信号而选取的定向样本，不是完整就业市场统计。文中的“求职价值”是基于样本和当前项目条件作出的工程判断。

### 2.3 成本等级

| 等级 | 单人开发粗略量级 |
| --- | --- |
| S | 2–5 天 |
| M | 1–2 周 |
| L | 3–6 周 |
| XL | 需要持续演进，不适合在一个小版本内完成 |

## 3. Capability Inventory

| 能力 | v0.4 已有 | 主要缺失 | 主流 Coding Agent 怎么做 | 求职价值 | 成本 |
| --- | --- | --- | --- | --- | --- |
| Agent loop | 串行 ReAct tool loop；tool result 回到下一轮；单轮最多 12 个模型步骤 | 无 streaming、取消、重试、工具并发、循环检测、token/cost budget 和结构化结束状态 | 暴露 turn limit、权限模式、流式事件和可恢复 session；对超时与失败有明确状态 | 高：能展示 agent harness 基础，但当前实现仍薄 | M |
| Repository exploration | 有界 `list_dir`/`show_tree`、严格 UTF-8 分段读取、ripgrep literal/regex 搜索与统一文件视图、机器解析 Git status、diff stat 和实际 diff | 无符号、引用、调用关系、Git history、LSP 或语义检索；真实大仓库效果仍待 dogfood | 组合 grep/codebase search、相关文件选择和按需读取；大仓库不靠全量读取 | 极高：基础文本定位能力已补齐，下一步需要任务验证 | 已完成首批；扩展 M |
| Context management | 从 session 重建最近 40 个 event | 无 token 计数、相关性选择、摘要、compaction、缓存、溢出策略和成本统计 | 保留当前问题需要的源码与结果；历史通过 compaction/retrieval 按需恢复 | 高：JD 高频，但应基于任务 eval 优化 | M–L |
| Code editing | 结构化多文件原子事务；create/replace/delete/move；自动父目录、SHA 冲突检查、rollback、edit revision 和 Candidate | 无 binary/mode/copy/case-only rename、格式化、通用 undo 或跨进程事务 | 提供 create/edit/delete/move、multi-file patch、diff、reapply 或 checkpoint | 极高：首批 workspace edit 已补齐，下一步需真实任务验证 | 已完成首批；扩展 M |
| Command and validation | execution worktree 内受控运行 pytest；有 timeout、approval 和 artifact | 无 lint、typecheck、build、项目脚本、其他语言测试、依赖安装和受控通用命令 | 允许配置项目级 test/lint/build 命令，在权限边界内把失败结果送回 Agent | 极高：基础 Coding Agent 的核心能力 | M |
| Git/workspace | clean source、detached worktree、resume、Candidate freeze/hash、apply 前复检 | 无常规 branch/commit 工作流、冲突处理、局部接受和自然 undo | 使用 checkpoint、自动 commit 或隔离 workspace，提供 diff/undo/review | 高：这是 v0.4 当前最有区分度的能力 | 已较强；扩展 M |
| Safety and approval | PathGuard、敏感文件阻止、Policy、Approval、minimal environment、`shell=False`、审计 | 无主机 sandbox、网络隔离、CPU/内存/磁盘限制；策略和命令能力不够灵活 | 工具级 allow/ask/deny，加 sandbox、workspace isolation 和可复现执行 | 极高：适合面试深入说明边界与 trade-off | L–XL |
| Provider/model | Fake 和 DeepSeek；内部 request/response 类型 | OpenAI adapter 仍是 placeholder；无 streaming、多 provider、fallback、usage/cost | provider adapter、模型选择、失败回退、usage telemetry 和模型比较 | 中高 | S–M |
| Instructions and extensibility | 固定 system prompt、ToolRegistry | 未形成项目规则自动加载；repo exploration strategy 未进入稳定主链；无 MCP、skills、hooks | 项目级 instructions、可配置 tools、MCP/skills/hooks | 高，但不应早于基础工具和 eval | M |
| Observability | append-only events、transcript、stdout/stderr、command/edit/Candidate receipts | 无统一 task trace、耗时/token/cost、失败分类、离线 replay 和比较视图 | trajectory、offline replay、scorer、regression alert 和 dashboard | 极高：是项目从 demo 走向工程系统的关键证据 | M |
| Agent evaluation | Runtime 单元/集成测试、一个 tiny bug 示例仓库 | 无 Agent 任务集、成功判定、trajectory eval、模型/提示/工具回归基线 | 固定数据集、验收脚本、trajectory 记录、离线 replay、质量和成本比较 | 极高：JD 中最稳定的共同信号 | M |
| User experience | CLI、session resume、Candidate diff 审查 | 无 streaming、计划/进度、工具状态、取消、错误恢复提示和 task summary | 展示任务进度、工具调用、修改、diff、测试和审批；隐藏底层 receipt 复杂度 | 高：直接影响能否演示和解释系统价值 | M |

## 4. 现有能力的代码依据

- 基础文件工具：[`codeagent/tools/fs_read.py`](../../codeagent/tools/fs_read.py)
- 三个固定 READ Git 工具：[`codeagent/tools/git_read.py`](../../codeagent/tools/git_read.py)
- ripgrep 搜索服务：[`codeagent/runtime/repository_search.py`](../../codeagent/runtime/repository_search.py)
- 串行 Agent loop：[`codeagent/runtime/runner.py`](../../codeagent/runtime/runner.py)
- 最近 40 个 event 的上下文重建：[`codeagent/context/builder.py`](../../codeagent/context/builder.py)
- pytest-only command：[`codeagent/runtime/command_service.py`](../../codeagent/runtime/command_service.py)
- 原子 workspace 编辑：[`codeagent/runtime/atomic_edit.py`](../../codeagent/runtime/atomic_edit.py)
- Candidate freeze/apply：[`codeagent/runtime/candidate.py`](../../codeagent/runtime/candidate.py)
- OpenAI placeholder：[`codeagent/model_gateway/openai_client.py`](../../codeagent/model_gateway/openai_client.py)
- 当前实现边界：[当前 Runtime 实现](../ARCHITECTURE.md)
- 自动化测试说明：[TESTING.md](../TESTING.md)

## 5. 主流 Coding Agent 的基础能力基线

### 5.1 定位和读取

基础 Coding Agent 通常能够：

- 读取文件和目录；
- 使用 grep、文件搜索或 codebase search；
- 根据当前任务选择相关上下文；
- 在大仓库中避免无差别读取全部源码。

Cursor 的官方工具清单包括 Read File、List Directory、Codebase、Grep、Search Files 和 Web。Aider 会结合当前对话生成紧凑 repository map，并建议只把任务相关文件放入 chat context。

### 5.2 修改和回退

常见基础能力包括：

- 创建、编辑、删除和移动文件；
- 多文件修改；
- 展示 diff；
- undo、checkpoint 或 Git commit；
- 编辑无法应用时重新定位和 reapply。

Cursor 提供 Edit、Reapply 和 Delete File。Aider 根据模型选择 whole/diff 等编辑格式，并利用 Git commit、diff 和 undo 管理修改。

### 5.3 执行和验证

基础 Coding Agent 需要在权限边界内执行项目已有的验证方法，而不是只支持某个固定测试框架：

- test；
- lint；
- typecheck；
- build；
- 必要的项目脚本。

Aider 支持配置 lint/test/build 命令，并将失败输出反馈给 Agent 继续修复。Cursor Agent 可以使用受控 Terminal。

### 5.4 控制和反馈

主流产品通常提供：

- 工具或命令审批；
- sandbox 或隔离 workspace；
- session resume；
- task progress、命令状态、diff 和测试结果；
- 取消和失败解释。

Claude Code 的 CLI 暴露 allowed/disallowed tools、permission mode、MCP、session resume、max turns 和结构化输出。这些能力并不意味着本项目必须复制 Claude Code，而是说明一个可用 Agent 需要明确的执行控制面。

### 5.5 产品资料

- [Cursor Agent Tools](https://docs.cursor.com/en/agent/tools)
- [Claude Code CLI reference](https://docs.anthropic.com/en/docs/claude-code/cli-usage)
- [Aider repository context](https://aider.chat/docs/faq.html)
- [Aider linting and testing](https://aider.chat/docs/usage/lint-test.html)
- [Aider Git integration](https://aider.chat/docs/git.html)
- [Aider edit formats](https://aider.chat/docs/more/edit-formats.html)

## 6. 实际 JD 调研

### 6.1 样本

| 公司与岗位 | JD 中明确出现的能力信号 |
| --- | --- |
| [OpenAI — Software Engineer, Codex Core Agents](https://openai.com/careers/software-engineer-codex-core-agents-san-francisco/) | sandbox、tool execution、长任务、stateful workflow、测试/debug、token/latency/reliability/cost |
| [Cursor — Software Engineer, Agent Evaluation and Quality](https://cursor.com/careers/software-engineer-agent-evaluation-and-quality) | curated dataset、offline replay、scorer/judge、regression alert、dashboard、用户反馈闭环 |
| [Anthropic — Model Performance Software Engineer, Claude Code](https://job-boards.greenhouse.io/anthropic/jobs/5098025008) | coding-task eval framework、实验基础设施、产品与研究衔接、可靠性、Python/TypeScript |
| [Sift Stack — Software Engineer, Backend (Agentic AI)](https://jobs.ashbyhq.com/siftstack/cab9033c-133d-437d-84ae-d5bc0d6ddec0) | tool interface、sandbox、context/memory、compaction、skills、guardrail、eval、MCP |
| [Rebar — Software Engineer, Agentic Workflows](https://jobs.ashbyhq.com/rebar/dccf8ac4-f2af-4de6-9437-a9787fa7372d) | indexing/retrieval、orchestration、tool calling、eval/observability、cost/latency、UX |
| [Perplexity — Software Engineer, Agent Capabilities](https://jobs.ashbyhq.com/Perplexity/7f2b3619-5ffa-467b-be6f-7a6b7d487892/) | tool calling、long-running tasks、subagents、平台抽象、benchmark、产品判断 |
| [Build — AI Engineer, Harness & Evals](https://jobs.ashbyhq.com/build/cdf0c29b-157e-4b85-a767-e72211022c96/) | structured output、RAG、eval、tracing、permission、sandbox、policy、audit |

### 6.2 共同信号

这些 JD 反复强调的能力大致分为：

1. evaluation、回归和可观测性；
2. 工具设计、orchestration 和可靠执行；
3. 普通软件工程与生产可靠性；
4. context、retrieval 和 state；
5. sandbox、guardrail 和 approval；
6. token、latency 和 cost；
7. MCP、RAG、memory、subagent 等按场景使用的扩展能力；
8. 产品判断、用户反馈和端到端交付。

招聘信号的核心不是“使用了多少 Agent 框架或流行术语”，而是能否让非确定性的 Agent 行为变得可执行、可测量、可调试，并能从真实使用反馈中持续改进。

## 7. 对当前项目的优先级判断

### P0：先让它能用

优先补齐：

- 基于成熟搜索工具的可靠 repository search；
- `git diff` 内容、必要的 Git 历史读取；
- 创建、删除、移动和可靠的多文件 patch；
- 可配置但受控的 command profiles；
- pytest、lint、typecheck 和 build；
- tool timeout、取消、失败回注和清晰结束状态；
- 项目级 Agent instructions。

目标不是工具数量本身，而是让 Agent 能在几个真实仓库中完成小型 bugfix 和 feature task。

### P1：让它能测

采用分层验收，不自建完整 benchmark：

- pytest 验证工具 schema、路径、安全、执行和 artifact 等确定性契约；
- ItsDangerous、Click 和 HTTPX 只承担 3–5 个正式 CLI dogfood 场景，不宣称 benchmark 代表性；
- 外部任务级自动评估优先复用 SWE-bench 的任务、容器环境和 grader，通过薄 adapter 接入正式 Runtime；
- 记录任务状态、测试结果、修改文件、tool calls、turns、latency、token/cost 和 trajectory；
- 将环境、baseline、模型、定位、编辑、验证、Candidate 和 grader 失败分开归因；
- 对 prompt、tool schema、模型和 context 策略在固定外部子集上做回归比较。

现有 pytest 继续验证 Runtime 组件。新的任务级 eval 用来回答“Agent 是否真的完成了开发任务”，两者不能互相替代。

### P1：让它能展示

第一版只需要清晰的 task trace：

```text
用户任务
  → Agent 搜索了哪些文件
  → 读取了哪些代码范围
  → 执行了哪些修改
  → 哪些命令失败或成功
  → 最终 diff
  → 验收结果
```

它可以先是 Rich CLI summary 或静态 HTML，直接消费现有 session events、command artifacts 和 Candidate。这里不预先要求 Project Model、知识图谱或新的 artifact pipeline。

### P2：有评估数据后再优化

- context compaction；
- token/cost budgeting；
- provider 抽象和模型比较；
- MCP client；
- tree-sitter/LSP symbol 工具；
- 更细的 sandbox；
- 插件化 command/tool profiles。

### 暂缓

- 长期 RAG 项目知识库；
- 图数据库；
- multi-agent；
- 完整 IDE；
- 自动生成复杂 Project Model；
- 为了可视化而先建立新的 artifact 体系。

## 8. 建议的下一实践目标

下一阶段围绕以下结果推进：

> 在至少三个具有不同结构的真实或固定样例仓库中，Agent 能自主定位相关代码，完成受控多文件修改，运行项目对应的验证命令，并通过可重复 eval 和可读 task trace 证明任务完成。

已导入的 ItsDangerous、Click 和 HTTPX 固定快照及其场景边界见 [`examples/eval-repos/README.md`](../../examples/eval-repos/README.md)。这些快照只是 repository fixtures，用于开发和 CLI dogfood，不作为正式外部 benchmark。自动任务评估优先复用 SWE-bench 的环境与 grader。

这个目标同时覆盖：

- **能用**：具有真实开发所需的基本工具和闭环；
- **能测**：具有任务级 eval 和回归证据；
- **能展示**：具有 trajectory、diff 和测试结果；
- **求职价值**：对应 JD 中稳定出现的 agent harness、tooling、eval、reliability 和 observability 能力。

具体阶段、边界和验收口径见[近期实践计划](../NEXT_PHASE_PLAN.md)。各组件的 schema、命令安全模型和实现细节仍需通过 Technical Design 分别确认。
