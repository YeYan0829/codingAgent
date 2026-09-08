# GitHub 展示素材清单

## 展示方式

GitHub 首页以真实截图为主。截图负责说明产品能做什么，短 GIF 只补充静态图片无法表达的状态变化。
不再制作需要旁白、字幕和大量剪辑的完整演示视频。

README 的首轮素材控制为三张截图和一张 GIF。人工验收完成后，可以再补一张验证状态截图和一张权限拒绝 GIF。

## README 页面顺序

| 顺序 | 页面内容 | 使用的素材 | 读者应理解的内容 |
| --- | --- | --- | --- |
| 1 | 标题与一句话定位 | 无 | CodeAgent 在 VS Code 中运行，修改先隔离，交付由用户决定 |
| 2 | 产品主图 | `overview.png` | 时间线、原生 Diff 和待交付修改属于同一次任务 |
| 3 | 三项核心价值 | 主图下的短文字 | 过程可见、命令受控、修改可审查 |
| 4 | 一次任务如何完成 | 现有 Mermaid 流程图 | 从任务到 Accept/Discard 的完整路径 |
| 5 | 用户控制 | `approval.png` | 受保护操作会先解释原因并等待用户决定 |
| 6 | 会话恢复 | `history-switch.gif` 和 `history.png` | 历史会话可以查找并直接恢复 |
| 7 | Quick Start | 安装命令与固定任务文本 | 访客可以复现同一条产品链路 |
| 8 | 工程亮点 | 现有短段落和 docs 链接 | worktree、sandbox、验证状态和上下文控制解决什么问题 |
| 9 | 验证与限制 | 自动测试摘要、人工验收链接 | 展示结论有证据，同时保留明确边界 |

评测结果只有在正式批次完成后才加入首页。未完成时保留简短说明，不用单个成功任务代替整体成绩。

## 必需截图

### 1. 产品主图：`overview.png`

使用 Quick Start 仓库已经完成的任务。左侧显示 Explorer，中央打开 `calculator.py` 的原生 Diff，
右侧显示 CodeAgent 时间线和 Current Delivery。

画面中需要同时看见：

- 用户提交的修复任务；
- 一组已经完成的工具活动；
- 当前验证已通过；
- 一个待审查文件；
- 中央 Diff 中的小范围修改。

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

打开 History，并让列表中至少出现两个无敏感信息的 Session。当前仓库的会话可以点击，其他仓库只显示摘要。

README 配文：

> Session 保存在本地。重载窗口后仍可查找历史，并在对应仓库中恢复任务、修改和验证状态。

如果没有合适的跨仓库会话，只截取当前仓库列表。不要为了画面伪造额外仓库或会话结果。

## 必需 GIF

### History 切换：`history-switch.gif`

只录制 5～8 秒：

1. 从当前会话点击 History；
2. 点击另一个当前仓库的会话；
3. 界面直接返回，并显示所选会话的标题与时间线。

GIF 用来说明“选择后立即进入会话”这一交互。画面不包含任务执行等待、模型请求或终端输出。

README 配文：

> 在 History 中选择会话后，界面会直接恢复对应时间线。

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
├── history-switch.gif
├── validation-state.png       # 可选
├── approval-reject.gif        # 可选
└── delivery-actions.png       # 可选
```

- PNG 使用 1440×900 或 1600×1000，保持相同窗口比例。
- GIF 宽度使用 900～1100 像素，8～10 fps，单个不超过 4 MB。
- GIF 不带声音，首尾停留约半秒，并保持循环后容易理解。
- VS Code 使用同一主题、字体大小和侧栏宽度。
- README 中按页面宽度显示主图。功能截图不要并排压缩成难以阅读的小图。

需要压缩 GIF 时，可以从一段很短的 MP4 直接生成：

```bash
ffmpeg -i history-switch.mp4 \
  -vf "fps=8,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=bayer" \
  -loop 0 docs/assets/showcase/history-switch.gif
```

## 最省事的采集顺序

1. 复用已经完成的 Quick Start Session，打开 Diff，截取 `overview.png`。
2. 打开 History，截取 `history.png`，随后录制 5～8 秒的 `history-switch.gif`。
3. 新建 Quick Start Session，停在 worktree Approval，截取 `approval.png`。
4. 完成人工清单 E、G、H 后，再决定是否补充可选素材。

前三步不需要重放完整任务，也不需要剪出连续故事。每份素材只证明一个用户可见行为。

## 发布前检查

- [ ] 素材来自当前冻结 wheel 和 VSIX。
- [ ] API Key、用户名、主目录、Session ID 和通知内容均不可见。
- [ ] source、Diff、时间线和验证状态来自同一个真实 Session。
- [ ] Approval 截图没有把未验证的 Reject 行为写成已通过。
- [ ] 图片在 GitHub README 的正常宽度下仍能读清按钮与代码。
- [ ] GIF 离开配文也能看懂开始状态和结束状态。
- [ ] README 链接到 Quick Start、当前验证记录和详细限制。
