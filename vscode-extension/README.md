# CodeAgent VS Code Extension

本扩展启动本地 `codeagent-rpc`，提供 Session、Run/Continue、Stop/Approval、原生 Diff 与 Changes 处理界面。
扩展要求 VS Code 1.106 或更高版本，CodeAgent 默认显示在右侧 Secondary Side Bar，左侧 Primary Side Bar
继续显示 Explorer。每个 VS Code 窗口以当前打开的第一个 workspace folder 作为新 Session 的 source repository，
不需要在同一 workspace 内重复指定。

History 默认显示当前 workspace 的 Session；切换 **All workspaces** 可检索同一 Session Root 下的全局本地历史，
并按状态筛选。其他 repository 的条目只显示摘要，必须先在 VS Code 打开对应 workspace 才能进入或继续会话。

VS Code 会记住用户移动过的 View。若从旧版 CodeAgent 升级后仍显示在左侧，执行 **View: Reset View Locations**
恢复新的默认位置，或把 CodeAgent 容器拖到右侧；之后 VS Code 会继续保存用户选择。

## 开发运行

先在仓库根目录安装 Runtime：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

使用 VS Code 打开仓库根目录，按 F5 并选择 `Run CodeAgent Extension`。

## 打包安装

安装 `@vscode/vsce` 后，在本目录执行：

```bash
npx --yes @vscode/vsce package \
  --baseContentUrl https://github.com/YeYan0829/codingAgent/blob/main/vscode-extension/ \
  --baseImagesUrl https://github.com/YeYan0829/codingAgent/raw/main/vscode-extension/
```

然后在 VS Code 执行 `Extensions: Install from VSIX...` 并选择生成的 `.vsix`。目标机器还需安装
与扩展同版本的 `codeagent-runtime`（当前为 `0.5.0`），并确保 `codeagent-rpc` 在 PATH；VSIX 当前不捆绑
Python Runtime。开发仓库模式会
检查扩展目录父目录的 `.venv/bin/python` 并优先使用；否则从 Extension Host PATH 查找。
也可将 `codeagent.rpcCommand` 设为 `codeagent-rpc` 的绝对路径（不能附带参数）。安装后可以打开任意受支持的 Git workspace 使用，无需为每个 workspace 重装扩展。

API Key 通过扩展内配置页写入 VS Code SecretStorage，不要写入 VSIX、仓库或 settings.json。
完整安装与排错见[安装与运行](../docs/INSTALLATION.md)，产品行为见[产品使用流程](../docs/PRODUCT.md)，维护协议见
[Product RPC](../docs/RPC.md)，人工验收状态见[发布验收](../docs/RELEASE_VALIDATION.md)。
