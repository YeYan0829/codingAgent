# 上游实现参考

本目录用于本地检出 Coding Agent 上游源码，不将第三方仓库内容提交到本项目。

当前调研基线：

| 本地目录 | 上游仓库 | Commit |
| --- | --- | --- |
| `openhands-software-agent-sdk/` | `https://github.com/OpenHands/software-agent-sdk.git` | `704cbe6015e3d59cabe04632175d99df2d448999` |
| `swe-agent/` | `https://github.com/SWE-agent/SWE-agent.git` | `3ea751c087f32b16e039a2233dd6eefecef325d5` |
| `aider/` | `https://github.com/Aider-AI/aider.git` | `5dc9490bb35f9729ef2c95d00a19ccd30c26339c` |

对应的源码级 Context 调研见
[`CONTEXT_IMPLEMENTATION_REFERENCE_STUDY_2026-08-31.md`](../docs/archive/research/CONTEXT_IMPLEMENTATION_REFERENCE_STUDY_2026-08-31.md)。

重新获取时使用：

```bash
git clone --depth 1 https://github.com/OpenHands/software-agent-sdk.git reference/openhands-software-agent-sdk
git clone --depth 1 https://github.com/SWE-agent/SWE-agent.git reference/swe-agent
git clone --depth 1 https://github.com/Aider-AI/aider.git reference/aider
```
