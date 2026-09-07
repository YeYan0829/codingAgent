# 修复零数量行为

`calculator.divide(total, count)` 在 `count == 0` 时应返回 `0`，当前实现会抛出异常。请先确认失败行为，完成最小
修复，运行 `python3 verify.py`，并总结修改和验证结果。不要安装依赖或访问网络。
