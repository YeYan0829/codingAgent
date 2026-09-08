# 离线快速体验

本目录提供一个最小、确定、无需第三方依赖的 Git repository fixture，用于体验 CodeAgent 的隔离修改、验证、
Diff 和 Accept/Discard 流程。它只验证产品链路，不代表真实仓库理解能力或 SWE-bench 成绩。

## 准备仓库

从 CodeAgent repository 根目录运行：

```bash
examples/showcase/prepare.sh /tmp/codeagent-showcase/quick-start
```

脚本把 [`repos/offline-zero-count`](repos/offline-zero-count/) 复制到指定空目录，初始化干净 Git baseline，并拒绝
覆盖已有内容。

确认失败基线：

```bash
cd /tmp/codeagent-showcase/quick-start
python3 verify.py
```

当前实现应抛出 `ZeroDivisionError`。`verify.py` 不需要 pytest、网络或 CodeAgent Runtime 的 Python 环境。

## 使用 CodeAgent

通过 CLI 或 VS Code 打开 `/tmp/codeagent-showcase/quick-start`，配置 DeepSeek 或 GLM 后发送：

```text
修复零数量时的错误，运行仓库中的验证脚本并总结修改。
```

建议按以下顺序检查：

1. 首次受保护操作前审查创建 Session worktree 的 Approval；
2. 查看 search/read/edit/command 工具活动；
3. 确认 `python3 verify.py` 针对当前 Candidate 成功；
4. 打开 changed file 的原生 Diff；
5. 确认 source 此时仍保持失败 baseline；
6. 点击 Accept 后再次运行 `python3 verify.py`，或使用 Discard 确认 source 不变。

fixture 和测试命令离线；DeepSeek/GLM 模型请求仍通过 Runtime 访问 provider API，可能产生费用。Fake provider 只做
确定性只读探索，不能完成该修复。

更完整的真实仓库任务见 [`../eval-tasks/`](../eval-tasks/)，安装和环境边界见
[安装与运行](../../docs/INSTALLATION.md)。

需要为 GitHub README 截取产品画面时，使用同一 Quick Start 仓库并按
[GitHub 展示素材清单](SHOT_LIST.md)准备截图和短 GIF。
