# 真实 LLM Agent 测试审计与 Working Context 问题

> 本文保留 HTTPX 失败证据和当时的根因分析。后续 D-2026-08-31-01 没有否定这些观测，但改变了解法：v2 不自动维护稳定 Working Set，而是删除造成双重源码语义的 Active Code，先验证近期 Tool Observation、旧 residue 和 OpenHands 式 condensation。

状态：2026-08-30 调研结论；下一轮 Technical Design 的主要输入
范围：DeepSeek Flash/Pro、HTTPX `NO_PROXY` CIDR 任务、执行切片、工具使用、Context 与 Agent 编排

## 1. 目的与结论

自动化测试已经证明工具 schema、权限、安全、编辑事务、命令、验证、Session 和 Context projection 的确定性接入正确。本轮使用真实 LLM 的目的，是观察 Agent 能否在连续工具操作中维持任务认知并完成真实修复，不把模型是否聪明纳入 Runtime 单元测试。

五次 HTTPX 测试得到的核心结论是：当前瓶颈不只是模型能力或 Context token 容量，而是 Working Context 编排不稳定。模型已经多次形成足够正确的修复方案，却因源码工作集随“最近一次工具结果”移动、旧闭合工具协议持续累积、缺少稳定执行 checkpoint 而反复探索，最终没有编辑并耗尽步骤或输入容量。

在解决这一问题前，不应继续通过提高 48-step 上限、扩大默认 token budget、禁止 `sed/cat` 或堆叠防呆提示来掩盖根因，也不应把 SWE-bench adapter 作为下一实现优先级。

## 2. 测试任务

任务要求 HTTPX：

- `NO_PROXY` 中 IPv4/IPv6 CIDR 只匹配网段内 IP；
- 保持 hostname、通配域名、scheme 和 port pattern 行为；
- 覆盖 IPv4 `192.168.0.0/16` 与 IPv6 `2001:db8::/32` 的范围内/外测试；
- 使用标准库地址语义，不联网、不安装依赖；
- 运行指定 `tests/test_utils.py` 定向 pytest。

仓库规模和修改面均较小，正常预期是读取 `_utils.py` 与相关测试、确认 URL/CIDR 表达、修改一至两个文件并验证。该任务不应依赖 extended thinking 才能完成。

## 3. 五次 Session 对比

| Session | 模型/Runtime 阶段 | User messages | Model steps | Tool calls | 主要调用 | 编辑 | 结果 |
| --- | --- | ---: | ---: | ---: | --- | ---: | --- |
| `5429e4c7c19a` | Flash；旧事件窗口 | 4 | 48 | 70 | read 37 / search 20 | 2 | 依赖三条“继续”；产生错误 patch，未验证 |
| `824083396f22` | Flash；Context v1 + execution slices | 2 | 48 | 54 | read 30 / search 11 / command 8 | 0 | 原任务保持在同一 UserTurn；48-step 终止 |
| `66fae4e48b25` | Pro；旧 Prompt/read schema | 1 | 48 | 65 | read 35 / search 28 | 0 | 精确重复 15 次并调用不存在的 `grep_found`；48-step 终止 |
| `a4dca0d1a266` | Pro；新 Prompt/read schema | 1 | 40 | 53 | read 23 / search 27 | 0 | read 边界误解明显减少；Context build 未捕获异常中断 |
| `2f6ce17a6bde` | Pro；Search schema + CLI capacity fix | 1 | 35 | 41 | read 18 / command 20 | 0 | 基线测试通过、方案基本正确；`23024 > 22000` 明确终止 |

上述计数来自各 Session 的 `events.jsonl`；完整 Session 和 worktree 不复制进项目文档。

## 4. 已确认有效的修复

### 4.1 UserTurn 与 execution slice 分离

旧 Session `5429e4c7c19a` 每 12 步写入伪 Final，用户必须发送三次“继续”，导致最近自然语言上下文只剩无语义消息。新实现让 12-step slice 只成为控制点，同一原始 UserMessage 可连续执行，交互 `/continue` 和非交互自动续片不新增用户消息。

证据：`824083396f22` 的原任务在 48 个 ModelStep 内始终属于同一 UserTurn，原始约束没有再被“继续”挤出。

### 4.2 Prompt 不再要求每次必须调用工具

旧 Prompt 的“只能提出工具调用请求”可能诱导模型即使已有结论也继续选择安全的 read/search。新 Prompt 改为所有 workspace 操作必须通过工具，但允许直接分析、编辑、验证或 Final，并减少 Runtime 内部术语。

### 4.3 `read_file` 范围语义明确化

旧结果在显式部分读取时可能返回 `total_lines=null`，模型多次把自己请求的 `end_line=70/119/139` 误认为文件末尾。现在始终提供 requested/returned range、file total lines、前后是否还有内容和完整 SHA。

证据：`a4dca0d1a266` 不再声称 242 行文件只有 119 行；完全相同调用的重复 surplus 从前一次 Pro 的 15 降为 3。优化有效，但没有解决行动收敛。

### 4.4 `search_text` literal/regex 显式化

Pro 曾多次提交含 `|` 的 query 却未设置 `mode=regex`，Runtime 按默认 literal 返回零结果，模型随后反复改写搜索。现在 schema 明确默认 literal，regex 必须显式选择，结果回显实际解释模式。

