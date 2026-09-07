# SWE-bench 评测

SWE-bench 用真实开源仓库的 bug fix 检查 Coding Agent。CodeAgent 使用官方任务镜像和 official grader。

本页解释评测如何固定环境、保存运行记录和计算结果。已有数字见[评测结果](EVALUATION_RESULTS.md)。

## 先理解四个概念

**固定题集（selection）** 是一次评测要运行的有序题目列表。它还记录每道题使用的 Docker 镜像和 Git 版本。

**准备后的源码** 是从官方镜像 `/testbed` 导出的仓库。CodeAgent 会记录它的 commit 和文件树，
避免下次运行时换成不同代码。

**运行记录（run manifest）** 保存本批次的代码版本、模型设置和题集哈希。
它用于判断两次运行是否可以比较。

**official grader** 是 SWE-bench 的官方评分程序。它读取 Agent 生成的 patch，判断问题是否解决。

## 一道题如何运行

```text
读取固定题集
→ 从指定 Docker 镜像导出 /testbed
→ 核对 Git commit、文件树和镜像 digest
→ 创建宿主 Git worktree
→ 在一次性 Docker container 中运行 Agent 命令
→ 导出最终 patch
→ 使用 official grader 评分
→ 保存结果、token、成本和执行摘要
```

搜索、读取和编辑文件都使用宿主 Git worktree。这个目录保存 Agent 对当前题目的修改。

每条项目命令会启动一个新的 Docker container。宿主 worktree 挂载到容器的 `/testbed`，
所以命令产生的文件变化会返回同一个 worktree。

Docker 只替换命令执行环境。Agent loop、文件工具和修改规则仍使用本地 Runtime。

评测不会经过 VS Code 的人工 Accept。harness 使用自动审批，因此不能用它证明产品界面的审批流程。

## 题目和环境如何固定

selection JSON 记录以下内容：

- SWE-bench suite 和上游版本
- 有序的 instance id
- 难度和仓库分布
- official image digest
- 数据集中的 base commit
- 导出后源码的 HEAD 和文件树

镜像使用 digest 固定。源码直接从镜像中的 `/testbed` 导出，不会重新 clone 仓库并猜测依赖。

官方答案和测试列表只用于环境检查与评分。harness 不会主动把这些内容加入 Agent 的模型输入。

当前公开题集：

- [开发十题](../benchmarks/swebench/verified-first-10.json)
- [两题计量 smoke](../benchmarks/swebench/verified-smoke-2.json)
- [正式固定 50 题](../benchmarks/swebench/verified-eval-50.json)

固定 50 题已经生成，但正式模型运行仍为 **pending**。题集文件不是模型成绩。

## Gold preflight

Gold preflight 会把官方答案应用到官方环境，再运行 official grader。

只有官方答案能够通过的题目，才会进入固定 50 题。这一步用于排除坏镜像或坏测试环境。

它不能证明 CodeAgent 可以解决这些题，也不能作为 Agent resolved 成绩。

固定 50 题的生成规则是：

- 从 SWE-bench Verified 中排除开发十题。
- 使用 seed 42。
- 按剩余题目的难度比例分配数量。
- 每个难度内先排序，再使用固定 seed 打乱。
- 单个代码仓库最多占 30%。
- 每道题必须通过 gold preflight。

最终题集包括 19 Easy、26 Medium 和 5 Hard，覆盖 10 个仓库。

它应称为“SWE-bench Verified 自定义固定 50 题子集”。
它不是全量 Verified 成绩，也不是官方榜单成绩。

## Agent 结果和 grader 结果

Agent 给出最终回复，不代表 bug 已经修好。Agent 自己运行的测试通过，也不代表 official grader 一定通过。

因此，每道题分别记录：

| 字段 | 普通含义 |
| --- | --- |
| `agent_status` | Agent 是正常结束、预算耗尽，还是运行失败 |
| `validation_passed` | Agent 自己的测试命令是否对最终修改成功 |
| `patch_present` | 是否产生非空 patch |
| `oracle_status` | official grader 是否正常完成 |
| `oracle_passed` | official grader 是否判定问题已经解决 |

正式报告不会把这些字段合并成一个模糊的“成功”。

## 批次恢复

`codeagent swebench-batch` 按固定顺序逐题执行。每道题开始前，会先写入 `running` 状态。

完成后，结果会原子写入任务目录。单题异常不会删除此前已经完成的结果。

使用相同 `--run-id` 可以恢复批次。恢复时会检查题集哈希和模型配置是否相同。

已经完成的记录还必须保留 prediction、执行摘要和唯一的会话事件文件。
条件满足后，该题会被跳过。

