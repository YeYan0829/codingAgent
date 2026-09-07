# 展示视频镜头清单

## 所有视频的共同画面

- 使用 VS Code 深色默认主题和 100%～110% 缩放；隐藏通知、帐号名、API Key、绝对主目录和无关终端历史。
- 左侧保留 Explorer，中央显示任务或原生 Diff，CodeAgent 固定在右侧 Secondary Sidebar。
- 开始前确认 source `git status --short` 为空，记录候选 commit、模型、预算和任务 ID。
- 等待模型和测试的长时间片段可以剪辑；画面或字幕标出真实耗时和剪辑，不加速工具事件来伪装实时速度。
- 录制鼠标点击和产品可见事实，不展示或解说隐藏推理。

## A. 快速体验：60～90 秒

| 时间 | 画面 | 观众应看到的证据 |
| --- | --- | --- |
| 0:00～0:08 | 终端运行 `python3 verify.py` | 原始失败可复现，仓库离线可用 |
| 0:08～0:18 | 打开右侧 CodeAgent，新建任务并发送短请求 | 当前 repository、模型和任务输入清楚 |
| 0:18～0:30 | 审查并允许创建隔离 workspace | source 尚未改变，写操作进入 Candidate |
| 0:30～0:50 | 展开一组搜索/读取/编辑/命令活动 | 工具时间线、命令状态和 focused verification |
| 0:50～1:08 | 点击 changed file 打开中央原生 Diff | 实际 patch、小范围修改、validation current |
| 1:08～1:20 | 点击 Accept，再展示 source 状态 | 用户确认后 source 才发生变化 |

快速体验不讲 SWE-bench，不用“自主解决复杂仓库”作为字幕。

## B. Stop：30～45 秒

1. 使用 HTTPX 或 Click 新 Session 启动任务，等待出现至少两组读取/搜索活动。
2. 点击 Stop，保留 `Stopping after current operation` 到 idle 的状态变化。
3. 展示最后一个已完成或取消的工具事件，确认停止后没有新 step。
4. 展示 Candidate 和历史仍在；如需修正工作，结束本轮后发送新的 UserTurn，而不是宣称恢复旧执行栈。

## C. 多轮分析、实施与复核：60～90 秒

1. 使用 Click 仓库，在同一个 Session 发送“先分析、暂不修改”的第一轮请求。
2. 保留 Agent 根因和最小方案的简短结果；确认 source 和 Candidate 此时没有代码修改。
3. 发送“按刚才方案实现”的第二轮请求，不重复 issue；画面保留 Agent 读取历史后开始编辑和验证。
4. 打开原生 Diff，以代码审查者身份发送第三轮复核请求，不提前 Accept。
5. 展示 Agent 针对现有 Candidate 检查问号、感叹号和 `max_length`，以及新的 current validation。
6. 最后展示一个 Session 中的三个 UserTurn 和统一 Current Delivery，再 Accept 或留给主视频处理。

这条短片证明 Session 历史进入后续上下文、Candidate 跨 UserTurn 保留，以及用户可以根据中间结果改变下一步工作。
它不与执行切片 Continue 混用：自动切片不会产生新的用户消息，三次请求才是三次真实 UserTurn。

三轮输入依次为：

```text
先阅读 CODEAGENT_TASK.md 和相关实现，说明根因与最小修改方案，暂时不要修改文件。

按刚才的方案实现，补充回归测试，并运行任务文件要求的验证。

复核当前修改是否同时覆盖问号和感叹号，并确认没有改变 max_length 行为；运行相关测试，不要扩大修改范围。
```

## D. Budget waiting 与扩额：45～60 秒

1. 使用 README 中的 2/4 录制配置创建 HTTPX Session。
2. 让画面保留一次 2 步切片结束后自动继续的时间线，期间不点击 Continue。
3. 到达 `Step budget reached: 4 / 4` 后停留两秒，强调系统没有自动变成 8。
4. 点击 `Increase budget and continue`，输入 `+4`，展示 limit 变为 8 且仍属于原 UserTurn。
5. 视频字幕注明发布默认值为 12/48，录制降低上限只为缩短演示。

## E. Reject 或 Discard：30～60 秒

权限版本使用 environment/network demo：展示完整命令、Network access、once scope，点击 Reject，随后展示命令未执行、
授权未保存和 Agent 的失败说明。代码交付版本使用快速体验：完成 Candidate 后点击 Discard，展示 source 保持 baseline。
二者至少录一条；若都录制，分别命名为 Permission Reject 和 Candidate Discard，避免混淆。

## F. History：30～45 秒

1. 预先在两个 workspace 各保留一个无敏感信息的 Session。
2. 打开 History，在 Current 中搜索标题并按 Changes 或 Needs attention 筛选。
3. 切换 All workspaces，展示另一个 repository 的 Session 可以检索但按钮不可打开。
4. 返回当前 workspace 的 Session，展示消息、工具活动、Diff 和 validation 事实恢复。

## G. 真实仓库主视频：3～5 分钟

| 时间 | 画面 | 必须保留的信息 |
| --- | --- | --- |
| 0:00～0:30 | 打开 issue/`CODEAGENT_TASK.md`，展示失败测试，发送第一轮请求 | 仓库、基线 commit、任务 ID、模型、固定预算 |
| 0:30～1:20 | 第一轮搜索并定位实现和测试 | 至少两个与定位有关的代码证据，不声称展示思维链 |
| 1:20～2:30 | 第二轮根据定位结果实施、运行 focused test 和必要回归 | 不重复 issue；命令、退出状态、真实耗时和剪辑标记 |
| 2:30～3:40 | 用户检查 Diff，发送简短复核请求，再确认 current validation 和 Accept | 用户审查能改变后续动作，source 在 Accept 后更新 |
| 3:40～5:00 | 独立 oracle 和评测报告中的该题 | resolved、步骤、工具调用、token、成本、终止状态 |

正式案例从冻结的 50 题批次中选择：oracle 通过、Medium 优先、48 步内完成、定位至少涉及两个文件或模块。评测前
如使用 Click/HTTPX 临时成片，标题和字幕写 `real-repository dogfood`，正式评测完成后再决定是否替换。

## 录制前最终检查

- [ ] wheel/VSIX 来自同一干净 commit，视频中能给出该 commit。
- [ ] API Key 只存在于 SecretStorage，Output、终端、History、Diff 均无密钥。
- [ ] 准备新的 workspace 和 Session；失败基线、目标测试和 oracle 都已彩排。
- [ ] VS Code 右侧栏宽度足以显示按钮和 validation，左侧 Explorer 与中央 Diff 不被遮挡。
- [ ] 关闭桌面通知、Git 凭据提示、自动更新提示和无关扩展徽标。
- [ ] 记录原始录屏、剪辑版、字幕稿、任务 patch 和运行指标的对应关系。
- [ ] 快速体验、控制短片和主视频各自只讲它能够证明的结论。
