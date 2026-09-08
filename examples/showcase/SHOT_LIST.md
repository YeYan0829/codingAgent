# GitHub 展示素材清单

## 展示方式

GitHub 首页以真实截图为主。截图负责说明关键状态。短 GIF 可以补充时间线中的执行过程。

README 的首轮素材控制为三张截图。可选增加一张时间线 GIF，不要求重新运行任务。

## 当前截图取舍

现有图片已经覆盖主任务、Approval、欢迎页和 History。首版 README 只使用其中三类：

- 主任务图：保留 Diff、时间线、验证结果和 Accept/Discard；重新截图时收起终端。
- Approval：当前画面可以直接使用，它清楚显示了创建隔离工作区的原因和用户操作。
- History：使用 Current 页面即可；All workspaces 中存在测试会话标题，不放入公开首页。

欢迎页适合安装文档，不进入 README 首屏。它主要说明任务入口，没有提供执行结果。

当前主任务图中的终端同时显示旧失败和右侧新验证通过。这个状态符合 Accept 前 source 不变的设计，
但陌生读者容易误解。终端还显示本机用户名和设备名，因此公开版本应收起终端后重新截取。

三张竖图都显示了并列的 Codex 和 Chat 标签。如果操作方便，重新截图前隐藏这两个 View，
避免读者把它们误认为 CodeAgent 的组成部分。这不影响验收结论，也不是产品修复项。

## README 页面顺序

| 顺序 | 页面内容 | 使用的素材 | 读者应理解的内容 |
| --- | --- | --- | --- |
| 1 | 标题与一句话定位 | 无 | CodeAgent 在 VS Code 中运行，修改先隔离，交付由用户决定 |
| 2 | 产品主图 | `overview.png` | 时间线、原生 Diff 和待交付修改属于同一次任务 |
| 3 | 执行过程 | 可选 `timeline-review.gif` | 从任务、工具活动到验证结果的时间顺序 |
| 4 | 一次任务如何完成 | 现有 Mermaid 流程图 | 从任务到 Accept/Discard 的完整路径 |
| 5 | 用户控制 | `approval.png` | 受保护操作会先解释原因并等待用户决定 |
| 6 | 会话恢复 | `history.png` | 历史会话可以查找，并在对应仓库恢复 |
| 7 | Quick Start | 安装命令与固定任务文本 | 访客可以复现同一条产品链路 |
| 8 | 工程亮点 | 现有短段落和 docs 链接 | worktree、sandbox、验证状态和上下文控制解决什么问题 |
| 9 | 验证与限制 | 自动测试摘要、人工验收链接 | 展示结论有证据，同时保留明确边界 |

评测结果只有在正式批次完成后才加入首页。未完成时保留简短说明，不用单个成功任务代替整体成绩。

## 必需截图

### 1. 产品主图：`overview.png`

使用 Quick Start 仓库已经完成的任务。左侧显示 Explorer，中央打开 `calculator.py` 的原生 Diff，
右侧显示 CodeAgent 时间线和 Current Delivery。

主图只需要同时看见：

- 能说明任务目标的标题；
- 当前验证已通过；
- Current Delivery 中的待审查文件；
- 中央 Diff 中的小范围修改。

用户消息和工具活动不要求塞进同一张图。它们可以由下方的时间线 GIF 或单独截图说明。

README 配文：

> CodeAgent 在隔离工作区中完成修改和验证。开发者可以查看执行过程与原生 Diff，再决定是否交付到源仓库。

这张图承担首屏展示，不再用 GIF 代替。静态主图打开快，也方便读者停留查看文字和 Diff。

### 2. 权限审批：`approval.png`

使用尚未点击 Allow 的 worktree Approval。右侧应完整显示操作原因、目标路径和按钮。
左侧或中央保留未修改的 source 文件。

README 配文：

> 首次受保护操作会暂停并说明原因。只有用户允许后，CodeAgent 才会创建隔离工作区并继续执行。

