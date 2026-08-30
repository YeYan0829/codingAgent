# Search and Read Tools Technical Design

> 实现状态：本设计的搜索与读取契约已实现。文中保留的 readonly/execution 表述是当时的设计输入；当前 Session 初始读取 source，首次受保护操作后统一读取按需创建的 Agent worktree。搜索/读取 schema 和 PathGuard 边界未因此改变。

本文定义 Stage 2 第一批搜索与读取工具的实现契约。设计输入见 [Stage 2 Research（历史快照）](archive/STAGE2_RESEARCH_2026-08.md)，当前 Runtime 边界见[当前 Runtime 实现](ARCHITECTURE.md)。

本文只覆盖 Search/Read。它不设计编辑、Command Profiles、容器 sandbox 或 SWE-bench adapter；实现必须继续复用 `ToolRegistry`、`DefaultPolicy`、`PathGuard` 和敏感路径规则。

状态：2026-08-26 review 通过并完成首批实现。实现有两项局部调整：`build_fs_tools` 在未显式注入时构造 `RepositorySearchService`；`git_diff_stat` 与 `git_diff` 共用可选 `path/context_lines` schema。两者均未开放任意 executable 或 flags。

## 1. Goals

- 使用成熟 repository search 替换 Python 全量遍历；
- 让文件列举和文本搜索共享 repository visibility/ignore 语义；
- 提供正则、literal、case、glob、context 和结果上限；
- 严格读取 UTF-8 文本，不静默替换非法字节；
- 使用稳定 Git porcelain parser 返回真实 status；
- 让 Agent 能查看有界的实际 Git diff；
- 对 invalid arguments、path rejection、tool unavailable、timeout、domain failure 和 infrastructure failure 做最小但可靠的区分。

## 2. Non-goals

- symbol、reference、call graph、tree-sitter、LSP 或 RAG；
- Python search fallback；
- 跟随 symlink；
- 自动识别非 UTF-8 encoding；
- arbitrary `rg` 或 Git flags；
- Git history、commit search、blame 或 staged/index 编辑；
- 为 Docker、SWE-bench 或远程执行建立新 backend；
- 修改 readonly/execution session 模型。

## 3. Design Decisions

### D1. ripgrep 是 required dependency

`search_text` 使用固定构造的 `rg --json` argv，`find_files` 使用固定构造的 `rg --files --null` argv。不实现 Python fallback。

CLI 构造 registry 时解析并冻结可信 `rg` absolute path；找不到时 service 保留 unavailable 状态，不使整个 registry 构造失败。工具被调用时返回 `tool_unavailable`，以便 readonly 能力中的其他工具继续可用。通过 PATH 解析出的 executable 如果位于 active workspace 内必须拒绝。

Stage 2 不下载或安装 ripgrep。安装要求进入 README/测试文档。

### D2. Runtime 保持最终安全边界

ripgrep 只负责搜索和 repository visibility，不负责授权。每个输入 root、每个返回 path 都重新经过 `PathGuard`；敏感路径内容永不进入 match/context。symlink 不跟随，即使未来修改 rg 参数也不能移除 Runtime 复检。

### D3. 默认 repository visibility

默认搜索和文件列举：

- 遵守 `.gitignore`、`.ignore`、`.rgignore` 和 Git exclude；
- 不包含 hidden；
- 不搜索 binary；
- 不 follow symlink。

`include_hidden=true` 是受控扩展，只增加 ripgrep `--hidden`；ignore 和敏感路径规则不变。显式 `path` 可以指向一个非敏感的 tracked hidden file。Stage 2 不建立“只枚举 tracked hidden files”的额外 Git/ripgrep 联合管线。

### D4. 保留工具名称，收紧并扩展 schema

保留 `list_dir`、`show_tree`、`read_file`、`search_text`、`find_files`、`git_status` 和 `git_diff_stat`，减少 prompt、session 和测试迁移成本；新增 `git_diff`。

