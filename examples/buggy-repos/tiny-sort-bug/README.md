# tiny-sort-bug

这是一个极小的只读分析样例仓库，用于验证真实 LLM 能通过 CodeAgent Runtime 的只读工具分析 bug。

已知问题：`sort_utils.py` 中的 `sort_numbers` 使用 `nums.reverse()` 并直接返回结果。Python 的 `list.reverse()` 会原地修改列表并返回 `None`，所以测试期望排序列表时会失败。

请不要让 agent 修改这个仓库。它只用于人工功能测试。
