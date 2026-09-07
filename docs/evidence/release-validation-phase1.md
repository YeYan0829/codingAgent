# Release Validation Phase 1 证据

执行日期：2026-09-07。

本记录对应 Git HEAD `7faf6ff55eea99d1cfeb90ced02c67d13b9b880c` 和当时的未提交工作区。
工作区有 63 条 `git status --porcelain=v1` 记录。因此，以下结果不能由该 HEAD 单独复现。

## 版本与产物

Python 包、`codeagent.__version__` 和 VS Code Extension 的版本均为 `0.5.0`。
README 将其声明为发布候选开发阶段。`docs/RELEASE_VALIDATION.md` 的目标是 `v0.5.0-rc.1`，尚未创建 tag。

本轮使用独立目录 `/tmp/codeagent-phase1-20260907-pAhNtr`。该目录不属于项目源码。

| 产物 | 结果 | SHA-256 |
| --- | --- | --- |
| `codeagent_runtime-0.5.0-py3-none-any.whl` | 构建成功，160921 bytes | `4088fc4c0f24f41724238f02be3a853d0e040aa2f488a379d000f7d49c3500bf` |
| `codeagent-0.5.0.vsix` | 构建成功，21916 bytes，8 个文件 | `d4cb14cad10689ec7fed47f0714c300d24e749f82b234966d8aa4ce01e24b8d3` |

VSCE 报告 `package.json` 没有 `repository` 字段。它没有影响构建、安装内容或 Runtime 连接，因此记录为非阻断发布元数据问题。

## Wheel 构建与独立安装

构建命令：

```bash
.venv/bin/python -m pip wheel --no-cache-dir --no-deps \
  --wheel-dir /tmp/codeagent-phase1-20260907-pAhNtr/wheelhouse .
```

wheel 包含全部 `codeagent` Python package、两个 Markdown prompt、MIT License 和 dist-info。
它没有包含 `tests/`、`docs/`、`.github/` 或 `vscode-extension/`。

三个 console entry point 为：

```text
codeagent = codeagent.cli:app
codeagent-product-demo = codeagent.product.demo:main
codeagent-rpc = codeagent.product.rpc:main
```

使用 `--no-cache-dir` 将 wheel 安装到全新 venv 后，以下检查通过：

- `import codeagent`、`import codeagent.product.rpc`；
- `codeagent --help`；
- `codeagent-rpc --help`；
- `codeagent-product-demo --help`；
- 在源码目录外对临时 Git clone 运行 Fake provider 只读任务；
- 通过 stdio 发送 `initialize` 和 `session/list`。

导入位置是新 venv 的 `site-packages`。wheel 没有 `/home/a1872/` 等开发机路径。
`codeagent-product-demo` 使用通用的 `/tmp` 默认目录，这不是对开发仓库的依赖。

## VSIX 内容与 Runtime 连接

打包前执行了 `node --check extension.js` 和 `npm test`。打包命令为：

```bash
npx --yes @vscode/vsce package \
  --out /tmp/codeagent-phase1-20260907-pAhNtr/release/codeagent-0.5.0.vsix \
  --baseContentUrl https://github.com/YeYan0829/codingAgent/blob/main/vscode-extension/ \
  --baseImagesUrl https://github.com/YeYan0829/codingAgent/raw/main/vscode-extension/
```

VSIX 只包含 manifest、`extension.js`、`rpcClient.js`、README、License 和图标。
版本为 `0.5.0`，VS Code engine 为 `^1.106.0`。包内没有 test、`.vscode`、`node_modules`、嵌套 VSIX 或开发机路径。
包内 README 链接使用项目的真实 GitHub 地址，没有保留打包占位地址。

manifest 声明了 `codeagent.newTask`、`codeagent.refreshSessions`、右侧栏 View，
以及 `codeagent.rpcCommand` 和 `codeagent.sessionRoot` 两个设置。

使用解包后的真实 `RuntimeConnection` 和 wheel venv 中的真实 `codeagent-rpc` 运行
[`runtime-connection-smoke.js`](runtime-connection-smoke.js)，结果如下：

```text
explicit: VSIX RuntimeConnection -> wheel RPC PASS
PATH: VSIX RuntimeConnection -> wheel RPC PASS
missing executable: rejected PASS
```