当前恢复检查没有覆盖所有文件内容哈希，也没有覆盖 grader 命令和价格快照。
一个已经完成但 grader 未通过的题目也会被复用。

异常或不完整任务可以重新执行，但旧尝试没有形成完整账本。
因此，当前批次汇总不能完整统计所有重试成本。

## 运行命令

下面的变量都由运行者提供：

- `TASK_REPO`：固定 commit 的 SWE-bench 任务数据仓库。
- `SOURCE_CACHE`：从官方镜像导出的源码缓存。
- `RUNS_DIR`：本批次的输出目录。
- `GRADER_COMMAND`：已安装的 official grader 命令前缀。

示例：

```bash
codeagent swebench-batch benchmarks/swebench/verified-smoke-2.json \
  --task-repo "$TASK_REPO" \
  --source-cache "$SOURCE_CACHE" \
  --output-root "$RUNS_DIR" \
  --provider glm --model glm-5.2 \
  --grader-command "$GRADER_COMMAND" \
  --report-template "$RUNS_DIR/grader-reports/codeagent__glm-5.2.{run_id}.json" \
  --price-snapshot benchmarks/swebench/prices/glm-5.2-standard-api-2026-09-04.json \
  --run-id example-smoke
```

CLI 会拆分 `GRADER_COMMAND`，再追加 prediction、instance id 和 run id 参数。
它不会自动安装 SWE-bench。

`--report-template` 必须匹配当前 grader 版本的实际报告路径。
可以使用 `{run_id}` 和 `{instance_id}` 占位符。

先用 smoke 验证路径和环境。恢复正式批次时，不要更换 `--run-id` 或实验配置。

## 模型和 Runtime 身份

run manifest 会记录以下信息：

- Runtime 版本、Git commit 和工作目录是否 dirty
- 模型 provider、model、endpoint、temperature 和输出上限
- 上下文上限与模型步骤预算
- selection 文件哈希
- SWE-bench 上游 commit
- 每道题的镜像和准备后源码身份

“身份”在这里指用于确认运行条件的一组客观值。配置 fingerprint 是这些设置的哈希摘要。

当前自动恢复不会检查 Runtime commit、grader 配置和价格快照的全部身份。
正式评测必须从干净 checkout 运行，并在每次恢复前额外核对这些字段。

## Token、成本和执行摘要

Runtime 会记录 provider 为每次模型响应返回的 token 用量。

单题和批次报告会汇总：

- 模型请求数，以及其中有用量记录的请求数
- input、output 和 total token
- provider 提供时的 cached input、cache miss 和 reasoning token
- 用量记录是完整、部分可用还是不可用

未知值保存为 `null`，不会按零处理。

成本使用独立价格快照计算。报告保存币种、费率、来源和生效日期。

如果 token 或费率不完整，只会报告已知部分，不会把它写成完整总成本。

执行摘要从会话事件中确定性计算。它记录模型调用次数、终止原因、工具类型、文件修改和重复操作。

执行摘要不使用 LLM judge，也不判断修改是否聪明或测试是否充分。

## 时延记录

命令结果已经记录开始时间、结束时间和持续毫秒数。批次记录也有整体起止时间。

这些数据还不能分开表示源码准备、Agent 执行和 grader 三个阶段。
因此，当前无法生成正式报告要求的逐题分阶段中位数和 P90。

## 正式报告要求

正式 50 题报告至少需要公开：

- 固定题集、Runtime commit、模型和步骤预算
- Docker 镜像、SWE-bench 上游版本和 grader 版本
- 每次初始运行与恢复运行
- 每题 patch 哈希、grader 结果和失败类别
- 解决题数、正常结束率和完整交付率
- 所有运行的 token、成本和计量完整度
- 三个阶段的时延和机器条件
- 去除凭据与本机路径的 JSON、CSV 和可读报告

当前缺少完整尝试账本、分阶段时延和公开报告映射。
在补齐这些条件前，不会把固定 50 题运行称为正式结果。

这些条件只阻塞正式评测报告，不阻塞一个不宣称正式成绩的产品 RC。

## 测试入口

- 源码准备：`tests/test_swebench_source_preparation.py`
- Docker 投影：`tests/test_swebench_docker_executor.py`
- 真实官方镜像：`tests/test_swebench_docker_integration.py`
- Gold preflight：`tests/test_swebench_preflight.py`
- harness 和 grader：`tests/test_swebench_harness.py`
- 固定题集：`tests/test_swebench_selection.py`
- 批次、计量和摘要：`test_swebench_batch.py`、`test_benchmark_accounting.py`、`test_trajectory_analyzer.py`

真实 Docker 测试默认跳过。运行方式见[测试说明](TESTING.md)。
