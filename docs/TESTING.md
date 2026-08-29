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

## 覆盖范围

- Search/Read schema、ignore/hidden、SensitivePath、PathGuard、symlink、UTF-8、截断和 Git fixed argv；
- Atomic Edit 四种 operation、SHA conflict、目录副作用、rollback、rollback failure 和 resume recovery gate；
- `run_command` 闭合 schema、cwd/limits、fixed inner bash、host `shell=False`、pipeline/redirect/quoting/chaining；
- output cap、timeout、process-group TERM/KILL、setup/payload 失败分类和 bwrap unavailable fail-closed；
- Permission 的 READ/WRITE/NETWORK、ONCE/SESSION、canonical symlink、文件/目录语义、WRITE parent expansion、hard deny 和 identity 复检；
- deterministic MountPlan、root/Candidate/source/Git/runtime/sensitive `/tmp` 与 OFF/HOST network 翻译；
- command create/update/delete/rename provenance、失败/timeout 修改仍为合法 Candidate、after audit failure 才 taint、进程树不确定才 recovery；
- Candidate revision、validation stale、evidence-exists accept gate、统一 patch、source 三方合并、discard/resume；
- Fake Model 经正式 ToolRegistry/AgentRunner 完成 read → command/edit；不接真实 LLM。

真实 Bubblewrap 测试额外验证 Candidate 写成功、source/Git metadata 写失败、默认 network namespace 无法访问 host localhost、批准 NETWORK 后可访问，以及 Python/pytest 与 Make 两类已有工具链。

## 测试不证明

- 模型是否会聪明选择工具、权限或验证命令；
- validation 是否充分；
- domain/port 网络隔离、cgroup 资源配额或通用 syscall policy；
- repo-local secret 文件自动识别；
- 所有 Linux distribution、WSL/kernel 或 nested-container 组合。
