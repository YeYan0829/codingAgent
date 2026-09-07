# 安装与运行

CodeAgent 由两个独立部分组成。Python Runtime 负责执行任务，VS Code Extension 提供界面。
安装 VSIX 不会自动安装 Runtime。

当前版本为 `0.5.0`，仍处于发布候选开发阶段。本文不声明已经存在可下载的正式 Release 产物。
当前产物验证状态见[发布验收](RELEASE_VALIDATION.md)。

## 系统要求

| 组件 | 要求 | 用途 |
| --- | --- | --- |
| 操作系统 | Linux 或 WSL2 | 当前支持的平台 |
| Python | 3.11 或更高版本 | 运行 Runtime 和 CLI |
| Git | 支持 `worktree` 和 `merge-tree --write-tree` | 隔离和合并修改 |
| ripgrep | 提供 `rg` 命令 | 搜索代码 |
| Bubblewrap | 提供可用的 `bwrap` 命令 | 隔离本地命令 |
| VS Code | 1.106 或更高版本 | 使用 Extension 界面 |
| Node.js | 建议 22 | 开发和打包 Extension |
| Docker | 可选 | 只用于 SWE-bench |

Ubuntu 或 WSL2 可以安装基础依赖：

```bash
sudo apt-get update
sudo apt-get install -y bubblewrap ripgrep python3-venv git
```

安装 `bwrap` 不代表当前系统允许创建 sandbox。可以运行下面的命令检查：

```bash
bwrap --ro-bind / / --unshare-user --unshare-pid --unshare-net \
  --new-session --die-with-parent --proc /proc --dev /dev -- /bin/true
```

检查失败时，CodeAgent 会拒绝运行项目命令。搜索和读取代码仍然可用。

## Runtime 环境与项目环境

Runtime 安装在自己的 Python 环境中。这个环境只负责运行 CodeAgent。

目标项目可能有另一套 Python、Node 或其他依赖。CodeAgent 不会自动安装或激活项目环境。

每条 Agent 命令又会进入一个新的 Bubblewrap sandbox。这个 shell 使用独立 HOME 和 `/tmp`，默认不能联网。

因此，在 Runtime 环境中安装 pytest，不代表目标项目会自动使用这个 pytest。
请在任务中给出项目自己的测试命令，或让 Agent 先读取项目文档。

## 开发者：从源码安装

```bash
git clone https://github.com/YeYan0829/codingAgent.git
cd codingAgent
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/codeagent --help
```

不需要运行测试时，可以把 `.[dev]` 改为 `.`。开发依赖目前只额外安装 pytest。

安装后提供三个命令：

| 命令 | 用途 |
| --- | --- |
| `codeagent` | 运行 CLI 和 SWE-bench 命令 |
| `codeagent-rpc` | 为 VS Code Extension 提供本地 JSON-RPC 服务 |
| `codeagent-product-demo` | 创建离线演示仓库 |

下面的安装检查不需要 API Key：

```bash
.venv/bin/codeagent ask . "只读取 README，并用三句话概括项目"
```

它使用内置 Fake provider，只检查只读工具链。它不能修改代码，也不代表真实模型质量。

## 配置模型

CLI 当前支持 DeepSeek 和 GLM。Runtime 不会读取目标仓库中的 `.env`。

DeepSeek：

```bash
read -rsp "DeepSeek API Key: " DEEPSEEK_API_KEY
echo
export DEEPSEEK_API_KEY
.venv/bin/codeagent start . --provider deepseek --model deepseek-v4-flash
```

GLM：

```bash
read -rsp "GLM API Key: " GLM_API_KEY
echo
export GLM_API_KEY
.venv/bin/codeagent start . --provider glm --model glm-5.2
```

使用 GLM Coding Plan 专属 endpoint 时，先设置：

```bash
export GLM_BASE_URL=https://open.bigmodel.cn/api/coding/paas/v4
```

模型请求由 Runtime 进程发出。项目命令默认断网，不代表模型请求也断网。
真实模型调用可能产生费用。

## 运行离线演示

在 CodeAgent 源码目录中执行：

```bash
.venv/bin/codeagent-product-demo --root /tmp/codeagent-product-demo
cd /tmp/codeagent-product-demo
python3 verify.py
cd -
```

`verify.py` 应以 `ZeroDivisionError` 失败。演示仓库不依赖第三方包或网络。

要用 CLI 让真实模型处理这个仓库：

```bash
.venv/bin/codeagent start /tmp/codeagent-product-demo \
  --provider glm --model glm-5.2
```

发送：

```text
修复零数量时的错误，运行仓库中的验证脚本并总结修改。
```

第一次修改前，CodeAgent 会请求创建 Git worktree。完成后可以查看修改：

```bash
.venv/bin/codeagent show-changes <session-id>
```

确认后再应用到源仓库：

```bash
.venv/bin/codeagent accept-changes <session-id>
```

目标目录必须是 Git 仓库根目录，并且至少有一个 commit。工作目录不能有 tracked 或 untracked 修改。
Git ignored 文件不会自动复制到新的 worktree。

## 开发运行 VS Code Extension

1. 用 VS Code 打开 CodeAgent 源码仓库。
2. 按 `F5`，选择 **Run CodeAgent Extension**。
3. 在新的 Extension Development Host 中打开目标 Git 仓库。
4. 在右侧 CodeAgent 中配置模型和 API Key。
5. 点击 **Start a new task**。

