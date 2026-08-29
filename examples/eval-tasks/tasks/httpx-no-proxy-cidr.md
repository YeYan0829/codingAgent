# httpx-no-proxy-cidr

## 给 Agent 的请求

HTTPX 会把 `NO_PROXY` 中的 IPv4/IPv6 CIDR 转成 mount pattern，但 `URLPattern` 当前没有真正按网段判断目标 IP，IPv6 CIDR 的 pattern 形式也不稳定。请让 CIDR 条目只匹配网段内的 IP，并保持普通 hostname、通配域名、scheme 和 port pattern 行为不变。

至少覆盖 IPv4 `192.168.0.0/16` 的范围内/外地址，以及 IPv6 `2001:db8::/32` 的范围内/外地址。实现应使用标准库地址语义，不依赖 DNS 或真实网络。

请补充回归测试，并运行：

```text
PYTHONPATH=. ~/.cache/codeagent-evals/httpx-no-proxy-cidr/bin/python -m pytest -q -p no:cacheprovider -k 'environment_proxies or url_matches or pattern_priority or cidr' tests/test_utils.py
```

不要安装依赖、不要访问网络、不要重写整个 proxy subsystem。

## 人工观察

- 是否追踪 `get_environment_proxies → URLPattern → Client mounts`；
- 是否同时处理 IPv4/IPv6 和非法/非 CIDR hostname；
- 是否避免把 CIDR 当成域名 suffix；
- 是否运行 targeted `test_utils.py`，而不是联网或完整 client suite。
