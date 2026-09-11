# v0.5.0 发布检查清单

本文记录 v0.5.0 的最终发布门禁和实际验证结果，不承担架构设计或 benchmark 方法说明。检查日期为
2026-09-11；Evaluation Candidate 为 `932b52ffff4209a748c6646a056d2ab43111637e`。

## Evaluation 与代码冻结

- [x] 正式 HAL SWE-bench Verified Mini 50 已完成，`batch-summary.json.finished=true`；
- [x] selection、50 题分母、pinned repositories 和 Candidate commit 与冻结配置一致；
- [x] 50 题均有唯一完成结果，35 resolved、13 unresolved、2 budget exhausted；
- [x] official grader report 与 task artifact 对齐；
- [x] usage、main/condenser、成本和停止原因统计完整；
- [x] partial、diagnostic 和 calibration artifact 未混入正式结果；
- [x] README、评测报告和机器可读证据使用同一份正式 artifact；
- [x] Candidate 之后 `codeagent/` 与 `tests/` 无差异，发布收尾未修改 Runtime 行为。

正式结果、运行身份与证据哈希见[评测结果](EVALUATION_RESULTS.md)和
[机器可读摘要](evidence/v0.5.0-hal-mini-50.json)。

## 文档与仓库

- [x] README 按产品定位、快速开始、工作流、工程亮点、评测证据和边界重新组织；
- [x] CHANGELOG、安装、Benchmark 和评测文档只描述已实现、已验证的能力；
- [x] Architecture、Context、RPC 文档不包含本轮新增承诺；
- [x] 14 个发布相关 Markdown 文件的相对链接检查通过；
- [x] README 引用的四张产品截图均存在；
- [x] tracked 文档未发现开发机绝对路径、API key、临时 TODO 或 FIXME；
- [x] `git diff --check` 通过；
- [x] 本轮变更范围仅为发布文档、包元数据和机器可读评测摘要。

当前工作树包含本轮待审核的发布整理，这是预期状态。创建 release commit 后仍需在 clean checkout 重复版本、链接和产物哈希
检查；release commit 与 Candidate commit 可以不同，但 production code tree 必须继续保持一致。

## 自动化与隔离验证

- [x] Python full suite：381 passed、5 skipped；
- [x] Extension tests：1 passed；
- [x] `compileall` 通过；
- [x] 产品元数据与文档定向检查：16 passed；
- [x] opt-in 真实 Bubblewrap integration：5 passed；
- [x] 正式 HAL 50 已实际覆盖 Docker task projection，50 题均产出 patch，未出现 infra error；
- [x] 机器可读摘要中的结果、outcome、usage 与 cost 已和正式 artifact 机械核对。

这里没有为发布收尾重新运行付费 benchmark，也没有仅为重复证据额外执行 Docker 批次。

## Release Artifact

产物位于本地 `dist/`，该目录按项目策略不纳入 Git。两项产物均由当前发布树重新构建，不复用历史 RC 文件。

| 产物 | SHA-256 |
| --- | --- |
| `codeagent_runtime-0.5.0-py3-none-any.whl` | `2a41bf401dc4537e8f444ad3b1828fd1a264130e5507bebd58fd6d8a66b4fbc9` |
| `codeagent-0.5.0.vsix` | `3c624b46a729d332af8683897ce9fdd6fd8b84ca80fc8de483c813f58d58ef1c` |

- [x] wheel 内容审计：包含 Runtime、License 和完整包元数据，不含测试、缓存、session、密钥或评测运行输出；
- [x] fresh venv 安装 wheel，`pip check` 无 broken requirements；
- [x] fresh venv 中版本为 0.5.0，`codeagent --help` 与 `codeagent-rpc --help` 通过；
- [x] installed wheel 使用 Fake provider 完成一次只读工具调用和最终回答；
- [x] VSIX 内容审计：仅包含扩展运行文件、图标、README、License 和 manifest；
- [x] VSIX 安装到隔离的 Extension/User Data 目录，注册版本为 0.5.0；
- [x] packaged Extension 到 fresh-wheel RPC 的显式路径与 PATH 连接 smoke 均通过，缺失 executable 能正确拒绝；
- [x] `dist/SHA256SUMS` 对两个文件校验通过；
- [x] wheel 与 VSIX 未发现密钥或开发机绝对路径。

VS Code 的完整可视化流程已在代码冻结前人工审核；本轮只验证重新打包后的内容和 Runtime 连接，没有重复声称执行 GUI 人工验收。

## 版本一致性

- [x] `pyproject.toml`：0.5.0；
- [x] `codeagent.__version__`：0.5.0；
- [x] Extension `package.json`：0.5.0；
- [x] README、安装文档、CHANGELOG 与评测文档：v0.5.0；
- [x] Python 与 VSIX 元数据包含 License、Repository、Homepage 和 Issues 链接。

## 发布操作

以下是审核通过后的发布动作，不是代码或文档缺陷：

- [ ] 审核并提交本轮 release-only 变更；
- [ ] 从最终 release commit 的 clean checkout 复核版本、链接和 checksum；
- [ ] 创建并推送 `v0.5.0` tag；
- [ ] 创建 GitHub Release，上传 wheel、VSIX 和 `SHA256SUMS`；
- [ ] 在 Release Notes 中同时记录 Candidate SHA、release SHA、HAL 结果与 artifact checksum。

在上述发布动作完成前，不应把本地构建产物描述为已经公开发布。

## 已知边界

- HAL Mini 50 是固定 50 题样本，35/50 不能外推为完整 SWE-bench Verified 排名；
- 难度标签来自项目固定 selection 的启发式元数据，不是官方难度分层；
- 15 个未通过任务中，12 个属于方案或规格理解偏差，2 个耗尽 72 步预算，1 个在官方测试补丁应用阶段冲突；
- 单次正式运行成本约 ¥150.04，只是该配置和该轮运行的实测，不是固定产品价格；
- v0.5.0 仍是本地单任务 Runtime，不提供后台队列、远程执行、多租户或无人值守自治。

在不继续修改 production code 的前提下，当前状态可进入 release commit、clean-checkout 复核、打 tag 和上传产物阶段。