当前只完成了 Approval 允许路径的人工验收。截图不能写成 Reject 或权限范围也已经通过。

### 3. 历史会话：`history.png`

打开 Current History，保留至少一个没有敏感信息的 Session。标题应与 Quick Start 任务对应。

README 配文：

> Session 保存在本地。重载窗口后仍可查找历史，并在对应仓库中恢复任务、修改和验证状态。

All workspaces 截图包含无关测试会话标题，不用于公开首页。跨仓库行为通过文字和验证记录说明。

## 可选时间线 GIF：`timeline-review.gif`

如果希望展示完整过程，可以复用现有 Session 录制 6～10 秒：

1. 中央保持原生 Diff 不动；
2. 右侧从用户任务缓慢滚到一组已完成的工具活动；
3. 最后停在验证通过和 Current Delivery。

这段 GIF 不执行模型、不重新运行任务，也不需要拼接多个片段。只裁掉录制开始和结束的空白即可。
README 仍先放静态主图，保证加载前和暂停阅读时能看到清楚的最终结果。

README 配文：

> 任务中的代码读取、命令和验证按时间显示。完成后，当前修改与对应的测试状态会留在同一会话中。

## 验收完成后可补的素材

### 验证状态：`validation-state.png`

完成清单 E 后再制作。画面并列展示旧测试结果已经过期，以及重新测试后恢复为当前结果。
如果一张图无法清楚呈现，可以使用两张同尺寸截图，不必制作 GIF。

### 权限拒绝：`approval-reject.gif`

完成清单 C 的 Reject 和权限范围后再制作。GIF 控制在 6～10 秒，只保留点击 Reject、任务停止越权操作、
界面给出结果三个动作。

### 交付选择：`delivery-actions.png`

完成清单 G 和 H 后再制作。截图显示 Accept 与 Discard，但配文必须分别说明：Accept 应用修改，
Discard 清空尚未交付的修改。

## 文件位置与规格

素材统一放在 `docs/assets/showcase/`：

```text
docs/assets/showcase/
├── overview.png
├── approval.png
├── history.png
├── timeline-review.gif        # 可选
├── validation-state.png       # 可选
├── approval-reject.gif        # 可选
└── delivery-actions.png       # 可选
```

- 主图 PNG 使用 1440×900 或 1600×1000，保持 VS Code 窗口比例。
- Approval 和 History 竖图在 README 中以约 420 像素宽度显示；History 截掉下方大块空白。
- GIF 宽度使用 900～1100 像素，8～10 fps，单个不超过 4 MB。
- GIF 不带声音，首尾停留约半秒，并保持循环后容易理解。
- VS Code 使用同一主题、字体大小和侧栏宽度。
- README 中按页面宽度显示主图。功能截图不要并排压缩成难以阅读的小图。

## 最省事的采集顺序

1. 复用已经完成的 Quick Start Session，打开 Diff，截取 `overview.png`。
2. 如果需要过程展示，保持 Diff 打开，只录制右侧时间线滚动。
3. 打开 Current History，截取 `history.png`。
4. 新建 Quick Start Session，停在 worktree Approval，截取 `approval.png`。
5. 完成人工清单 E、G、H 后，再决定是否补充其他素材。

以上步骤不需要重放完整任务，也不需要剪出连续故事。每份素材只证明一个用户可见行为。

## 发布前检查

- [ ] 素材来自当前冻结 wheel 和 VSIX。
- [ ] API Key、用户名、主目录、Session ID 和通知内容均不可见。
- [ ] source、Diff、时间线和验证状态来自同一个真实 Session。
- [ ] Approval 截图没有把未验证的 Reject 行为写成已通过。
- [ ] 图片在 GitHub README 的正常宽度下仍能读清按钮与代码。
- [ ] 如果加入可选 GIF，离开配文也能看懂开始状态和结束状态。
- [ ] README 链接到 Quick Start、当前验证记录和详细限制。
