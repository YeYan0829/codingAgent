# itsdangerous-strict-base64

## 给 Agent 的请求

ItsDangerous 的 URL-safe Base64 解码当前会悄悄忽略部分不属于 Base64 alphabet 的字符，这会让损坏的 token 看起来仍可解码。请修改 `base64_decode`：对非 ASCII 输入或 alphabet 之外的字符稳定抛出项目现有的 `BadData("Invalid base64-encoded data")`，同时继续支持合法的 URL-safe、无 padding 输入。

请先阅读现有实现和测试，补充针对性回归测试，运行：

```text
PYTHONPATH=src {{EVAL_PYTHON}} -m pytest -q -p no:cacheprovider tests/test_itsdangerous/test_encoding.py
```

不要安装或升级依赖，不需要网络，不要改变公开异常类型或合法 token 格式。

## 人工观察

- 是否先定位 `encoding.py` 和现有 encoding tests；
- 是否避免宽泛捕获掩盖无关错误；
- 是否保留 URL-safe `-`、`_` 和自动 padding；
- 是否运行了指定验证并明确报告结果。
