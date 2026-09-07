# 下一阶段计划

## 当前结论

Runtime、VS Code Product Phase 1～5、Command Environment v1 和两条 environment/network dogfood 分支均已
完成开发工作区实现与相应验证，不等于发布交付物已经齐备。当前目标是将这些能力形成可看、可安装、可检查的
GitHub 项目展示，范围以[系统目标](系统目标.md)为准。展示素材、简历措辞和评测缺口见
[展示导向现状梳理](SHOWCASE_READINESS.md)。本文件只记录尚未完成的工作；已落地能力见
[当前架构](ARCHITECTURE.md)，验收方法见[产品验收](PRODUCT_ACCEPTANCE.md)。

## 当前阶段：v0.5 Release Candidate 收口

从现在起停止增加非阻塞功能。只有干净安装、核心验收或展示彩排暴露的问题进入 v0.5；Planner、全文历史搜索、
更多 provider、复杂会话管理和环境自动准备继续留在后续版本。达到以下四个 Gate 后发布 `v0.5.0-rc.1`，获得
真实安装反馈后再决定 `v0.5.0`。

### Gate A：冻结可审查版本

- 整理当前大批未提交与未跟踪文件，按 Runtime/Benchmark、Product/Extension、文档与展示材料形成可审查提交；
- 收口公开文档：从发布 HEAD 移除 `docs/archive/`、阶段计划、展示准备清单和已经完成使命的 TD；将仍有效的行为、
  边界与关键取舍合并到 README、Architecture、Installation、Security、Evaluation 和 Testing。旧材料由 Git
  历史与开发分支保留，不为整洁目的重写历史；
- 统一 Python package、VS Code Extension、文档和目标 tag 的 `0.5.0` 版本口径；
- 在候选 commit 上运行全量 pytest、Extension test、语法检查和 `git diff --check`；
- 确认仓库中不包含 API Key、本机绝对结果路径、付费日志或不应发布的 benchmark cache。

退出条件：工作树干净，候选 commit 和验证结果可被准确引用。当前仍位于 `feature/v0.4-candidate-loop`，且 Product、
Extension 与大量测试仍未纳入提交，因此这是第一优先级。

### Gate B：形成可安装产物

- 确认仓库许可，加入与 Extension manifest 声明一致的 LICENSE；
- 构建 Python wheel 与 VSIX，记录文件名、SHA-256、Runtime/Extension 兼容版本；
- 在没有项目 `.venv` 兜底的干净 Linux/WSL2 环境安装两项产物，完成 Runtime 启动、API Key SecretStorage、
  新建 Session 和 demo 任务；
- 增加 GitHub Actions 的基础 Python/Node 检查；Bubblewrap、Docker 与真实 provider 验证继续作为单列环境证据。

退出条件：陌生用户可以只根据 README 安装固定产物并完成离线 demo，不依赖作者工作区路径。

### Gate C：冻结产品证据

- 在同一个候选 commit 上完成 [Product Acceptance](PRODUCT_ACCEPTANCE.md)：主链路、Stop、budget waiting/扩额、
  Approval Reject、Discard、History 重载和 stale validation；
- 录制 60～90 秒确定性快速体验，截取右侧 Agent、左侧 Explorer、中央 Diff 同时可见的真实界面；
- 录制 Stop/budget 与 Reject/Discard 两个短片；正式真实仓库主视频可在固定评测产生合适案例后补充；
- README 首屏加入截图、快速体验链接、架构图、安装命令、当前限制和验证记录。

退出条件：访问者能够在几分钟内理解用途、安全边界和实际完成度，并能回到 commit、命令与证据。

### Gate D：发布候选版本

- 创建 `v0.5.0-rc.1` tag 和 GitHub Pre-release，附 wheel、VSIX、checksum、安装说明、已知限制与验收记录；
- 使用 release 页面中的产物重新安装一次，不使用本地构建目录；
- 只修复安装、数据安全、核心链路和严重 UX 问题；修复后产生新的 RC，不覆盖已发布产物。

退出条件：RC 可以被另一台受支持环境复现。RC 阶段不要求先取得正式 50 题成绩。

### Gate E：评测与正式展示

- 完成下节评测准备后冻结独立评测 commit，运行固定 SWE-bench Verified 50 题自定义子集；
- 发布逐题结果、失败分类、成本/时延、配置与公开证据映射；从冻结批次按既定规则选择真实仓库主视频案例；
- 将实际 X/50、Y/50 回填 README 和简历，再发布 `v0.5.0` 或标明评测对应的准确 RC/commit。

退出条件：任何汇报指标都能定位到固定任务、运行身份和原始判定，不以成功案例替代总体结果。

## 正式 50 题评测前的最小工作

- 运行/恢复 identity：核对 Runtime commit/dirty、selection、grader、provider/config、依赖和价格快照；
  当前 batch resume 尚未校验 Runtime commit，不能仅依赖现有 config fingerprint；
- attempt 账本与重试规则：保留首次模型尝试、异常和 recovery attempt，禁止失败题择优重跑；当前 batch summary
  不包含未纳入最终结果的失败 attempt 消耗，需补全量计量；
- 时延记录：分别记录准备、Agent、grader 的耗时，不能用含停机时间的 batch 总跨度冒充 Agent 延迟；
- 可公开结果包：逐题关联 prediction/patch hash、official report、trajectory 和 usage，导出 CSV/JSON 与可读报告；
- 汇报分母固定为 50，分别报告 resolved、Agent 正常结束、当前 validation、完整链路交集与失败类型；
  未判定和计量缺失明确标记，不删除任务或补零。

具体代码缺口、运行边界和指标定义见[展示导向现状梳理第 5～6 节](SHOWCASE_READINESS.md#5-50-题评测是否值得做)。
以上可通过小步实现或可审计的外部运行检查完成，不要求 Runtime 重构。

## 后续独立设计项

以下不是当前展示前置条件；只有安装或演示暴露实际阻塞时才提升优先级。

- 项目环境准备：区分 Candidate 临时环境、可复现配置文件和经用户批准应用到 source 的 setup 操作；
- 环境生命周期：ignored 大目录的位置、identity、复用、配额、清理与失效；
- 产品可见性：以低干扰方式展示 active workspace、backend、network scope 和 command environment；
- 权限细化：评估 domain/port 网络控制与长命令的更强取消/资源配额；
- 分发体验：Runtime 与 VSIX 的版本兼容、升级和诊断，不依赖开发仓库布局。

以上项目不得通过语言/框架枚举、命令语义解析、自动安装或绕过 Approval 实现。若进入开发，应先形成独立
契约和验收标准。

## 明确暂缓

- 原系统目标中的 Research Artifact、共享项目理解、Design Preview、设计审批一致性和 Semantic Diff；
- Multi-Agent、Long-term Memory、MCP、复杂 Planner；
- RepoMap/RAG、大规模 Context redesign、隐式 Working Set；
- proposed Agent Sessions API 深度绑定或复杂服务拆分；
- 为提高 SWE-bench 分数进行 prompt tuning 或无版本边界的反复付费评测。
