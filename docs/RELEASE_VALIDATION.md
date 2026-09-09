# v0.5.0-rc.1 发布验收

本页记录 `v0.5.0-rc.1` 的冻结条件。正式 HAL 50 题在 tag 创建后运行，因此不属于这里的发布成绩。

## 冻结基线

| 项目 | 值 |
| --- | --- |
| Branch | `feature/v0.4-candidate-loop` |
| Release tag | `v0.5.0-rc.1` |
| Commit | 使用 `git rev-parse v0.5.0-rc.1^{commit}` 获取；tag 与 commit 一一对应 |
| Python package | `codeagent-runtime 0.5.0` |
| Runtime | `codeagent.__version__ == 0.5.0` |
| VS Code Extension | `0.5.0` |
| 冻结后 working tree | clean |

Commit 不能在自身内容中保存自己的哈希，因此本页用注释 tag 作为版本控制内的稳定身份。
发布记录和产物记录应保存命令解析出的完整 commit。

## 自动验收

| 验证 | 状态 | 结果 |
| --- | --- | --- |
| Python full suite | PASS | 发布工作目录：`323 passed, 5 skipped` |
| Clean clone full suite | PASS | `322 passed, 6 skipped`；clone 不含被忽略的本地 task repository，因此多跳过一个 opt-in 用例 |
| Benchmark / Provider 定向回归 | PASS | selection、顺序、持久化、恢复、成本保护、GLM、reasoning 和 accounting：`29 passed` |
| Product / RPC 定向回归 | PASS | `36 passed` |
| Extension | PASS | `node --check extension.js` 和 `npm test` |
| 真实 Bubblewrap | PASS | `5 passed`；实际创建 namespace 并检查文件与网络边界 |
| 真实 Docker projection | PASS | `1 passed`；从 SWE-bench 镜像准备并运行源码 |
| GLM-5.3 Provider smoke | PASS | reasoning → tool → result → continued reasoning → final；usage 和成本完整 |
| GLM-5.3 Docker benchmark smoke | PASS | 单题完成、official grader 正常、结果可恢复且没有重复模型调用 |
| Clean clone / source install | PASS | 从 tag commit 建立全新 clone 和 venv；Fake Runtime 与 RPC smoke 通过 |
| Wheel 独立安装 | PASS | fresh venv 安装后，版本、`codeagent --help` 和 `codeagent-rpc --help` 通过 |
| VSIX 内容 | PASS | 8 个预期文件；版本、入口、commands、views 和 settings 正常 |
| Runtime ↔ Extension | PASS | 解包 VSIX 后以 explicit path 和 PATH 两种方式连接 wheel RPC |

需要显式开启的 Bubblewrap 和 Docker 用例就是 full suite 中的 5 个 skipped。它们已经在真实环境中单独通过。

## 人工 GUI 验收

用户已用 Quick Start 仓库验证安装、模型配置、任务时间线、Approval、隔离修改、验证状态、原生 Diff、
Accept、Discard、Stop / Budget 和 History。History 导航与最后一轮 UI 修复也已回归通过。

人工 GUI 验收状态：**PASS**。

## 发行产物

构建统一设置 `SOURCE_DATE_EPOCH=1704067200`，使 wheel 和 VSIX 可以从干净 checkout 重现。

| 产物 | 路径 | SHA-256 |
| --- | --- | --- |
| wheel | `/home/a1872/projects/code-agent/dist/codeagent_runtime-0.5.0-py3-none-any.whl` | `c90e812563e86fd0348fd310f1d840aaf2057a94e2d895350b1ff11133cba38c` |
| VSIX | `/home/a1872/projects/code-agent/dist/codeagent-0.5.0.vsix` | `69caff23683a56ba2f83361a71d09157a628edef4632ca0cba32e66afd99c3a6` |

VSIX 不包含 Python Runtime。目标机器需要分别安装同版本 wheel 和 VSIX。

## 正式 benchmark 基线

| 项目 | 固定值 |
| --- | --- |
| Benchmark | HAL SWE-bench Verified Mini |
| Selection | [hal-verified-mini-50.json](../benchmarks/swebench/selections/hal-verified-mini-50.json)，固定 50 题 |
| Model | GLM-5.3 |
| Reasoning | enabled，effort `max` |
| 统一资源 | 72 次模型调用；单次输出 8,192；全部难度相同 |
| 顺序 | Easy → Medium → Hard；难度只影响先后 |
| Attempts | 每题一个正常完成结果；中断按固定恢复规则处理 |
| 成本保护 | 下一题开始前检查本地估算余额；低于 10 CNY 停止 |
| Resume | 每题完成后原子保存；恢复跳过 resolved 和正常 unresolved |
| 正式成绩 | PENDING |

旧自定义 50 题和 GLM-5.2 运行只属于 Development。它们不会并入 HAL 正式结果。

GLM-5.3 smoke 机器记录见
[glm-5.3-tool-roundtrip-2026-09-09.json](../benchmarks/swebench/smoke/glm-5.3-tool-roundtrip-2026-09-09.json)。
完整方法和启动命令见[评测方法](BENCHMARKING.md)。

## Release blockers

None。

正式 HAL 50 题仍为 pending。这是 tag 创建后的下一项工作，不阻止 `v0.5.0-rc.1` 冻结。
