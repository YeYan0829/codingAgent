# 安装与使用 v0.5.0

本版支持Linux／WSL2、Python 3.11+和VS Code 1.106+，需要Git、ripgrep与可用的Bubblewrap。Git需支持worktree和`merge-tree --write-tree`。Docker用于SWE-bench，日常本地任务无需Docker。

## 安装Runtime与插件

从[v0.5.0 Release](https://github.com/YeYan0829/codingAgent/releases/tag/v0.5.0)下载同版wheel、VSIX与校验和，在下载目录执行：

```bash
sha256sum --ignore-missing -c SHA256SUMS
python3 -m venv "$HOME/.local/share/codeagent/venv"
"$HOME/.local/share/codeagent/venv/bin/python" -m pip install ./codeagent_runtime-0.5.0-py3-none-any.whl
"$HOME/.local/share/codeagent/venv/bin/codeagent-rpc" --help
```

在VS Code执行 **Extensions: Install from VSIX...**，选择`codeagent-0.5.0.vsix`。将`codeagent.rpcCommand`设为已安装的`codeagent-rpc`可执行文件绝对路径。WSL用户在同一个WSL环境安装Runtime与扩展，并通过WSL窗口打开项目。

可用以下命令检查系统是否允许创建沙箱：

```bash
bwrap --ro-bind / / --unshare-user --unshare-pid --unshare-net \
  --new-session --die-with-parent --proc /proc --dev /dev -- /bin/true
```

检查失败时需要修复系统的namespace支持；Runtime拒绝执行项目命令。

## 完成一次任务

打开一个已有提交、工作目录干净的Git仓库。在CodeAgent侧栏配置Provider与API Key，发出任务并给出项目测试命令。密钥保存在VS Code SecretStorage；模型请求由Runtime发出，项目命令默认断网。

v0.5.0的VS Code模型设置提供GLM-5.2与DeepSeek V4。GLM-5.3用于CLI和正式测评，尚未进入本版扩展的模型白名单。第一次修改或执行命令前会请求创建隔离工作区；调查、修改和测试在候选目录中推进。审查原生Diff及验证结果后选择Accept应用到源项目，或Discard丢弃候选修改。

Runtime与项目可以使用不同依赖环境。项目命令进入独立沙箱，请给出项目自己的解释器和测试入口。

## 从源码安装

在本版源码目录执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/codeagent --help
```

无API Key的只读链路检查：

```bash
.venv/bin/codeagent ask . "读取README并概括项目用途"
```

内置fake模型用于确认安装和工具链。真实任务在CLI中可用`--provider glm --model glm-5.3 --reasoning --reasoning-effort high`选择本版正式测评所用模型参数。
