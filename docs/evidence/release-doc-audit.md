# v0.5.0 正式文档审校验证记录

日期：2026-09-07。对象为 HEAD `7faf6ff55eea99d1cfeb90ced02c67d13b9b880c` 加 dirty 工作区，
不是冻结 release commit。本文是本轮执行摘要，不是 CI attestation；正式产物应在干净 checkout 重建并公开日志。
没有执行付费模型或正式 50 题评测。

## Fact audit

| 类别 | 发现与处理 | 当前事实来源 |
| --- | --- | --- |
| A：事实不一致 | 创建 worktree 的干净仓库前提、Stop/timeout 区别、validation 两层口径、源树并发保护范围均补充 | `workspace/git_worktree.py`、`runtime/runner.py`、`runtime/sandbox_executor.py`、`runtime/validation.py`、`product/read_model.py`、`runtime/candidate.py` |
| B：亮点过度简化 | README 改为命令权限组合、逐轮上下文重建、验证交付、可干预执行、环境固定与评测计量 | 当前 Runtime、context builder、Product、benchmark 实现及相应测试 |
| C：内部术语 | 产品流程采用会话/请求/模型步骤；对象关系集中定义于架构和 RPC | README 与九份正式文档交叉审查 |
| D：安装链路 | auto 是文件存在探测；支持 executable/PATH；握手返回版本未校验；原 VSIX 构建命令缺文档 URL 基址 | `vscode-extension/extension.js`、`rpcClient.js`、`package.json`、真实构建和 headless 连接 |
| E：证据不足 | 未验证的 GUI、冻结产物与正式评测保持 pending；开发成绩表缺身份则撤下 | [发布验收](../RELEASE_VALIDATION.md)、[评测结果](../EVALUATION_RESULTS.md) |

额外修正：安装 demo 切换目录后需返回源码目录；`CODEAGENT_TEST_BWRAP` 是路径而非 `1`；
CI 文件存在不等于 CI 已执行；VSIX 补入根目录现有 MIT 文本。所有变更限于文档、证据摘录和许可证，未修改功能。

## 本轮执行

| 检查 | 实际结果 | 证据范围 |
| --- | --- | --- |
| `.venv/bin/python -m pytest -q` | 302 passed、3 skipped，25.92s | 默认确定性套件；skip 为两个 bwrap 和一个 Docker 用例 |
| Extension `npm test` | 1 个测试文件通过 | Node runner 汇总口径，不能解释为只有一个交互断言 |
| `CODEAGENT_TEST_BWRAP=/usr/bin/bwrap` 的集成测试 | 2 passed，3.24s | 初次在外层沙盒中 namespace 失败，获准宿主重跑后通过 |
| `CODEAGENT_RUN_SWEBENCH_DOCKER=1` 的集成测试 | 1 failed，2.01s | 当前 WSL 的 Docker Desktop integration 不可用；未运行 grader |
| wheel build | 成功，86 个归档条目 | 包含 prompts、product RPC 与三个 console entry points |
| wheel 新环境安装 | 成功 | Python 3.12 新 venv、正常解析依赖；未使用 editable 安装或项目 PYTHONPATH |
| 源码外 demo / Fake ask | 成功 | demo 基线按预期 ZeroDivisionError；Fake 工具链可读取，不证明模型修复质量 |
| VSIX build | 最终成功，8 个归档条目 | vsce 3.9.2、Node v24.20.0；显式文档基址；包含 LICENSE.txt，无 Python Runtime |
| 包内连接客户端 → wheel | explicit 与 PATH 两种模式通过 | VS Code API 被 mock；真实 spawn、initialize、session/create/run/get/list 与 Fake Runtime |
| 找不到 executable | ENOENT 拒绝 | headless 错误路径；真实 UI 另验 |
| 最终 VSIX 复检 | 通过 | 最终 JS 与已运行连接测试的 JS 相同，新增许可证存在 |
| 文档本地互链/版本/静态证据 | 通过 | 正式入口及扩展 README 的相对文件和锚点；没有开发者 home 路径、旧 TD 依赖；版本为 0.5.0 |
| selection 静态核对 | 通过 | 50 个唯一任务，19/26/5，10 个仓库；未重跑 gold preflight |
| `git diff --check` | exit 0 | 当前工作区空白检查 |

本轮 VSIX 最初的无 URL 构建失败，原因是 manifest 无 repository 字段导致相对 README 链接无法转换。
使用 [INSTALLATION](../INSTALLATION.md) 中的 baseContentUrl/baseImagesUrl 后构建成功；正式冻结时必须将 `main`
替换为包含公开文档的固定 commit/tag。沙盒内第二次文件枚举为空，沿用已授权宿主构建环境重跑成功。
这些结果没有证明 GitHub Release 下载、真实 VSIX 安装或 Extension Host GUI 操作已完成。

## 本轮产物身份

这些是本地审校产物，不是正式下载链接或冻结发布承诺。

| 文件 | SHA-256 |
| --- | --- |
| `codeagent_runtime-0.5.0-py3-none-any.whl` | `fff142c80082554e321c02f305c9a54a4229652359c7eae238304bba70bff979` |
| `codeagent-0.5.0.vsix` | `e171c297bff922b0137980c7d8de74aa154c2592953ffe20ed0bd6e88c6cda03` |

Runtime 依赖使用安装时解析版本，未锁定。干净安装仅验证本次依赖组合和离线路径；不外推未来依赖、平台或真实 provider
兼容性。完整 GUI 验收和可执行步骤见[发布清单](../RELEASE_VALIDATION.md)。
