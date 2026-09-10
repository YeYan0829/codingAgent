# v0.5.0 发布检查清单

本文只记录机械发布门禁，不承担架构、设计或 benchmark 方法说明。当前阶段是：Runtime 与 Extension 已冻结为
Evaluation Candidate，正式 HAL SWE-bench Verified Mini 50 结果待运行；尚未创建最终 tag 或 release artifact。

## Evaluation Candidate 门禁

正式 HAL 50 启动前必须满足：

- [x] validation pipeline 对 `purpose=validation` 启用 Bash `pipefail`；
- [x] Context semantic condensation、reasoning 和 truncation recovery 实现及定向回归完成；
- [x] VS Code Extension / timeline 人工验收完成；
- [x] 当前架构、Context、命令环境、RPC、安全、测试和 benchmark 文档与代码一致；
- [x] 已实现 TD 退出 active docs；
- [x] Python full suite、Extension tests、`compileall` 和 `git diff --check` 通过；
- [x] Candidate commit 后 working tree clean；
- [ ] 从最终 Evaluation Candidate SHA 启动 HAL 50。

Candidate SHA 和本轮实测数量记录在冻结报告中；commit 不能在自身内容里保存自己的 SHA。正式运行的
`run-manifest.json` 必须再次记录同一个 commit 且 `runtime.dirty=false`。

## 正式 HAL 50 完成门禁

- [ ] `run-manifest.json.finished_at` 与 `batch-summary.json.finished=true`；
- [ ] selection ID、SHA、50 题分母和 pinned repositories 与冻结配置一致；
- [ ] 每题都有唯一 completed result，或在最终报告中明确合法停止原因；
- [ ] official grader report path 与 task artifact 一致；
- [ ] usage coverage、main/condenser 分项和成本状态完整说明；
- [ ] partial、diagnostic 和 calibration artifact 未混入正式结果；
- [ ] `docs/EVALUATION_RESULTS.md` 填入由 artifact 机械导出的最终数字；
- [ ] README benchmark headline、成本和证据链接与同一 artifact 一致。

如果 HAL 暴露 Runtime、Context、tool、validation、reasoning、prompt 或 harness correctness bug，应停止并标记本轮
evaluation，不得边修改 production code 边继续累计结果。

## 发布文档门禁

HAL 完成后允许进行 documentation-only closure：

- [ ] README 更新项目定位、Quick Start、最终 HAL 指标和证据链接；
- [ ] `docs/EVALUATION_RESULTS.md` 区分 final、diagnostic、calibration；
- [ ] CHANGELOG / release notes 只写已实现功能和最终确认结果；
- [ ] Architecture、Context 和 RPC 不出现设计阶段状态或未实现承诺；
- [ ] 所有 Markdown 相对链接有效；
- [ ] tracked 文档无开发机绝对路径、secret 或临时 TODO；
- [ ] release commit 与 Evaluation Candidate 的 production code tree 相同。

若 HAL 后只有文档变更，最终 release commit SHA 可以不同于 Evaluation Candidate SHA，但发布记录必须同时列出两者并证明
其 production code diff 为空。

## 安装与产物门禁

从最终 release commit 的 clean clone 执行：

- [ ] Python source install；
- [ ] `codeagent --help` 与 `codeagent-rpc --help`；
- [ ] Python full suite；
- [ ] opt-in 真实 Bubblewrap smoke；
- [ ] 必要的真实 Docker projection smoke；
- [ ] wheel build、fresh venv install 和版本检查；
- [ ] VSIX build、内容清单和版本检查；
- [ ] Runtime ↔ packaged Extension 连接 smoke；
- [ ] Extension 人工 smoke：配置、任务、Approval、timeline、Diff、Accept/Discard、Stop/预算、History；
- [ ] wheel 与 VSIX SHA‑256 保存到 release notes。

构建命令和环境要求见[安装与运行](INSTALLATION.md)，自动化测试范围见[测试说明](TESTING.md)。不要复用历史 RC 产物的
checksum 作为最终 release 证据。

## 版本与仓库门禁

- [ ] `pyproject.toml`、`codeagent.__version__`、Extension `package.json` 和用户文档版本一致；
- [ ] `git status --short` 为空；
- [ ] `git diff --check` 通过；
- [ ] tracked files 不包含 session、API key、本地缓存、临时回放 repo 或未分类 benchmark 输出；
- [ ] public repository 中的 license、安装命令、图片和链接有效；
- [ ] release tag 指向最终 release commit；
- [ ] tag、GitHub release、artifact checksum 和 benchmark evidence 相互引用同一版本。

## 当前已知发布边界

以下不是 HAL 启动 blocker，但必须在最终发布前关闭：

- 正式 HAL 50 结果仍是 **PENDING**；
- README 最终 benchmark 指标与发布叙事尚未更新；
- 最终 wheel/VSIX 尚未从 release commit 重建；
- 最终 tag 和 GitHub Release 尚未创建。

评测方法见[Benchmark 文档](BENCHMARKING.md)，当前系统事实见[架构](ARCHITECTURE.md)。
