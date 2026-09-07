# v0.5.0 Release Validation Phase 1

本页记录 `v0.5.0-rc.1` 发布前的安装与核心链路验收。它不表示 tag 或 Release 已创建。

状态只使用：`PASS`、`FAIL`、`MANUAL PENDING`、`BLOCKED`、`NOT RUN`。

## 当前发布基线

| 项目 | 当前值 |
| --- | --- |
| Branch | `feature/v0.4-candidate-loop` |
| HEAD | `7faf6ff55eea99d1cfeb90ced02c67d13b9b880c` |
| Working tree | 63 条修改或未跟踪记录 |
| Python package | `codeagent-runtime 0.5.0` |
| Runtime | `codeagent.__version__ == 0.5.0` |
| VS Code Extension | `0.5.0` |
| README 声明 | `0.5.0` 发布候选开发阶段 |
| 本页目标 | `v0.5.0-rc.1`，尚未创建 |

本轮结果对应上面的 HEAD 加未提交工作区。HEAD 本身不包含当前全部实现和文档，不能作为冻结发布版本。

## 自动验收结果

完整命令、产物哈希和输出摘要见
[Phase 1 证据](evidence/release-validation-phase1.md)。

| 验证目标 | 状态 | 方法 / command | 当前结果 | 对应 commit | Evidence / notes |
| --- | --- | --- | --- | --- | --- |
| 源码 clean install | BLOCKED | 从冻结 commit clone；新建 venv；执行 README 的 editable install 和 Fake smoke | 当前 dirty 状态没有可 clone 的等价 commit | HEAD + dirty | 已准备 [clean-install-smoke.sh](evidence/clean-install-smoke.sh)；冻结后执行 |
| wheel 构建与内容 | PASS | `pip wheel --no-cache-dir --no-deps .`；读取 zip、metadata 和 entry points | wheel 构建成功；package、prompts、License 和 3 个入口齐全 | HEAD + dirty | SHA-256 与文件表见 Phase 1 证据 |
| wheel 独立安装 | PASS | 全新 venv 使用 `pip install --no-cache-dir <wheel>` | import、三个 CLI、Fake smoke 和 stdio RPC 通过 | HEAD + dirty | 导入来自新 venv；没有开发仓库路径依赖 |
| VSIX 构建与内容 | PASS | `node --check`；`npm test`；`vsce package`；读取 VSIX zip 和 manifest | 8 个预期文件；版本、engine、activation、commands、views 和 settings 正常 | HEAD + dirty | VSCE 的 missing repository warning 为非阻断元数据问题 |
| wheel ↔ VSIX 分装连接 | PASS | 解包 VSIX；用真实 `RuntimeConnection` spawn wheel venv 中的 RPC | explicit path 和 PATH 均完成 initialize/create/run/get/list；缺失 executable 被拒绝 | HEAD + dirty | [runtime-connection-smoke.js](evidence/runtime-connection-smoke.js)；VS Code API 为 mock |
| Python 默认测试 | PASS | `.venv/bin/python -m pytest -q` | 303 passed，5 skipped | HEAD + dirty | opt-in bwrap/Docker 已另行开启 |
| 产品、交付与 RPC 定向测试 | PASS | 运行 product changes/execution/read model/RPC/demo/manifest 六组测试 | 33 passed | HEAD + dirty | 使用临时真实 Git 仓库；模型和部分边界使用测试替身 |
| Extension 单测 | PASS | `cd vscode-extension && npm test` | 1 passed | HEAD + dirty | 验证渲染逻辑；不等同 GUI 安装 |
| 真实 Bubblewrap | PASS | 设置 `CODEAGENT_TEST_BWRAP=/usr/bin/bwrap` 后运行集成测试 | 5 passed | HEAD + dirty | 覆盖可写区、只读区、网络、超时清理和 fail closed |
| 真实 Docker projection | PASS | 设置 `CODEAGENT_RUN_SWEBENCH_DOCKER=1` 后运行单个集成用例 | 1 passed | HEAD + dirty | 不是正式 SWE-bench 50 题 |
| 远端 CI | NOT RUN | push 冻结 commit 后检查 workflow run | 当前没有对应远端 run | 待冻结 | workflow 文件存在不能代替执行结果 |
| 真实 VS Code 安装与操作 | MANUAL PENDING | 安装 wheel 和 VSIX，在图形 VS Code 完成人工清单 | 尚未由用户操作 | 待冻结 | headless 连接不能代替真实 Extension Host |

## Runtime 与 VSIX 如何连接

VSIX 不包含 Python Runtime。用户需要先安装同版本 wheel，再安装 VSIX。

扩展默认使用 `auto`。在普通安装环境中，它从 Extension Host 的 PATH 查找 `codeagent-rpc`。
用户也可以把 `codeagent.rpcCommand` 设置为该可执行文件的绝对路径。

源码开发模式有一个额外分支。扩展目录父目录存在 `.venv/bin/python` 时，`auto` 会用它运行
`python -m codeagent.product.rpc`。打包后的 VSIX 不依赖该目录；本轮已用不含开发 venv 的解包路径验证 PATH fallback。

扩展通过 Node `spawn` 启动 Runtime。双方使用 stdin/stdout 传输 JSON-RPC，首个请求是协议版本 `1.0` 的
`initialize`。Runtime 返回自身版本、协议版本和能力列表。

