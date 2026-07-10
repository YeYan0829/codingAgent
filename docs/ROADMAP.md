# Roadmap

## v0.1 readonly exploration

完成本地 CLI、FakeLLM、只读工具、session、policy、approval、安全边界和 pytest。

## v0.2 controlled shell / test runner

加入受控命令白名单，优先支持项目测试命令，不开放任意 shell。

## v0.3 patch draft

允许 agent 生成 patch 草案，但不直接应用。

## v0.4 approval-based apply_patch

在明确 approval 后应用 patch，并记录完整事件。

## v0.5 git worktree sandbox

为写操作提供隔离 worktree，降低污染主工作区的风险。

## v0.6 benchmark harness

加入可重复运行的 benchmark harness 和结果报告。

## v0.7 SWE-bench / Terminal-bench adapter

接入外部评测适配器，验证真实任务表现。

## v0.8 memory / context compression / project index

加入项目索引、上下文压缩和长期记忆能力。
