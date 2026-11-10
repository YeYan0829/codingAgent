# 安装与使用

CodeAgent v0.6.0 由 Python Runtime 与 VS Code 插件组成：Runtime 在本地执行任务，插件负责会话、批准与修改审查。安装完成后，你可以在侧栏描述目标，查看工具反馈，并直接检查当前项目中的变化。

## 1. 准备运行环境

| 组件 | 要求 | 用途 |
| --- | --- | --- |
| 系统 | Linux 或 WSL2 | Runtime 与命令沙箱 |
| Python | 3.11+ | 运行 Runtime |
| VS Code | 1.106+ | 插件界面 |
| ripgrep | `rg` 命令 | 代码检索 |
| Bubblewrap | 可用的 `bwrap` 与用户命名空间 | 本地命令隔离 |
| Git | 项目按需使用 | 查看项目版本与差异 |

Ubuntu／WSL2 安装基础依赖：

```bash
sudo apt-get update
sudo apt-get install -y bubblewrap ripgrep python3-venv git
```

确认当前系统允许创建沙箱：

```bash
bwrap --ro-bind / / --unshare-user --unshare-pid --unshare-net \
  --new-session --die-with-parent --proc /proc --dev /dev -- /bin/true
```

命令退出码应为 0。系统禁用所需 namespace 时，Runtime 会拒绝项目命令，并返回原因；文件读取与搜索仍可使用。

## 2. 安装配套 Runtime 与插件