当前客户端会等待 initialize 成功，但不会主动比较返回的版本或能力。因此用户必须安装配套的 `0.5.0` 产物。
这属于当前兼容性边界，不影响本轮同版本握手。

Runtime 找不到或异常退出时，界面显示 `Runtime unavailable`。用户可以选择 `Retry Runtime` 或
`Open Output`。当前界面没有常驻的 `connected` 文本；没有错误且任务入口可用，表示连接已经建立。

## 真实 Bubblewrap 覆盖范围

真实 bwrap 用例通过环境变量显式开启。它们实际创建 namespace，没有使用测试替身。

| 行为 | 状态 | 实际检查 |
| --- | --- | --- |
| bwrap 发现与 sandbox 创建 | PASS | 检查 executable、required flags 和 namespace smoke |
| Agent worktree 写入 | PASS | sandbox 内创建文件并在 worktree 中读回 |
| source workspace 只读 | PASS | 写入被拒绝，原文件字节不变 |
| host root 只读 | PASS | 修改 `/var/tmp` sentinel 被拒绝，宿主文件不变 |
| 默认网络关闭 | PASS | 默认 namespace 无法访问 host localhost；一次批准后可访问 |
| timeout 与子进程清理 | PASS | 超时返回，后台子进程未延迟写入 |
| 运行期目录清理 | PASS | command runtime HOME 在完成后清理 |
| sandbox 不可用时关闭命令 | PASS | 不存在的 bwrap 路径返回 `sandbox_unavailable`，payload 未运行 |

`tests/test_sandbox_executor.py` 使用 `HostFixtureBackend`，不会创建 namespace。
`tests/test_command_sandbox_service.py` 使用 fake executor。它们验证执行器和策略逻辑，不计作真实 bwrap 证据。

## 人工产品验收清单

以下各项状态均为 `MANUAL PENDING`。执行对象必须是冻结 commit 生成的 wheel 和 VSIX。
证据记录当前使用的 commit、产物 SHA-256、VS Code 版本和实际结果，不要求本轮截图或视频。

| 步骤 | 验证目标 | 操作与通过条件 | Evidence |
| --- | --- | --- | --- |
| A | Runtime ↔ Extension | 在全新 venv 安装 wheel，再安装 VSIX。先设置 `codeagent.rpcCommand` 为绝对路径，再用 PATH 启动。打开目标 Git 仓库后，任务入口可用且 Output 无握手错误。随后配置不存在的路径，确认出现 Runtime unavailable、Retry 和 Open Output | 待填写 |
| B | 基本任务 | 用 `codeagent-product-demo` 创建离线失败示例。新建 Session，发送 bugfix 任务。确认时间线持续更新，模型消息和工具调用可见 | 待填写 |
| C | Approval | 触发 worktree 或命令审批。确认界面显示原因和权限范围。先 Reject，确认任务不会越权继续；重新触发后 Allow。一次许可不得扩大到无关后续权限 | 待填写 |
| D | Worktree / source protection | 在任务执行期间检查源仓库。Accept 前，Agent 修改只出现在隔离 worktree，source 文件内容保持不变 | 待填写 |
| E | Validation | 先运行失败验证，再修复并运行成功验证。成功后继续修改代码，确认旧结果显示过期。重新验证后恢复为当前结果 | 待填写 |
| F | Diff | 从 Changes 打开原生 Diff。确认文件列表和内容对应当前尚未交付的修改 | 待填写 |
| G | Accept | Accept 后确认修改进入 source，且没有自动创建额外 commit。分别制造非冲突 source 变化和真实冲突，确认合并或拒绝行为符合界面提示 | 待填写 |
| H | Discard | 新建任务并产生修改，然后 Discard。确认 source 不变，待交付修改被清空，Session 返回可理解状态 | 待填写 |
| I | Stop / Budget | 在模型、命令或审批阶段请求 Stop，确认它在当前操作结束后生效。用小预算触发上限，确认提示可理解；显式增加预算后继续同一任务，且权限没有扩大 | 待填写 |
| J | History | 完成后重载窗口并切换仓库。确认全局 History 能找到 Session；在错误仓库只能看摘要，打开对应仓库后才能恢复 | 待填写 |

## Release blockers

当前只有一个已确认的发布阻塞条件：工作区尚未冻结。用户无法从 HEAD clone 出本轮验证的版本，
所以源码 clean install 还不能给出 PASS。

处理方式是先审查并提交当前工作区，再对完整 commit 运行：

```bash
bash docs/evidence/clean-install-smoke.sh \
  https://github.com/YeYan0829/codingAgent.git \
  <完整冻结 commit> \
  /tmp/codeagent-clean-install-<commit>
```

本轮没有发现 wheel 独立安装、VSIX 构建、Runtime discovery、RPC handshake 或 Bubblewrap 边界方面的实现 blocker。
真实 GUI 核心流程仍需人工验收；这是尚未完成的发布门禁，不是已经复现的产品错误。

截图、视频和正式 SWE-bench 50 题没有执行，也不列为本阶段 blocker。

## Phase 1 建议

**需要重新冻结 commit 后复验。**

clean clone 通过后，再用同一 commit 的 wheel 和 VSIX 完成人工清单。此阶段不创建 tag、Pre-release，
也不运行正式 SWE-bench 50 题。