`list_dir` 和 `show_tree` 是显式目录观察工具，不承诺与 repository search 完全相同的 ignore 视图；它们继续标记或隐藏敏感名称。统一 visibility 的硬要求只作用于 `search_text` 和 `find_files`。

### D5. 只支持严格 UTF-8

`read_file` 使用 `errors="strict"`。非法 UTF-8 返回 `unsupported_text_encoding`；NUL binary 返回 `unsupported_file_type`。不进行 replacement、locale encoding 或 encoding guessing。

### D6. Git 使用机器协议，Agent 读取格式化结果

`git_status` 内部固定运行：

```text
git status --porcelain=v1 -z --untracked-files=all
```

Runtime 解析 NUL records，再生成稳定的人类可读内容和结构化 metadata。Agent 不直接解析 raw porcelain。

`git_diff` 只读取当前 active workspace 相对 index 的 unstaged diff。Stage 2 不加入 staged/base/commit selector，因为 Agent 不执行 Git staging。`git_diff_stat` 继续保留为低成本概览。

### D7. 最小扩展 ToolResult

`ToolResult` 增加可选 `error_code: str | None = None`。不引入新的通用 result hierarchy。

约定：

- `ok=true`：工具完成；零搜索结果、clean Git status 和 empty diff 都属于成功；
- `ok=false`：请求未形成有效领域结果；
- `truncated=true`：返回内容或结果集合因设计上限不完整；
- `error_code`：稳定机器分类；
- `error`：面向模型和用户的简短解释；
- `metadata`：工具特有的有界结构。

本设计使用以下 error code：

| code | 含义 |
| --- | --- |
| `invalid_arguments` | schema 之外的组合约束或非法 regex/glob |
| `path_rejected` | workspace escape、symlink escape 或敏感内容路径 |
| `not_found` | 输入路径不存在 |
| `unsupported_file_type` | 目录、binary 或其他非普通文本文件 |
| `unsupported_text_encoding` | 不是合法 UTF-8 |
| `tool_unavailable` | `rg` 或 `git` executable 不可用 |
| `timed_out` | 固定工具进程超时 |
| `command_failed` | rg/Git 已启动但返回无法解释为正常领域结果的错误 |
| `internal_error` | parser、I/O 或其他 Runtime 基础设施失败 |

Policy/Approval denial 由 `AgentRunner` 使用既有事件链表达；实现本设计时同步填充对应 `ToolResult.error_code`，但不改变 Policy 决策。

## 4. Agent-facing Tools

所有 path 和 glob 使用 workspace-relative POSIX 表示。schema 必须设置 `additionalProperties: false`。

### 4.1 `search_text`

用途：在 repository 可见文件中搜索 literal 或 ripgrep regex。

```json
{
  "query": "required non-empty string",
  "path": ".",
  "mode": "literal | regex",
  "case": "sensitive | insensitive | smart",
  "include": ["*.py", "src/**"],
  "exclude": ["tests/fixtures/**"],
  "include_hidden": false,
  "context_lines": 0,
  "max_results": 100
}
```

约束：

- `query` 长度 1..4096；
- `path` 默认 `.`，可以是目录或单个文件；
- `mode` 默认 `literal`；
- `literal` 把 query 作为完整普通文本，`|`、括号、字符类和 `.*` 均没有正则含义；使用这些语义必须显式传 `mode=regex`；
- `case` 默认 `smart`；
- include/exclude 各最多 20 项，每项最长 256；
- glob 不能包含 NUL，不能以 `/` 开始，也不能通过 `..` 表达 workspace 外路径；
- `context_lines` 范围 0..3；
- `max_results` 范围 1..200，默认 100。

固定 argv 由 Runtime 映射：

```text
rg --json --no-config --no-follow
   [--fixed-strings]
   [--case-sensitive | --ignore-case | --smart-case]
   [--glob include] [--glob !exclude]
   [--hidden]
   [--context N]
   -- query path
```

