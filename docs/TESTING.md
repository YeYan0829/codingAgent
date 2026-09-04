# 测试说明

## 运行方式

```bash
.venv/bin/pytest -q
.venv/bin/python -m compileall -q codeagent tests
git diff --check
```

真实 Bubblewrap integration 默认跳过；指定可信测试 executable 后运行：

```bash
CODEAGENT_TEST_BWRAP=/usr/bin/bwrap .venv/bin/pytest -q tests/test_bubblewrap_integration.py
```

真实 SWE-bench Docker integration 也默认跳过，显式启用：

```bash
CODEAGENT_RUN_SWEBENCH_DOCKER=1 \
  .venv/bin/pytest -q tests/test_swebench_docker_integration.py
```

## 覆盖范围

- Search/Read schema、ignore/hidden、SensitivePath、PathGuard、symlink、UTF-8、截断和 Git fixed argv；
- `read_file` 的请求/返回范围、稳定文件总行数、前后剩余内容标记与完整 bytes SHA；
- `search_text` 默认 literal、显式 regex、schema 默认值和结果中的实际 query interpretation；
- Atomic Edit 四种 operation、SHA conflict、目录副作用、rollback、rollback failure 和 resume recovery gate；
- `run_command` 闭合 schema、cwd/limits、fixed inner bash、host `shell=False`、pipeline/redirect/quoting/chaining；
- output cap、timeout、process-group TERM/KILL、setup/payload 失败分类和 bwrap unavailable fail-closed；
- Permission 的 READ/WRITE/NETWORK、ONCE/SESSION、canonical symlink、文件/目录语义、WRITE parent expansion、hard deny 和 identity 复检；
- deterministic MountPlan、root/Candidate/source/Git/runtime/sensitive `/tmp` 与 OFF/HOST network 翻译；
- command create/update/delete/rename provenance、失败/timeout 修改仍为合法 Candidate、after audit failure 才 taint、进程树不确定才 recovery；
- Candidate revision、validation stale、evidence-exists accept gate、统一 patch、source 三方合并、discard/resume；
- Fake Model 经正式 ToolRegistry/AgentRunner 完成 read → command/edit；不接真实 LLM。
- Context 新旧 Event identity 投影、ToolCall terminal outcome 配对和 provider protocol error 边界；
- old tool residue、Recent Tool Observation、range provenance、token 预算、完整 UserTurn 淘汰和 minimum-set 明确失败；
- command residue 保留结构化 status/exit code 与有界 stdout/stderr tail，并兼容旧 Event 的 Runtime markers；
- Context 派生对象保持内存态，不生成 snapshot/residue/budget artifact 目录。
- 执行切片保持单一 UserTurn/UserMessage、Resume `/continue`、原始请求延续、协议对完整和总 ModelStep 预算明确终止；
- Context 最低集合超限会记录结构化终止事件并由 CLI 清楚展示，不以 traceback 或伪 Assistant Final 结束；
- eval task 模板不使用 Sandbox private HOME 下的 `~/.cache`，prepare 阶段生成含实际绝对 Python 路径的 workspace 任务文档。
- SWE-bench prepared source 准入、Docker execution projection、gold preflight、prediction/grader 状态分离和 token usage 聚合。

真实 Bubblewrap 测试额外验证 Candidate 写成功、source/Git metadata 写失败、默认 network namespace 无法访问 host localhost、批准 NETWORK 后可访问，以及 Python/pytest 与 Make 两类已有工具链。

## 测试不证明

- 模型是否会聪明选择工具、权限或验证命令；
- validation 是否充分；
- domain/port 网络隔离、cgroup 资源配额或通用 syscall policy；
- repo-local secret 文件自动识别；
- 所有 Linux distribution、WSL/kernel 或 nested-container 组合。
- 长工具轨迹中模型能否及时从探索切换到编辑，或在总步骤与 Context 容量内完成真实任务。

最后一项必须由真实模型 benchmark 衡量。当前 GLM/DeepSeek 十题基线与状态口径见
[Benchmark 说明](BENCHMARKING.md)；历史 HTTPX 审计保存在 `archive/evaluation/`。
