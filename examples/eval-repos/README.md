# L1 真实仓库评估样例

本目录保存三个来自 GitHub 的固定仓库快照，用于设计和验证比算法题更接近真实开发的 Coding Agent 任务。

这些目录本身只是 **repository fixtures**。首轮人工任务、准备脚本和 workspace 外 oracle 已放在 [`../eval-tasks/`](../eval-tasks/README.md)；它们是 dogfood fixtures，不等同于正式 benchmark 数据集。

## 快照来源

| 目录 | 上游仓库 | 固定 commit | 许可 | 快照规模 | 主要评估场景 |
| --- | --- | --- | --- | --- | --- |
| `itsdangerous/` | [pallets/itsdangerous](https://github.com/pallets/itsdangerous) | `672971d66a2ef9f85151e53283113f33d642dabd` | BSD-3-Clause | 50 个文件，约 1.4k 行 Python | 从算法题过渡到多模块 library；序列化、签名、错误语义和 feature change |
| `click/` | [pallets/click](https://github.com/pallets/click) | `68e7ea7228ca144c52e4d1d282cc09da59f7771f` | BSD-3-Clause | 166 个文件，约 23k 行 Python | CLI 参数解析、命令行为、多文件修改、typing、lint 和文档 |
| `httpx/` | [encode/httpx](https://github.com/encode/httpx) | `b5addb64f0161ff6bfe94c124ef76f6a1fba5254` | BSD-3-Clause | 125 个文件，约 14k 行 Python | HTTP 配置、代理、timeout、sync/async、CLI 和本地网络边界 |

导入日期为 2026-08-25。快照保留上游 LICENSE、源码、测试、文档和项目配置，移除了上游 `.git/` 元数据。不要把这里的文件描述为本项目原创代码。

## 为什么不只评估 bugfix

第一批任务应该覆盖不同的软件工程动作，而不是三个形式相同的缺陷修复：

| 场景 | 建议任务形态 | 主要暴露的 Agent 能力 |
| --- | --- | --- |
| Bugfix | 根据行为描述定位并修复跨文件回归 | 搜索、证据读取、失败诊断、最小修改 |
| Feature | 增加公开参数、配置项或小型行为，并同步测试和文档 | change planning、多文件编辑、创建内容、兼容性判断 |
| Command workflow | 除 pytest 外还要求 lint、typecheck、build 或 CLI smoke test | 受控 command profile、输出处理、失败恢复 |
| HTTP/configuration | 修改 proxy、timeout、header、transport 或环境配置行为 | 配置传播、同步/异步路径、边界条件、局部网络测试 |
| Maintenance | 更新 deprecated API、错误信息、typing 或项目配置 | 全局搜索、影响范围、机械修改与语义验证 |

## 命令与网络边界

“命令执行”已经通过 `run_command + Bubblewrap` 进入评估范围。Agent 可以提交任意 shell string，但实际文件系统、网络和进程副作用仍由 Runtime Policy 与 sandbox 强制限制。三个仓库提供了不同的验证入口：

- `itsdangerous`：pytest、ruff/pre-commit、mypy、pyright 和文档构建；
- `click`：pytest、ruff/pre-commit、mypy、pyright、文档构建；
- `httpx`：pytest、ruff、mypy、package build，以及可选 CLI。

“网络配置任务”也不等于允许 benchmark 依赖真实外网。默认验收应满足：

- 依赖在环境准备阶段安装，不由 Agent 临时联网安装；
- HTTP 行为优先通过 mock transport、本地 server、WSGI/ASGI app 或 loopback 验证；
- `httpx` 中标记为 `network` 的真实联网测试默认排除；
- 如果未来专门评估网络工具权限，应作为独立 capability suite，并显式记录 allowlist、DNS、凭据和审计策略。

这样可以区分三件事：

1. Agent 是否能修改网络相关代码；
2. Agent 是否能运行本地、确定性的网络测试；
3. Agent 本身是否获准访问外部网络。

第三项不是前两项的前置条件。

## 使用约束

这些快照没有嵌套 `.git/`，不能直接作为 v0.4 execution workspace。评估 harness 应当：

1. 把目标快照复制到一次性临时目录；
2. 在临时目录初始化 Git，并提交固定 baseline；
3. 安装该任务声明的固定依赖；
4. 应用任务专用的初始 patch 或选择固定历史状态；
5. 通过正式 `AgentRunner` 和 `ToolRegistry` 运行 Agent；
6. Agent 结束后运行独立 hidden tests 和 policy checks；
7. 丢弃临时工作目录，保留 run manifest、trajectory、patch 和结果。

不要让 benchmark adapter 直接替 Agent 修改源码，也不要向 Agent 暴露 hidden tests。

## 后续正式 benchmark 工作

人工任务已经可运行，但导入仓库仍不等于正式 benchmark 已经可用。后续需要：

- 从每个仓库选择少量、可重复、规模适中的具体任务；
- 确认任务能在固定 Python 和依赖版本下离线运行；
- 为任务建立 baseline、hidden oracle 和超时；
- 用当前 v0.4 跑出失败基线；
- 根据失败类型决定搜索、编辑、命令和 context 能力的增量。

在这些数据出现前，不因为 HTTPX 较大就预先引入 LSP、RAG 或 repository index。
