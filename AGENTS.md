# 给后续 Coding Agent 的协作说明

本项目是 CodeAgent Runtime，不是一次性 demo。请保持模块边界清晰，优先小步扩展，并同步补测试和文档。

协作规则：

- 不要随意重构目录结构，除非文档和测试同步更新。
- 不要删除 `.codeagent/sessions/` 下的 session 和日志。
- 不要绕过 safety、policy、approval 或 ToolRegistry。
- 新能力必须补充 pytest，并在 docs 中说明边界。
- 文档和代码注释优先使用中文。
- 代码标识符、模块名、类名、函数名保持英文。
- v0.1 是 readonly exploration，写文件、任意 shell、网络和危险操作默认不可用。