`--no-config` 防止用户级 ripgrep 配置静默改变工具语义。不得加入 `--no-ignore`、`--follow`、`--text` 或允许模型传递其他 flags。

输出按 ripgrep 产生顺序组织，每个 match/context 行使用：

```text
path:line:column: text
```

metadata：

```json
{
  "match_count": 12,
  "files_with_matches": 3,
  "limit_reason": null,
  "search_root": "src",
  "mode": "literal",
  "query_mode": "literal",
  "query_interpretation": "exact literal text; regex metacharacters are not interpreted",
  "case": "smart"
}
```

工具 schema 和每次结果都回显这一解释边界，避免 Agent 把带 `|` 的查询误当成正则并在零结果后重复改写查询。

`limit_reason` 为 `max_results`、`output_bytes` 或 `timeout`。timeout 返回 `ok=false/error_code=timed_out`；已经读取的 partial matches只保存在 metadata 计数，不作为成功内容返回，避免模型把不完整结果误认为完整搜索。

ripgrep exit code 0 表示有结果，1 表示无结果；两者都返回 `ok=true`。exit code 2 或 JSON/parser 错误返回失败。

### 4.2 `find_files`

用途：使用与 `search_text` 相同的 ignore/hidden/symlink 视图列举文件。

```json
{
  "path": ".",
  "include": ["*.py", "src/**"],
  "exclude": ["tests/fixtures/**"],
  "include_hidden": false,
  "max_results": 200
}
```

固定 argv：

```text
rg --files --null --no-config --no-follow
   [--glob include] [--glob !exclude]
   [--hidden]
   path
```

返回排序后的 workspace-relative POSIX path，每行一个。Runtime 必须对每条结果执行 PathGuard/sensitive 复检。敏感文件不返回真实名称还是标记名称，继续由 `SensitiveListingMode` 决定；但不论展示模式为何，其内容均不可读取或搜索。

metadata 包含 `file_count`、`limit_reason`、`search_root`。到达 `max_results` 或 byte limit 时 `ok=true/truncated=true`。

### 4.3 `read_file`

```json
{
  "path": "src/module.py",
  "start_line": 1,
  "end_line": 200
}
```

约束：

- path 必填；
- 行号从 1 开始；
- `start_line >= 1`；
- `end_line >= start_line`；
- 单次最多请求 1000 行；
- 未提供范围时从第一行开始读取，仍受 line/byte 上限限制。

实现以流式方式扫描文件，不把任意大小文件整体载入内存；完整 bytes 用于稳定 SHA-256，文本流使用严格 UTF-8 decode。返回正文仍受行数/byte 上限约束并保留通用 LF 展示语义；扫描继续到 EOF 以确定文件总行数。metadata 提供：

```json
{
  "path": "src/module.py",
  "start_line": 1,
  "end_line": 200,
  "total_lines": 846,
  "requested_range": {"start_line": 1, "end_line": 200},
  "returned_range": {"start_line": 1, "end_line": 200},
  "file_total_lines": 846,
  "has_more_before": false,
  "has_more_after": true,
  "content_bytes": 7312
}
```

`total_lines` / `file_total_lines` 始终表示整个文件的行数，不再因为显式请求只覆盖文件前段而返回 `null`。读取正文超过 64 KiB，或省略范围时文件超过默认 1000 行窗口，会标记截断；实际常量在实现中集中定义。

显式读取非敏感 hidden file 是允许的。敏感路径、symlink escape、binary 和非法 UTF-8 分别返回稳定 error code。

### 4.4 `list_dir` 与 `show_tree`

保留现有用途，但收紧参数：

- `list_dir.path` 必须是 workspace-relative directory；
- `show_tree.max_depth` 范围 1..4；
- 两者增加 entry 上限和 byte 上限；
- metadata/path 不再向模型暴露 host absolute path，改为 workspace-relative path；
- symlink entry 可以显示为 symlink marker，但不递归进入；指向 workspace 外时不能泄漏目标；
- 目录读取失败不能静默吞掉，`show_tree` 应设置 `truncated` 并在 metadata 记录 `unreadable_count`。