该检查实际执行了 spawn、stdio JSON-RPC、initialize、session create/run/get/list 和 Fake provider。
它 mock 了 VS Code API，因此不能代替真实 VS Code 安装和点击验收。

## Runtime discovery 的实际行为

`codeagent.rpcCommand` 的默认值是 `auto`。安装后的扩展先检查扩展目录父目录下是否存在
`.venv/bin/python`。这个分支用于源码仓库中的 Extension Development Host。

如果没有开发 venv，扩展执行 PATH 中的 `codeagent-rpc`。用户也可以把
`codeagent.rpcCommand` 设置为可执行文件的绝对路径。该设置不解析参数，也不通过 shell 执行。

扩展用 Node `spawn` 启动 Runtime，并用管道传输 JSON-RPC。它首先发送协议版本 `1.0` 的
`initialize` 请求。Runtime 返回协议版本、Runtime 版本和 capabilities。

当前客户端等待 initialize 成功，但不会比较返回的版本和 capabilities，也没有单个请求的超时。
因此，配套安装 `0.5.0` 是当前发布约束，自动版本兼容检查仍是已知限制。

找不到可执行文件或 Runtime 退出时，未完成请求会失败。界面显示 `Runtime unavailable`，
并提供 `Retry Runtime` 和 `Open Output`。当前没有常驻的 `connected` 文字；无错误且任务按钮可用是连接成功的界面信号。

## Bubblewrap 与 Docker

原有 [`test_bubblewrap_integration.py`](../../tests/test_bubblewrap_integration.py) 中只有两个用例真实调用 bwrap，
并通过 `CODEAGENT_TEST_BWRAP` 显式开启。本轮补充了宿主根目录只读、超时清理和缺失后端关闭测试。

```bash
CODEAGENT_TEST_BWRAP=/usr/bin/bwrap \
  .venv/bin/python -m pytest -q tests/test_bubblewrap_integration.py
```

结果为 `5 passed in 6.13s`。真实检查覆盖：

- bwrap 发现、feature probe 和 namespace smoke；
- Candidate/worktree 写入；
- source 和 Git metadata 只读；
- `/var/tmp` 下的宿主 sentinel 只读；
- 默认断网，以及一次批准后的 host network；
- 超时后停止子进程，且子进程不会延迟写入；
- 运行期 HOME 清理；
- bwrap 路径不存在时拒绝 payload。

[`test_sandbox_executor.py`](../../tests/test_sandbox_executor.py) 的 `HostFixtureBackend` 不创建 namespace。
它主要覆盖 shell 语法、输出上限、环境过滤、fresh shell 和进程组终止逻辑。
[`test_command_sandbox_service.py`](../../tests/test_command_sandbox_service.py) 使用 fake executor，
主要覆盖审批、变更审计和恢复状态。两组测试不作为真实 bwrap 证据。

单个真实 Docker projection 用例也已运行：

```bash
CODEAGENT_RUN_SWEBENCH_DOCKER=1 \
  .venv/bin/python -m pytest -q tests/test_swebench_docker_integration.py
```

结果为 `1 passed in 18.08s`。它验证 `/testbed` 映射、容器内写入回传和 source 不变。
本轮没有运行正式 SWE-bench 50 题。

## 自动测试

| Command | Result |
| --- | --- |
| `.venv/bin/python -m pytest -q` | `303 passed, 5 skipped in 24.02s` |
| 产品与 RPC 定向测试 | `33 passed in 3.48s` |
| `cd vscode-extension && npm test` | `1 passed` |
| `node --check vscode-extension/extension.js` | exit 0 |
| `git diff --check` | exit 0 |

默认跳过项包含四个 opt-in bwrap 用例和一个 opt-in Docker 用例。这些用例已在上面单独开启并通过。

## 尚未完成

当前未提交工作区无法由 HEAD clean clone 重建。因此源码 clean install 的最终结论是 `BLOCKED`，
不能用旧 HEAD 的 clone 结果代替。

冻结 commit 后运行：

```bash
bash docs/evidence/clean-install-smoke.sh \
  https://github.com/YeYan0829/codingAgent.git \
  <完整冻结 commit> \
  /tmp/codeagent-clean-install-<commit>
```

真实 VS Code 中的 VSIX 安装、界面连接、Approval、Diff、Accept、Discard、Stop、Budget 和 History
仍为 `MANUAL PENDING`。