本轮 **v0.6.0** 安装文件位于本地 `dist/`，正式上传后从 [GitHub Releases](https://github.com/YeYan0829/codingAgent/releases) 获取同名文件：

- `codeagent_runtime-0.6.0-py3-none-any.whl`
- `codeagent-0.6.0.vsix`
- `SHA256SUMS`

本地构建的同名文件位于仓库 `dist/`。先检查所下载文件的校验和，再安装 Runtime：

```bash
sha256sum --ignore-missing -c SHA256SUMS
python3 -m venv "$HOME/.local/share/codeagent/venv"
"$HOME/.local/share/codeagent/venv/bin/python" \
  -m pip install ./codeagent_runtime-0.6.0-py3-none-any.whl
"$HOME/.local/share/codeagent/venv/bin/codeagent-rpc" --help
```

在 VS Code 执行 **Extensions: Install from VSIX...**，选择 `codeagent-0.6.0.vsix`。打开设置，将 `codeagent.rpcCommand` 设为 Runtime 的可执行文件绝对路径，例如：

```text
/home/<用户名>/.local/share/codeagent/venv/bin/codeagent-rpc
```

设置值是一个可执行文件路径；请填写展开后的完整路径。WSL 用户将 Runtime 安装在 WSL 内，并通过 VS Code 的 WSL 窗口安装插件、打开项目。插件与 Runtime 使用同一版本。

## 3. 连接模型并完成第一次任务

1. 打开目标项目文件夹，在右侧打开 CodeAgent。多根工作区使用第一个文件夹。
2. 保存“仓库文件访问”范围。无需排除文件时可以留空保存；已禁止路径对读取、搜索、编辑和命令共同生效。
3. 从输入框命令菜单进入“模型连接与设置”，选择 GLM 或 DeepSeek、模型和 API Key。密钥保存在 VS Code SecretStorage，设置保存后用于新会话。
4. 新建会话，输入目标、期望行为及项目测试命令。正文依次显示调查、工具和执行反馈。
5. 展开修改文件，点击文件名打开原生 Diff；需要恢复时使用“撤销本轮”。运行中使用方形 Stop 按钮停止，等待回收结束后继续操作。

可以先在演示仓库中体验一次完整任务。按前文安装 wheel 后运行：

```bash
"$HOME/.local/share/codeagent/venv/bin/codeagent-product-demo" --root /tmp/codeagent-first-task
```

用 VS Code 打开生成目录，连接模型后发送：

```text
修复零数量时的错误，运行 python3 verify.py，并解释实现与验证结果。
```

演示脚本包含除零缺陷，验证命令无需第三方依赖。任务完成后可看到源码和验证反馈，再尝试 Diff、追问与撤销。源码开发者还可使用[免费界面样本](#7-开发测试与构建)检查各类交互。

## 4. 理解修改、批准与继续工作

**编辑与撤销。** Agent 直接修改当前项目，已有暂存、未暂存和未跟踪文件可以继续使用。受控文件编辑保存事务，默认撤销最近一轮；涉及文件被你或其他进程再次修改时，撤销会整组拒绝并说明冲突。命令可能产生构建文件或其他副作用，这些保留在项目中。

**运行环境。** Runtime 的 Python 环境用于运行 Agent；目标项目使用自己的解释器和依赖。每条项目命令启动新的非交互 shell；需要激活环境时，在同一条命令内完成激活与执行。本地 `/tmp` 每条命令重新创建，需要跨命令使用的调试文件应放在项目或持久 HOME 中。

**资源权限。** 项目内允许范围可以写入，Git／Runtime 元数据和仓库禁止路径受保护。网络和额外宿主写入需要批准。批准卡片给出命令与资源；拒绝后模型收到真实反馈，可以调整方案。模型服务请求由 Runtime 发出，与项目命令的网络权限分别管理。权限细节见[架构中的权限设计](ARCHITECTURE.md#从工具定义到真实资源权限)。

**会话与恢复。** 标题栏历史可以搜索、切换同项目会话，并保留各自草稿。重新打开时读取当前项目文件；此前的观察继续作为历史记录。连接中断后先查看状态和日志，再使用界面提供的继续或重连入口；结果未知的写操作保留现场供核对。

**可选委派。** 默认使用单 Agent。高级设置开启委派后，可以使用调查、命令或通用实现角色；主方等待子任务结束后继续，工具顺序执行。每个子角色拥有自己的历史，并使用父方允许的工具和资源。

## 5. 从源码运行与使用 CLI

```bash
git clone https://github.com/YeYan0829/codingAgent.git
cd codingAgent
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/codeagent ask . '读取 README 并概括项目' --provider fake
```

配置 GLM 凭据并启动交互任务：

```bash
read -rsp "GLM API Key: " GLM_API_KEY
printf '\n'
export GLM_API_KEY
.venv/bin/codeagent start . --provider glm --model glm-5.3 \
  --reasoning --reasoning-effort high
```

DeepSeek 使用 `DEEPSEEK_API_KEY`，例如 `--provider deepseek --model deepseek-v4-flash`。Runtime 从进程环境读取凭据；VS Code 使用插件中的 SecretStorage。GLM-5.3 使用 reasoning，支持 `low`、`high`、`max`，默认 `high`。需要自定义 endpoint 时在启动 Runtime 前设置 `GLM_BASE_URL` 或 `DEEPSEEK_BASE_URL`。

| 命令 | 用途 |
| --- | --- |
| `codeagent start <workspace>` | 开始交互任务 |
| `codeagent resume <session-id>` | 继续会话 |
| `codeagent show-changes <session-id>` | 查看变化 |
| `codeagent undo <session-id>` | 撤销最近一轮受控编辑 |
| `codeagent-rpc` | 启动插件使用的本地 RPC 服务 |

完整参数以各命令的 `--help` 为准。`ask` 用于非交互调用，`--delegation` 开启同步委派；需要人工批准的资源仍须经过正常批准路径。

## 6. 排查常见问题

| 现象 | 检查与处理 |
| --- | --- |
| Runtime 连接失败 | 打开 **View → Output → CodeAgent Runtime**，确认 `codeagent.rpcCommand` 可直接执行，随后重连 |
| 插件提示 Runtime 能力不匹配 | 安装配套版本的 wheel 与 VSIX，并检查当前实际启动路径 |
| 右侧找不到 CodeAgent | 执行 **View: Reset View Locations**，或手动调整侧栏位置 |
| 命令找不到项目依赖 | 使用项目自己的解释器／测试命令；在同一命令中激活环境 |
| `bwrap` 拒绝启动 | 执行第1节环境检查，核对内核／容器的 namespace 配置 |
| 撤销出现冲突 | 查看涉及路径的当前 Diff，保留外部改动后人工处理；重新点击不会覆盖冲突 |
| 找不到历史会话 | 检查 `codeagent.sessionRoot` 和 CLI 的 `--session-root` 是否相同 |

`rpcCommand=auto` 在源码开发布局中优先查找扩展父目录的 `.venv/bin/python`，独立安装时从 PATH 查找 `codeagent-rpc`。使用明确的绝对路径更便于核对版本。会话默认保存在 `~/.codeagent/sessions`，可由 `CODEAGENT_SESSION_ROOT` 或插件设置指定。

开发、构建和测试见下一节；两版评测附件及检阅方法见[测评报告](HAL50_EVALUATION.md#测评材料与版本检阅)。v0.5.0 的安装方式随其 [Release](https://github.com/YeYan0829/codingAgent/releases/tag/v0.5.0) 保留，该版本使用隔离候选与 Accept／Discard 工作流。


## 7. 开发测试与构建

在仓库根目录安装开发依赖，运行Python与插件回归：

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
cd vscode-extension
npm test
```

默认测试使用隔离临时仓库、确定性模型与SDK替身。完整回归先检查Bubblewrap、Docker服务及缓存镜像，再启用已有的免费集成测试：

```bash
CODEAGENT_TEST_BWRAP=/usr/bin/bwrap \
CODEAGENT_RUN_SWEBENCH_DOCKER=1 \
CODEAGENT_RUN_ENVIRONMENT_DOCKER=1 \
  .venv/bin/python -m pytest -q
```

Docker集成使用本地缓存的SymPy-20590和Pylint-4551镜像，不拉取镜像、不调用模型。固定题单兼容检查另需本地`reference/swe-bench-tasks`。各测试按实际依赖决定是否运行；源码包无需携带镜像和外部任务仓库。

可以用源码内置的免费交互样本检查任务、修改审查与恢复：

```bash
.venv/bin/python -m codeagent.product.ui_demo --root .codeagent/ui-demo
```

使用新的空目录生成样本，会得到`SAMPLES.md`、`samples.json`和测试工作区。模型与工具响应由免费夹具提供；实际命令仍通过Bubblewrap执行。

构建安装文件：

```bash
.venv/bin/python -m pip wheel . --no-deps -w dist
cd vscode-extension
npx --yes @vscode/vsce package --no-dependencies \
  --baseContentUrl https://github.com/YeYan0829/codingAgent/blob/v0.6.0/vscode-extension/ \
  --baseImagesUrl https://github.com/YeYan0829/codingAgent/raw/v0.6.0/vscode-extension/ \
  --out ../dist/codeagent-0.6.0.vsix
```

Python Runtime与VSIX分别打包。发布前在独立环境安装，检查CLI、插件连接、Diff、撤销、Stop与批准，最后生成校验和。测评的固定题单、两版配置和预检／评分工具见[HAL50测评工具](../benchmarks/README.md)。
