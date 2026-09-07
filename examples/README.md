# Examples

- [`showcase/`](showcase/)：最小离线 fixture，用于体验隔离修改、验证、Diff 与 Accept/Discard；
- [`eval-tasks/`](eval-tasks/)：三个可重复的真实仓库任务、准备脚本和 workspace 外 oracle；
- [`eval-repos/`](eval-repos/)：上述任务使用的无嵌套 `.git` 上游仓库快照；
- [`buggy-repos/tiny-sort-bug/`](buggy-repos/tiny-sort-bug/)：只读分析冒烟 fixture。

快速体验从 [`showcase/README.md`](showcase/README.md) 开始。仓库快照不是可直接复用的已修改 workspace；准备脚本会
复制快照、初始化干净 Git baseline，并拒绝覆盖已有目录。
