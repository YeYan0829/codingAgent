# click-short-help-sentences

## 给 Agent 的请求

Click 自动生成 command short help 时会在第一个以句点结尾的句子停止，但以 `!` 或 `?` 结尾的完整第一句不会得到相同行为，可能被继续拼接或截成省略号。请让 `_make_default_short_help` 把 `.`, `!`, `?` 都视为句末标点，同时保持段落、no-rewrap marker、嵌入单词或路径中的句点、`max_length` 和 `...` 长度行为不回归。

请补充最小回归测试，并运行：

```text
PYTHONPATH=src {{EVAL_PYTHON}} -m pytest -q -p no:cacheprovider tests/test_utils/test_make_default_short_help.py
```

不要安装依赖，不需要网络，不要对无关 help formatting 做重构。

## 人工观察

- 是否读取现有参数化测试后再修改；
- 是否以最小条件扩展句末判断；
- 是否继续保证返回值不超过 `max_length`；
- patch 是否只触及实现和对应测试。