在这个目录布局中，`rpcCommand=auto` 会检查仓库根目录。
它会找到其中的 `.venv/bin/python`。
Extension 会用它运行 `codeagent.product.rpc`。

如果 CodeAgent 没有出现在右侧，执行 **View: Reset View Locations**。
VS Code 可能保留了旧版本的 View 位置。

## 普通用户：安装 wheel 与 VSIX

拿到可信发布产物后，先把 Runtime 安装到独立虚拟环境：

```bash
python3 -m venv "$HOME/.local/share/codeagent/venv"
"$HOME/.local/share/codeagent/venv/bin/python" \
  -m pip install ./codeagent_runtime-0.5.0-py3-none-any.whl
"$HOME/.local/share/codeagent/venv/bin/codeagent-rpc" --help
```

然后在 VS Code 中执行 **Extensions: Install from VSIX...**。
选择 `codeagent-0.5.0.vsix`。

打开 Settings，找到 `codeagent.rpcCommand`。把它设为 `codeagent-rpc` 的完整绝对路径。
这个设置只接受可执行文件路径，不能填写 `python -m ...`，也不会展开 `~` 或 `$HOME`。

在 WSL 中，Runtime 和 Extension 必须位于同一个 WSL 环境。
Windows PATH 中的程序不会自动出现在 WSL Extension Host 中。

这种安装方式不依赖 CodeAgent 源码仓库，也不需要按 `F5`。

## Extension 如何找到 Runtime

| 设置 | 实际行为 |
| --- | --- |
| `rpcCommand=auto`，扩展父目录有 `.venv/bin/python` | 使用该 Python 启动 Runtime 模块 |
| `rpcCommand=auto`，没有上述文件 | 从 Extension Host 的 PATH 查找 `codeagent-rpc` |
| `rpcCommand` 是自定义路径 | 直接启动该可执行文件 |

`auto` 只检查文件是否存在，不会确认该 Python 已安装 CodeAgent。
独立安装时建议填写明确的 `codeagent-rpc` 绝对路径。

如果修改了 PATH，需要重启 VS Code 或对应的 Extension Host。

`codeagent.sessionRoot` 可以指定会话记录目录。留空时，Runtime 先读取 `CODEAGENT_SESSION_ROOT`，
再回退到 `~/.codeagent/sessions`。

启动后，Extension 会发送协议版本 `1.0` 的初始化请求。Runtime 会返回自身版本和支持的功能。

当前 Extension 不检查返回的 Runtime 版本，也不根据功能列表调整界面。请安装配套版本的 wheel 和 VSIX。

当前 RPC 请求也没有超时。如果进程存在但不响应，界面可能一直等待。

找不到 Runtime 或进程退出时，界面会显示 **Runtime unavailable**。
可以打开 Output 查看错误，再点击 **Retry Runtime**。

## 开发者：构建 wheel 和 VSIX

从仓库根目录执行：

```bash
.venv/bin/python -m pip wheel . --no-deps -w dist
cd vscode-extension
npx --yes @vscode/vsce package \
  --baseContentUrl https://github.com/YeYan0829/codingAgent/blob/main/vscode-extension/ \
  --baseImagesUrl https://github.com/YeYan0829/codingAgent/raw/main/vscode-extension/ \
  --out ../dist/codeagent-0.5.0.vsix
cd ..
sha256sum dist/*.whl dist/*.vsix
```

Extension 的 `package.json` 当前没有 `repository` 字段。
因此，省略两个 URL 参数时，vsce 无法转换 README 中的相对链接。

正式发布时，应把 URL 中的 `main` 替换为发布 tag 或固定 commit。

`--no-deps` 表示只构建 CodeAgent wheel。用户安装时仍需下载 Runtime 依赖。

当前依赖版本没有完全锁定，因此本页不承诺产物可以逐字节复现。

## Docker 与 SWE-bench

普通本地使用不需要 Docker。SWE-bench 需要官方镜像、固定任务数据和 official grader。

这些准备不属于快速安装流程。详细步骤见[评测方法](BENCHMARKING.md)。

## 常见问题

### `bwrap` 已安装，但命令仍被拒绝

先运行本文开头的 Bubblewrap 检查。某些容器、WSL 配置或企业 kernel 禁止所需 namespace。

检查失败时不要关闭 sandbox。搜索和读取功能仍然可以使用。

### 项目命令找不到程序或 Python 模块

每条命令都使用新的非交互 shell。Runtime 虚拟环境不会自动成为项目环境。

请使用项目文档给出的解释器路径。需要激活环境时，在同一条命令中完成激活和测试。

### Extension 显示 Runtime unavailable

打开 **View → Output → CodeAgent Runtime**。检查 `codeagent.rpcCommand` 指向一个可执行的 `codeagent-rpc`。

不要把它指向 Python 本体或包含参数的字符串。安装模式下建议使用绝对路径。

### 找不到之前的会话

不同 Session Root 保存不同的历史记录。CLI 后续命令需要继续使用相同的 `--session-root`。

Extension 用户应检查 `codeagent.sessionRoot`。**All workspaces** 只搜索当前 Session Root。

### 测试通过后，Accept 仍不可用

如果测试后代码又发生变化，界面会把旧测试结果标记为过期。请重新测试当前代码。

Accept 还会检查实际工作区状态，因此按钮可用也不保证最终合并一定成功。
详细边界见[安全模型](SECURITY.md)。