### 4.5 Context 容量失败可理解

`a4dca0d1a266` 在第 41 次模型请求前 Context build 抛出未捕获异常，Session 留在含糊 incomplete 状态。现在 Runner 写入 `turn_terminated(reason=context_budget_exceeded)`，CLI 区分单次模型输入容量与 Session 累计额度，并保留 workspace。

证据：`2f6ce17a6bde` 正确记录 `estimated_tokens=23024`、`usable_tokens=22000` 和 `steps_used_in_turn=35`。

## 5. 尚未解决的根因

### 5.1 Active Code 是移动聚光灯，不是稳定 Working Set

当前同一路径的 evidence 只保留最后一个 range。最新一次 `read_file` 正文完整保留，更早结果转 residue；后读的小范围会覆盖此前已覆盖的互补范围。

`2f6ce17a6bde` 在前三个 ModelStep 已覆盖：

```text
httpx/_utils.py 1–242
tests/test_utils.py 1–150
CODEAGENT_TASK.md complete
```

但后续再读 `_utils.py 1–60` 后，模型醒目可见的当前源码主要变成 1–60；先前 60–242 只剩“曾读取成功”的 residue。模型因此声称旧 read 内容被截断，并改用 `sed/cat` 重建当前视野。

### 5.2 Tool History 代替了代码工作集

工具历史应回答“发生过什么”，Working Set 应回答“下一步需要看什么”。当前源码正文主要依附在最新 tool result 上，导致每执行一个新观察，旧代码就退出工作视图。模型选择 `run_command` 读取不是任意命令设计本身错误，而是在绕开不稳定的 read 工作集。

命令 stdout 也只在最近结果中较完整；下一条命令又使前一条转 residue，于是出现 20 次 `sed/cat/python` 循环。

### 5.3 同时保留了成本，丢失了价值

active UserTurn 的所有闭合 assistant tool-call/result 协议对当前都保留，确保 provider pairing；但旧 read/search/command 正文已经缩减。结果是 Context 仍为几十组 call id、arguments、metadata 和 policy 摘要付出 token，却没有一个稳定的合并源码视图。

### 5.4 缺少稳定执行 checkpoint

模型多次公开输出：已经理解完整流程、找到 bug、基线通过、现在开始实现，并给出基本正确方案；下一步仍回到确认细节。当前 continuation 只说明“同一 UserTurn、不要从头开始”，没有稳定表达：

```text
当前阶段
已确认的关键事实
当前 Working Set
当前修改/验证状态
下一项直接动作
```

这不应成为 Runtime 权威业务事实或大量持久 artifact，但需要设计可 Resume、可校验、不会被最近工具调用覆盖的短期执行状态。

### 5.5 容量是结果，也是独立问题

`22000` 是下一次模型请求的输入预算，不是 Session 累计 token。`2f6ce17a6bde` 的 20 次 command 还携带长 shell/Python arguments、status、stdout/stderr tail 和 Effective Policy，使 active UserTurn 最低集合在 35 个 ModelStep 时达到 23024。

只增加输入预算会延迟失败，却不会阻止模型继续重建工作现场。

## 6. 下一轮 Technical Design 范围

建议下一轮命名为 **Agent Orchestration / Working Context Technical Design**，统一设计而不是继续局部修补：

1. Stable Working Set：同一 SHA 下多个相关 range 的合并、优先级、容量和文件变化失效；
2. Execution Checkpoint：阶段、已确认事实、当前修改/验证与下一动作的来源、可信度和 Resume 语义；
3. Tool History 与 Working Context 分离；
4. active UserTurn 内旧闭合 ToolExchange 的成对缩减和 provider 合法性；
5. slice 边界如何继续稳定工作，而非只继续同一 UserMessage；
6. command/read/search 输出如何进入同一 Working Set，而不是按工具各自形成短命正文；
7. 提前容量触发、目标释放量和 BudgetReport；
8. Fake Model/fixture 如何验证编排正确，不用真实 LLM 作为单元测试 oracle；
9. artifact 边界：优先内存派生和现有 Event 的紧凑 checkpoint，不新增目录家族；
10. 与未来 SWE-bench adapter/LangGraph 的边界。

## 7. 非目标与风险控制

- 不把模型生成的代码结论直接提升为 Runtime 权威事实；
- 不建立复杂 Task 实体、多 Agent planner 或通用知识库；
- 不禁止任意命令中的 `sed/cat`，安全仍由 Sandbox 强制；
- 不通过无限保留源码正文解决遗忘；
- 不先接入 LangGraph 再寻找状态语义；
- 不在设计完成前继续提高 step/token 默认值。

## 8. 关联资料

- [Context Management TD](../design/CONTEXT_MANAGEMENT_TECHNICAL_DESIGN.md)：当前 v1 契约与已暴露限制；
- [Context Capacity Research](../research/CONTEXT_CAPACITY_RESEARCH_2026-08-30.md)：Anthropic/OpenAI/LangGraph 的 trim/tool clearing/compaction 调研；
- [ARCHITECTURE](../../ARCHITECTURE.md)：当前已实现事实；
- [NEXT_PHASE_PLAN](../../NEXT_PHASE_PLAN.md)：下一轮设计顺序。