它们不调用 ripgrep，也不承诺应用 repository ignore；目的是观察用户显式指定的物理目录。Agent 需要 repository 文件视图时应使用 `find_files`。

### 4.5 `git_status`

Agent-facing schema 保持空对象。

格式化内容每行：

```text
XY path
R  old_path -> new_path
```

metadata：

```json
{
  "entries": [
    {"index": "M", "worktree": " ", "path": "a.py", "original_path": null}
  ],
  "entry_count": 1
}
```

entries 最多 200 项；更多结果设置 `truncated=true`。parser 必须正确处理空格、换行、引号、非 ASCII 和 rename path。submodule 只按 porcelain v1 当前状态展示，本阶段不展开内部状态。

### 4.6 `git_diff`

```json
{
  "path": ".",
  "context_lines": 3
}
```

固定命令：

```text
git diff --no-ext-diff --no-textconv --unified=N -- path
```

约束：

- 只允许一个 workspace-relative path，默认 `.`；
- `context_lines` 范围 0..10；
- 不允许 revision、raw flags 或 external diff；
- 只展示 unstaged working-tree diff；
- stdout 最多 64 KiB，超出时保留 head/tail 并设置 `truncated=true`；
- stderr 最多 8 KiB；
- empty diff 返回 `ok=true`。

metadata 包含 `path`、`content_bytes`、`returncode`。Git binary file 只显示 Git 自身的 binary difference marker，不向 Agent 输出 binary patch。

### 4.7 `git_diff_stat`

实现与 `git_diff` 共用可选 `path/context_lines` schema，便于查看同一范围的 stat 和 patch；Runtime 仍固定加入 `--no-ext-diff --no-textconv`，并使用相同 timeout、byte limit 和错误映射。Agent 不能传 revision、executable 或 Git flags。

## 5. Runtime Components

不建立通用 backend 或 Shared Tool Contract。只增加当前确有共享语义的两个内部组件。

### 5.1 `RepositorySearchService`

建议新文件：`codeagent/runtime/repository_search.py`。

职责：

- 验证 search/find 参数组合；
- 构造时使用 `shutil.which("rg")` 解析并冻结 absolute executable，拒绝 active workspace 内的 executable；
- 构造固定 argv；
- 启动 rg、处理 timeout 和输出上限；
- 解析 JSON Lines 或 NUL file list；
- 对结果执行 PathGuard 与敏感规则复检；
- 返回领域结构给 tool handler。

它不负责 ToolSpec、Policy、模型文本格式或 session event。

搜索进程使用 `shell=False`、`stdin=DEVNULL`、Linux 独立 process group 和最小环境。可以复用 `local_executor.py` 中小范围的 POSIX termination helper；不抽取通用 Execution Backend。

### 5.2 `GitReadService`

建议在 `codeagent/tools/git_read.py` 内先保持小型 service/class，不新建基础设施模块。职责：

- 固定 Git argv；
- timeout 和有界 stdout/stderr；
- porcelain `-z` parsing；
- diff/stat 文本输出；
- path 复检和错误映射。

如果后续 Candidate/workspace audit 也迁移到同一 porcelain parser，再将 parser 提取到 `codeagent/runtime/git_status.py`。本次不要为了未来复用先搬迁所有 Git 调用。

### 5.3 Tool handlers

`codeagent/tools/fs_read.py` 保留 ToolSpec 组装、内容格式化和直接文件/目录读取；`search_text`、`find_files` 委托 `RepositorySearchService`。

`codeagent/tools/git_read.py` 负责三个 Git Agent-facing tools。

`codeagent/tools/base.py` 只增加可选 `error_code`。`AgentRunner` 对 policy/approval denial 填充相应 code，其他行为不变。

## 6. Data Flow

### 6.1 Search

```text
Agent search_text arguments
→ Agent-facing JSON schema
→ handler 执行同等的 Runtime 参数与组合验证
→ PathGuard(input root)
→ RepositorySearchService builds frozen rg argv
→ rg subprocess
→ bounded JSON Lines parser
→ PathGuard + sensitive filter for every result
→ formatted ToolResult + bounded metadata
→ existing tool_result session event
→ model observation
```

### 6.2 Git status/diff

```text
Agent arguments
→ handler validation
→ PathGuard(optional path)
→ fixed Git argv in active_root
→ bounded stdout/stderr
→ porcelain parser or diff formatter
→ ToolResult
→ existing event/context path
```

工具执行仍只读取 `WorkspaceContext.active_root`。Readonly session 读取 source，execution session 读取 task worktree；本设计不改变 active workspace identity。

## 7. Limits and Process Behavior

首批实现常量：

| 项目 | 上限 |
| --- | --- |
| search query | 4096 chars |
| include/exclude | 各 20 项、每项 256 chars |
| search matches | 默认 100，最大 200 |
| find files | 默认 200，最大 1000 |
| read request | 最大 1000 lines / 64 KiB |
| list/tree entries | 1000 |
| git status entries | 200 |
| search/find formatted output | 64 KiB |
| Git diff stdout | 64 KiB head/tail |
| subprocess stderr | 8 KiB |
| rg/Git timeout | 10 seconds |

实现不能用无上限 `subprocess.run(capture_output=True)` 读取搜索结果。rg stdout 应逐条解析；stderr 写入临时文件或由有界 reader 消费，避免 pipe deadlock。达到 match/file/byte 上限后终止 rg 进程组并返回成功且 `truncated=true`，这与 timeout 失败不同。

Git diff 可以写入 session 外临时文件后做 head/tail 摘要；本阶段不把 readonly Git 输出持久化为长期 artifact。

## 8. Errors

异常映射必须集中在 service/handler 边界，不能把 Python exception 文本当作稳定协议。

关键行为：

- invalid regex：`invalid_arguments`；
- rg exit 1：成功、零结果；
- rg 不存在：`tool_unavailable`；
- Git workspace 非 repository：`command_failed`，保留当前清晰提示；
- workspace escape/sensitive path：`path_rejected`；
- 文件不存在：`not_found`；
- 非 UTF-8：`unsupported_text_encoding`；
- search/diff 达到输出上限：成功且 truncated；
- subprocess timeout：`timed_out`；
- JSON/porcelain parser 不符合预期：`internal_error`，不得返回可能错误解析的部分结果。

## 9. Security Invariants

- Agent 只能提交结构化参数，不能提交 executable、argv 或 shell；
- 所有 subprocess 使用 argv list、`shell=False` 和不可交互 stdin；
- `--no-config` 防止用户 rg config 改变固定协议；
- Git 禁用 external diff/textconv；
- 所有 Agent path 是 workspace-relative POSIX 表示；
- 输入和输出路径都经过 Runtime 复检；
- 不 follow symlink；
- 敏感路径可以按现有 listing mode 展示名称，但不能泄漏内容；
- metadata 不包含 host absolute path；
- output 和 metadata 均有数量/byte 上限；
- 搜索工具和固定 argv 的 `git_status`、`git_diff`、`git_diff_stat` 都使用 `PermissionLevel.READ`，不逐次 approval；未来 Git 写操作或更通用 Git command 需要重新评估权限。

## 10. Required Code Changes

### 修改

- `codeagent/tools/base.py`
  - 为 `ToolResult` 增加可选 `error_code`。
- `codeagent/tools/fs_read.py`
  - 严格 schema、relative metadata、UTF-8 read；
  - search/find 委托新 service；
  - list/tree 上限和错误分类。
- `codeagent/tools/git_read.py`
  - porcelain parser、真实 diff、bounded output 和错误分类。
- `codeagent/runtime/runner.py`
  - policy/approval denial 补稳定 error code。
- `codeagent/cli.py`
  - 最终无需修改；`build_fs_tools` 默认构造 service，同时保留显式注入点供测试使用。
- `pyproject.toml`、README 和测试文档
  - 声明 Linux 环境需要 `rg` executable；Python package dependency 不负责安装系统 binary。

### 新增

- `codeagent/runtime/repository_search.py`
- `tests/test_repository_search.py`

### 扩展测试

- `tests/test_tools_fs_read.py`
- `tests/test_tools_git_read.py`
- `tests/test_runner_approval.py` 或现有 denial 测试模块。

## 11. Test Plan

### Repository search

- literal、regex、case modes；
- include/exclude；
- `.gitignore`、`.ignore`、hidden 默认行为；
- `include_hidden` 与敏感文件过滤；
- symlink 不跟随及结果 PathGuard 复检；
- 文件名含空格、冒号、换行和非 ASCII；
- context lines；
- no matches exit 1；
- invalid regex exit 2；
- rg missing；
- timeout；
- match/file/byte limit 与进程终止；
- JSON partial/corrupt result fail closed。

### Read/list/tree

- strict UTF-8、invalid UTF-8、NUL binary；
- line range validation；
- large file bounded read；
- relative metadata；
- sensitive path、absolute/parent/symlink escape；
- entry/depth/output truncation；
- unreadable directory显式反馈。

### Git

- clean/modified/untracked/deleted/rename；
- 特殊 path 和 NUL parser；
- empty/normal/large/binary diff；
- path filter 与 escape；
- Git executable missing、non-repository、timeout；
- external diff/textconv 不执行。

### Integration

- ToolRegistry 向 readonly/execution session 注入相同读取工具，但读取各自 active root；
- ToolSpec → ModelTool schema；
- Policy/Approval denial error code；
- Fake model 能搜索、读取、查看 Git diff 并返回最终总结；
- 现有敏感文件和 readonly regression 不退化。

实现完成后，在至少一个本地 fixture 使用正式 CLI 验收：定位 bug、读取相关文件、查看 clean status；execution edit 由后续阶段完成前，只验证搜索/读取工具组合，不伪造完整 bugfix 成功。

## 12. Implementation Sequence

1. `ToolResult.error_code` 与 schema tests；
2. `RepositorySearchService`、rg process/limits/parser 单元测试；
3. 接入 `search_text`、`find_files`；
4. 收紧 `read_file`、`list_dir`、`show_tree`；
5. Git porcelain parser、`git_diff` 和 bounded output；
6. registry/provider schema 和 Agent loop 集成测试；
7. README、TESTING、ARCHITECTURE 更新；
8. 全量 pytest 与 readonly CLI dogfood。

每一步保持可回归，不与 Atomic Edit 或 Command Profiles 混合提交。

## 13. Review Resolutions

review 已解决实现前的两个问题：

1. `include_hidden=true` 允许枚举和搜索所有非 ignored、非敏感 hidden 文件，不增加 tracked-only 联合过滤；
2. 三个固定 argv Git 读取工具统一为 `READ`，不逐次 approval。

实现未发现需要改变产品级 design decision 的新问题。上限只能在测试或 dogfood 证据下调整，不能开放无界输出。

## 14. Future Considerations

本阶段明确不做：

- Python search fallback；
- symbol/LSP/index/RAG；
- persisted search artifacts 或 search cache；
- Git history/blame/commit selector；
- staged diff 与 Git 写操作；
- remote/container search backend；
- arbitrary rg/Git argv；
- 通用 subprocess backend 抽象。

未来 container/SWE-bench 环境只需保证 active workspace 中存在兼容 `rg`/Git，或在届时替换 service backend；当前 Agent-facing path/schema 不包含 host absolute path，因此无需为尚未实现的容器增加额外层。
