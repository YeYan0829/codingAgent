"use strict";

const {mountChat} = require("./webview");
const {createTranscriptViewState} = require("./viewState");
const {createMarkdownRenderer, renderAgentMessageHtml} = require("./markdown");
const {uiIcon, renderNavigation, renderWelcome, renderSettings, taskStatus} = require("./ui");

const {renderRepositoryAccess} = require("./repositoryAccess");
const vscode = require("vscode");
const path = require("path");
const fs = require("fs");
const crypto = require("crypto");
const { RpcClient } = require("./rpcClient");
const markdownSource = fs.readFileSync(path.join(__dirname, "vendor/markdown-it-15.0.3.min.js"), "utf8").replace(/<\/script/gi, "<\\/script");

function canonicalWorkspace(value) {
  if (typeof value !== "string" || !value.trim()) return "";
  let resolved = path.resolve(value);
  try { resolved = fs.realpathSync.native(resolved); } catch (_) { /* Missing workspaces remain searchable by saved path. */ }
  return process.platform === "win32" ? resolved.toLowerCase() : resolved;
}

function sameWorkspace(left, right) {
  const a = canonicalWorkspace(left);
  return Boolean(a) && a === canonicalWorkspace(right);
}

function validationLabel(validation, historical = false) {
  if (!validation) return "尚未验证";
  if (validation.deliveryStatus === "not_run_on_merged_tree") return "合并后的代码尚未验证";
  if (validation.deliveryStatus === "passed") return "交付版本验证通过";
  if (validation.deliveryStatus === "unknown") return "交付版本暂无验证记录";
  if (validation.status === "failed") return "验证未通过";
  if (historical || validation.historical) return "当时的验证通过";
  return validation.appliesToCurrentChanges ? "验证通过" : "修改后尚未重新验证";
}

function renderToolFailureHtml(tool, key, esc) {
  const failure = tool.failure;
  if (!failure) return tool.result ? '<div class="muted small">' + esc(tool.result) + '</div>' : "";
  const output = failure.output
    ? '<details data-disclosure="' + esc(key) + ':output"><summary>查看输出</summary>'
      + '<pre class="tool-output">' + esc(failure.output) + '</pre>'
      + (failure.outputTruncated ? '<small class="output-truncated">过长输出已截断。</small>' : "")
      + '</details>'
    : "";
  return '<div class="tool-failure ' + esc(failure.kind) + '"><strong>' + esc(failure.title) + '</strong>'
    + (failure.summary ? '<span>' + esc(failure.summary) + '</span>' : "") + output + '</div>';
}

function renderContextSummaryHtml() {
  return '<div class="runtime-note">↳ Earlier activity was summarized to keep this task running.</div>';
}

function renderTruncationNoticeHtml(item) {
  const message = item.tone === "warning"
    ? "Tried to continue automatically after a model response was cut short."
    : "Continued automatically after a model response was cut short.";
  return '<div class="runtime-note">↳ ' + message + '</div>';
}

function toolActivityLabel(tool) {
  const argument = tool.argumentSummary || "";
  const labels = {read_file: "读取", list_dir: "浏览目录", find_files: "查找文件", search_text: "搜索",
    apply_workspace_edit: "编辑文件", edit_file: "编辑文件", task: "委派子任务", run_command: "运行命令", spawn_agent: "分派只读子任务",
    send_agent: "继续子任务", wait_agents: "等待子任务结果", cancel_agents: "停止子任务",
    resolve_agent_failures: "处理子任务失败"};
  const label = labels[tool.tool] || tool.summary || tool.tool || "执行操作";
  return ["read_file", "run_command", "search_text"].includes(tool.tool) && argument ? label + " · " + argument : label;
}

function activitySteps(tools) {
  const inspection = new Set(['read_file', 'list_dir', 'find_files', 'search_text', 'git_status', 'git_diff', 'git_log', 'git_show']);
  const controls = new Set(['cancel_agents', 'resolve_agent_failures']);
  const steps = [];
  for (const tool of tools || []) {
    if (controls.has(tool.tool)) continue;
    const kind = inspection.has(tool.tool) ? 'inspection' : tool.tool === 'apply_workspace_edit' ? 'edit'
      : tool.tool === 'run_command' ? 'command' : tool.tool === 'wait_agents' ? 'wait' : ['task', 'spawn_agent', 'send_agent', 'delegate_readonly'].includes(tool.tool) ? 'task' : 'tool';
    const previous = steps.at(-1);
    const sameWait = kind === 'wait' && previous?.kind === kind
      && JSON.stringify([...(previous.tools[0].waitRunIds || [])].sort()) === JSON.stringify([...(tool.waitRunIds || [])].sort())
      && previous.tools[0].waitMode === tool.waitMode;
    if ((kind === 'inspection' && previous?.kind === kind) || sameWait) previous.tools.push(tool);
    else steps.push({kind, tools: [tool]});
  }
  return steps;
}

function renderActivityHtml(item, key, esc, extra = '', task = null, executionState = 'idle', approval = null, markdown = esc) {
  const steps = activitySteps(item.tools);
  const rows = steps.map(step => {
    const tool = step.kind === 'wait' ? step.tools.at(-1) : step.tools[0];
    const linkedRun = task?.runs?.find(run => run.runId === tool.runId);
    if (step.kind === 'task' && linkedRun) return renderChildRunHtml(linkedRun, task, esc, markdown, executionState);
    const statuses = new Set(step.tools.map(t => t.status));
    const live = executionState === 'running';
    let state = statuses.has('failed') ? '失败' : statuses.has('denied') ? '已拒绝'
      : statuses.has('interrupted') ? '已中断' : statuses.has('cancelled') ? '已取消'
      : statuses.has('running') ? live ? '执行中' : executionState === 'awaiting_approval' ? '等待批准' : '未确认结束'
      : statuses.has('pending') ? live ? '等待执行' : '未执行' : statuses.has('submitted') ? '已受理' : '完成';
    let label;
    if (step.kind === 'inspection') {
      const targets = [...new Set(step.tools.map(t => t.argumentSummary || t.summary || t.tool))];
      label = (step.tools.every(t => t.tool === 'read_file') ? '读取 ' : '查阅 ') + targets.slice(0, 3).join('、')
        + (targets.length > 3 ? ' 等 ' + targets.length + ' 项' : '');
    } else if (step.kind === 'edit') {
      label = '修改 ' + (tool.changedFiles?.join('、') || tool.argumentSummary || '文件');
    } else if (step.kind === 'command') {
      label = tool.command || tool.argumentSummary || '运行命令';
      if (tool.status === 'succeeded' && tool.exitCode === 0) state = '退出码 0';
      if (approval?.kind === 'command_permissions' && approval.summary?.command === label) state = '等待批准';
    } else if (step.kind === 'wait') {
      const names = [...new Set((tool.waitRunIds || []).map(id => {
        const run = task?.runs?.find(value => value.runId === id);
        return run ? runName(run, task) : '子任务';
      }))];
      label = '等待' + (names.join('、') || '调查') + '的结果' + (tool.waitMode === 'any' ? '（任一）' : '');
      if (['pending', 'running'].includes(tool.status)) state = executionState === 'stopping' ? '正在停止' : live ? '等待中' : '已中断';
      if (tool.status === 'submitted') state = tool.waitSatisfied === false ? '等待超时' : '等待结束';
    } else if (step.kind === 'task') {
      const run = task?.runs?.find(r => r.runId === tool.runId);
      label = (tool.tool === 'send_agent' ? '继续调查 · ' : '委派调查 · ') + (run?.goal || tool.argumentSummary || '项目调查');
      if (run) state = run.cancelRequested && !run.terminalAck ? '正在停止'
        : ({queued: '排队中', running: '调查中', completed: run.terminalAck ? '调查结束' : '收尾中',
          failed: '失败', interrupted: '已中断', cancelled: '已取消', timed_out: '已超时', budget_exhausted: '预算已用完'}[run.status] || run.status);
    } else label = tool.summary || toolActivityLabel(tool);
    const issue = step.tools.filter(t => ['failed', 'denied'].includes(t.status)).map(t => renderToolFailureHtml(t, key + ':' + t.callId, esc)).join('');
    const output = step.tools.map(t => t.output ? '<pre class="tool-output">' + esc(t.output) + '</pre>'
      + (t.outputTruncated ? '<small>输出已截断。</small>' : '') : '').join('');
    const records = step.tools.map(t => '<div class="tool-record"><code>' + esc(t.tool) + '</code>'
      + (t.argumentSummary ? '<p>' + esc(t.argumentSummary) + '</p>' : '')
      + '<small>' + esc(t.status) + (t.status === 'submitted' ? ' · 仅为调度回执' : '') + '</small>'
      + (t.result ? '<p>' + esc(t.result) + '</p>' : '') + '</div>').join('');
    const icon = uiIcon('chevron');
    return '<div class="work-step-block"><details class="work-step" data-disclosure="' + esc(key + ':step:' + step.tools[0].callId) + '" data-scroll-anchor="' + esc(key + ':step:' + step.tools[0].callId) + '"><summary>'
      + '<span class="work-icon" aria-hidden="true">' + icon + '</span><span class="work-target">' + esc(label)
      + '</span><span class="work-state">' + esc(state) + '</span></summary>'
      + output + records + '</details>' + issue + '</div>';
  }).join('');
  // 真正的失败也可发生在内部等待／取消操作，不能因为不展示调度行而隐藏。
  const hiddenFailures = (item.tools || []).filter(t => ['cancel_agents', 'resolve_agent_failures'].includes(t.tool)
    && ['failed', 'denied'].includes(t.status)).map(t => renderToolFailureHtml(t, key + ':' + t.callId, esc)).join('');
  return '<div class="activity-block">' + rows + hiddenFailures + (extra
    ? '<details class="activity-record" data-disclosure="' + esc(key) + '"><summary>执行详情</summary>' + extra + '</details>' : '') + '</div>';
}

function renderTaskAlertsHtml(task, esc, showCleanup = true) {
  if (!task?.runs?.length) return '';
  // 只突出执行失败或阻塞。正常报告的范围说明和“未独立验证”属于诊断，不是用户故障。
  const failed = task.runs.filter(run => !['queued', 'running', 'completed'].includes(run.status)
    && !(['stopped', 'stopping'].includes(task.status) && ['cancelled', 'interrupted'].includes(run.status)));
  const failedAgents = new Set(failed.map(run => run.agentId));
  const pending = task.runs.some(run => !run.terminalAck);
  const cleanup = pending && ['stopping', 'stopped', 'closing', 'failed', 'interrupted', 'completed', 'completed_with_warnings'].includes(task.status);
  let html = '';
  if (failed.length) {
    const unhandled = task.unhandledRuns?.length;
    const accepted = failed.filter(run => run.failureDecision).at(-1)?.failureDecision.reason;
    html += '<p class="task-alert">' + failedAgents.size + ' 个子任务未完成'
      + (unhandled ? '，调查结果尚不完整。' : accepted ? '，已记录此限制。' : '。')
      + '<span>' + esc((failed.at(-1).unresolved || []).join(' · ').slice(0, 240) || '可展开工作记录查看原因。') + '</span>'
      + (accepted ? '<span>' + esc(accepted) + '</span>' : '') + '</p>';
  }
  if (cleanup && showCleanup) html += '<p class="task-alert">仍在清理执行资源…</p>';
  return html;
}

function feeDetails(usage, esc) {
  return (usage?.fees || []).map(fee => '<p class="muted small">' + esc(fee.currency)
    + ' · 已结算 ' + esc(fee.settledMicrounits) + ' · 预留 ' + esc(fee.reservedMicrounits)
    + (fee.unpricedRequests ? ' · 未计价 ' + esc(fee.unpricedRequests) + ' 次请求' : '')
    + ' · 未知 ' + esc(fee.unknownMicrounits) + '（' + esc(fee.unknownRequests) + ' 次请求）；单位为百万分之一币种。</p>').join('');
}

function renderSessionDetailsHtml(detail, esc) {
  const usage = detail?.orchestration?.totalUsage || {};
  return '<section class="session-details"><dl><dt>工作区</dt><dd>' + esc(detail.workspace)
    + '</dd><dt>模型</dt><dd>' + esc(detail.provider) + ' / ' + esc(detail.model)
    + '</dd><dt>模型推理</dt><dd>' + esc(detail.reasoningEnabled ? detail.reasoningEffort : '未启用')
    + '</dd><dt>会话编号</dt><dd>' + esc(detail.sessionId) + '</dd></dl>'
    + '<h3>总用量</h3><p>' + esc(usage.requests || 0) + ' 次请求 · 已知 ' + esc(usage.knownTokens || 0) + ' tokens · '
    + esc(usage.unknownUsageRequests || 0) + ' 次用量未知</p>' + feeDetails(usage, esc)
    + '<p class="muted small">主任务、子任务及上下文摘要请求合计。</p>'
    + renderSessionDiagnosticsHtml(detail, esc) + '</section>';
}


function renderTurnDiagnosticsHtml(value, esc) {
  const records = (value.items || []).filter(item => item.type === 'contextSummary' || item.kind === 'truncationRecovery')
      .map(item => item.type === 'contextSummary' ? renderContextSummaryHtml(item) : renderTruncationNoticeHtml(item)).join('')
    + (value.items || []).filter(item => item.type === 'notice' && item.kind !== 'truncationRecovery'
      && (item.tone === 'info' || item.message === 'Approval requested'))
      .map(item => '<p class="muted small">' + esc(item.message) + '</p>').join('')
    + (value.items || []).filter(item => item.displayText !== undefined && item.displayText !== item.text)
      .map(item => '<details data-disclosure="original-' + esc(value.turnId) + '-' + esc(item.eventSeq) + '"><summary>原始回复</summary><pre>' + esc(item.text) + '</pre></details>').join('');
  return records ? '<details data-disclosure="diagnostics-' + esc(value.turnId)
    + '"><summary>会话上下文记录</summary>' + records + '</details>' : '';
}

function renderSessionDiagnosticsHtml(detail, esc) {
  const tasks = detail?.orchestration?.tasks || detail?.turns?.map(turn => turn.delegation).filter(Boolean) || [];
  const records = tasks.flatMap(task => (task.runs || []).map(run => {
    const messages = (run.items || []).filter(item => item.type === 'markdown');
    const outputs = messages.map(item => item.text);
    if (run.findings && !messages.some(item => item.final)) outputs.push(run.findings);
    return '<details data-disclosure="diagnostic-run-' + esc(run.runId) + '"><summary>'
      + esc(runName(run, task)) + '：' + esc(run.goal) + ' · ' + esc(runState(run)) + '</summary>'
      + renderRunDiagnosticsHtml(run, task, esc)
      + (outputs.length ? '<details data-disclosure="raw-output-' + esc(run.runId) + '"><summary>子任务原始输出</summary>'
        + outputs.map(text => '<pre class="tool-output">' + esc(text) + '</pre>').join('') + '</details>' : '') + '</details>';
  })).join('');
  const context = (detail?.turns || []).map(turn => renderTurnDiagnosticsHtml(turn, esc)).join('');
  return records || context ? '<details class="session-diagnostics" data-disclosure="session-diagnostics"><summary>排查记录</summary>'
    + records + context + '</details>' : '';
}

function renderLiveProgressHtml(detail, esc) {
  const state = detail?.executionState;
  if (!state || ['idle','awaiting_approval','unavailable'].includes(state)) return '';
  if (state === 'stopping') return '<p class="running-indicator" role="status">正在停止…</p>';
  if (state === 'external_running') return '<p class="running-indicator" role="status">正在等待上一次执行结束…</p>';
  const latest = detail.turns?.at(-1);
  if (latest?.delegation?.status === 'closing') return '<p class="running-indicator" role="status">正在清理执行资源…</p>';
  const tools = (latest?.items || []).flatMap(item => item.type === 'activityGroup' ? item.tools || [] : []);
  if (tools.some(tool => ['running', 'pending'].includes(tool.status) && !['cancel_agents', 'resolve_agent_failures'].includes(tool.tool))) return '';
  if (latest?.delegation?.runs?.some(run => run.execution === 'foreground' && ['running', 'queued'].includes(run.status))) return '';
  return '<p class="running-indicator" role="status">'+esc(detail.activity?.label||'主任务处理中…')+'</p>';
}

function runName(run) {
  return run.description || run.goal?.split('\n')[0]?.slice(0,100) || '子任务';
}

function runState(run) {
  if (run.cancelRequested && !run.terminalAck) return '正在停止';
  if (run.awaitingApproval) return '等待你的批准';
  const label = {queued:'正在启动',running:'执行中',completed:'已返回',failed:'未完成',interrupted:'已中断',cancelled:'已取消',timed_out:'已超时'}[run.status] || run.status;
  return !run.terminalAck && !['queued','running'].includes(run.status) ? label + ' · 正在回收' : label;
}

function renderRunDiagnosticsHtml(run, task, esc) {
  return '<details class="run-diagnostics"><summary>执行信息与用量</summary><p>'
    + (run.terminalAck?'资源已回收':'等待资源回收')+'</p><dl><dt>子会话</dt><dd>'+esc(run.agentId)
    +'</dd><dt>本次执行</dt><dd>'+esc(run.runId)+'</dd></dl>'
    +(run.usage?'<p>'+esc(run.usage.requests)+' 次请求 · 已知 '+esc(run.usage.knownTokens)+' tokens</p>'+feeDetails(run.usage,esc):'')
    +'<p>共享当前项目；结果记录本次观察，不认证当前文件。</p></details>';
}

function renderChildRunHtml(run, task, esc, markdown=esc, executionState) {
  const recordedActive=['queued','running'].includes(run.status);
  const uncertain=recordedActive&&['idle','unavailable','external_running'].includes(executionState);
  const active=recordedActive&&!uncertain;
  const role=run.role||'子任务';
  const wait=active?'主 Agent 等待此子任务':!run.terminalAck?'主 Agent 等待资源回收':'';
  const activity=uncertain?'状态待确认':run.awaitingApproval?'等待你的批准':active?(run.currentActivity?toolActivityLabel(run.currentActivity):run.activity?.label||'正在处理'):'';
  const key='child-'+(run.rootTurnId||task?.rootTurnId||'')+':'+run.runId;
  const items=(run.items||[]).map(item=>item.type==='activityGroup'?renderActivityHtml(item,key+':'+item.eventSeq,esc,'',null,active?'running':'idle',null,markdown)
    :item.type==='markdown'?'<div class="child-message agent-markdown">'+markdown(item.text||'')+'</div>':item.type==='notice'?'<p class="notice">'+esc(item.message)+'</p>':'').join('');
  return '<div class="child-task"><details class="child-run" id="child-run-'+esc(run.runId)+'" data-disclosure="'+esc(key)+'" data-scroll-anchor="'+esc(key)+'" data-run-status="'+esc(run.status)+'">'
    +'<summary><span class="child-chevron" aria-hidden="true">'+uiIcon('chevron')+'</span><span class="child-heading"><span class="child-title">'+esc(runName(run,task))
    +(run.action==='send'?' · 继续第 '+esc(run.runOrdinal||2)+' 次':'')+'</span><span class="child-activity">'+esc(role)+(role==='code-explorer'?' · 只读':'')
    +' · '+esc(activity||runState(run))+'</span>'+(wait?'<span class="child-activity">'+esc(wait)+'</span>':'')+'</span><span class="work-state">'+esc(uncertain?'状态待确认':runState(run))
    +(run.startedAt?'<span class="elapsed" data-start="'+esc(run.startedAt)+'" data-end="'+esc(run.completedAt||(!active?new Date().toISOString():''))+'"></span>':'')
    +'</span></summary><div class="child-transcript">'+(items||'<p class="muted small">尚未返回公开过程。</p>')
    +(run.findings&&!run.items?.some(i=>i.type==='markdown'&&i.final)?'<div class="agent-markdown">'+markdown(run.findings)+'</div>':'')
    +(run.error?'<p class="run-problem">'+esc(run.error)+'</p>':'')
    +'<button class="link-action" data-agent="'+esc(run.runId)+'">查看子任务详情</button></div></details></div>';
}

function renderTaskNoticeHtml(item, task, esc) {
  const run = task?.runs?.find(value => value.runId === item.runId);
  const label = (run ? runName(run, task) : '调查') + (item.status === 'completed' ? '已返回结果' : '未完成');
  return '<p class="task-arrival" data-scroll-anchor="arrival-' + esc(item.runId) + '">' + esc(label)
    + (run ? ' <button type="button" class="link-action" data-open-run="' + esc(run.runId) + '">查看过程</button>' : '') + '</p>';
}

function renderDelegationHtml(task, agents, esc) {
  return (task?.runs || []).map(run => renderChildRunHtml(run, task, esc)).join('');
}

function renderChangesHtml(value, detail, esc) {
  const feedback = value?.state === 'current_project' && value.lastUndo?.fileCount > 0
    ? '<p class="edit-feedback" role="status" title="已撤销该轮受控文件编辑；命令产生的其他修改保留。">已撤销 '+esc(value.lastUndo.fileCount)+' 个文件</p>' : '';
  if(!value?.available)return feedback;
  if(value.state!=='current_project')return '<details class="current-changes"><summary>历史交付记录 · '+esc(value.fileCount||value.files?.length||0)+' 个文件</summary><p class="muted">此记录不代表当前项目状态。</p>'+(value.files||[]).map(path=>'<button class="change-file" data-change-file="'+esc(path)+'" data-historical="true">'+esc(path)+'</button>').join('')+'</details>';
  const actions=detail?.availableActions||{};
  const reason=actions.canUndo?'撤销本轮受控编辑：'+(value.undoFiles||[]).join('、'):detail?.actionReasons?.canUndo||'等待任务结束后可撤销';
  return feedback+'<section class="current-changes" aria-label="修改审查"><details class="changed-files" data-disclosure="review-files"><summary>'
    +uiIcon('chevron')+uiIcon('diff')+'<span>已修改 '+esc(value.fileCount)+' 个文件</span></summary>'
    +'<div class="file-list">'+value.files.map(path=>'<button class="change-file" data-change-file="'+esc(path)+'" title="查看差异：'+esc(path)+'" aria-label="查看差异：'+esc(path)+'">'+uiIcon('diff')+'<span class="file-path">'+esc(path)+'</span><span class="file-diff-label">Diff</span>'+uiIcon('external')+'</button>').join('')+'</div>'
    +'<p class="changes-caption" title="差异从受控编辑起点比较当前文件，可能包含后续用户或命令修改。">已写入当前项目</p></details>'
    +'<div class="change-actions"><button id="undo-edits" class="review-action" aria-label="撤销本轮受控编辑" title="'+esc(reason)+'" '+(actions.canUndo?'':'disabled')+'>'+uiIcon('undo')+'<span>撤销本轮</span></button>'
    +'<button id="open-git" class="icon" aria-label="查看项目 Git 差异" title="查看项目 Git 差异（包含用户及外部修改）">'+uiIcon('branch')+'</button></div>'
    +(!actions.canUndo?'<p class="action-reason">'+esc(reason)+'</p>':'')+'</section>';
}


function renderTurnHtml(value, detail, esc, markdown, copyButton, runtimeUnavailable = false, processMarkdown = markdown) {
  const isLatest = detail?.turns?.at(-1)?.turnId === value.turnId;
  const executionState = runtimeUnavailable ? 'unavailable' : isLatest ? detail?.executionState : 'idle';
  const task = value.delegation;
  const ended = !isLatest || !['running', 'stopping', 'external_running', 'awaiting_approval'].includes(executionState);
  const replies = (value.items || []).filter(item => item.type === 'markdown');
  const answer = ended ? replies.findLast(item => item.final) || (replies.every(item => item.final === undefined) ? replies.at(-1) : null) : null;
  const activityItems = (value.items || []).filter(item => item.type === 'activityGroup' && item.tools?.length);
  const items = (value.items || []).map(item => {
    const key = value.turnId + ':' + item.eventSeq;
    if (item.type === 'markdown') return renderAgentMessageHtml(item.displayText ?? item.text, key, esc, item === answer ? markdown : processMarkdown, item === answer ? copyButton : () => '');
    if (item.type === 'activityGroup') return renderActivityHtml(item, key, esc, '', task, executionState, isLatest ? detail?.pendingApproval : null, processMarkdown);
    if (item.type === 'taskNotice') return renderTaskNoticeHtml(item, task, esc);
    if (item.type === 'contextSummary' || item.kind === 'truncationRecovery') return '';
    // 批准与预算已有专门操作入口；这些历史回执留在协议中，避免正文重复提示。
    if (item.message === 'Approval requested' || item.tone === 'info') return '';
    return '<div class="notice ' + esc(item.tone) + '">' + esc(item.message) + '</div>';
  }).join('');
  const linked = new Set(activityItems.flatMap(item => item.tools || []).map(tool => tool.runId).filter(Boolean));
  const unlinked = (value.delegation?.runs || []).filter(run => !linked.has(run.runId))
    .map(run => renderChildRunHtml(run, task, esc, processMarkdown, executionState)).join('');
  const files = value.changedFiles || [];
  const hasCurrentCard = isLatest && (detail?.workspaceEdits?.available || detail?.changesSummary?.available);
  const history = !hasCurrentCard && (files.length || value.validation)
    ? '<details class="turn-changes" data-disclosure="changes-' + esc(value.turnId) + '"><summary>修改记录 · ' + files.length + ' 个文件</summary>'
      + files.map(file => '<div class="file">' + esc(file) + '</div>').join('')
      + (value.validation ? '<p>' + validationLabel(value.validation, true) + ' · ' + esc(value.validation.command || '') + '</p>' : '') + '</details>' : '';
  return '<section class="turn"><div class="user-row user-message"><div class="message-shell"><div class="user-bubble">'
    + esc(value.userMessage) + '</div>' + copyButton(value.userMessage, '复制任务描述') + '</div></div>'
    + renderTaskAlertsHtml(value.delegation, esc, !(isLatest && ['running', 'stopping', 'external_running'].includes(detail?.executionState))) + items + unlinked + history
    + (value.attention || []).map(item => '<div class="notice">' + esc(item.message) + '</div>').join('') + '</section>';
}

const DEFAULT_PROFILE = Object.freeze({
  provider: "deepseek", model: "deepseek-v4-flash", temperature: 0.2,
  reasoningEnabled: false, reasoningEffort: "high",
  maxTokens: null, delegationEnabled: false, costPerTokenMicrounits: null, feeCurrency: "USD"
});

class ChatViewProvider {
  constructor(runtime, workspacePath, context, changeDocuments) {
    this.runtime = runtime;
    this.workspacePath = workspacePath;
    this.context = context;
    this.changeDocuments = changeDocuments;
    const storedProfile = context.globalState.get("productProfile") || {};
    let profileError;
    try {
      this.profile = validateProfile({...storedProfile, reasoningEffort: storedProfile.reasoningEffort || "high",
        delegationEnabled: storedProfile.delegationEnabled ?? storedProfile.orchestrationBackend === "langgraph"});
    } catch (error) {
      // 保留旧配置供用户修正；新建会话仍须通过validateProfile，不静默改用其他模型。
      this.profile = Object.fromEntries(Object.keys(DEFAULT_PROFILE).map(key => [key, storedProfile[key] ?? DEFAULT_PROFILE[key]]));
      profileError = "已保存的模型配置需要重新检查：" + error.message;
    }
    if (!storedProfile.reasoningEffort) this.profile.reasoningEffort = "high";
    if (this.profile.temperature === null) this.profile.temperature = DEFAULT_PROFILE.temperature;
    this.secretState = {deepseek: false, glm: false, fake: true};
    this.view = undefined;
    this.sessions = [];
    this.current = undefined;
    this.refreshTimer = undefined;
    this.rendered = false;
    this.runtimeError = undefined;
    this.operationError = profileError;
    this.refreshGeneration = 0;
  }

  async resolveWebviewView(view) {
    this.view = view;
    view.onDidDispose?.(() => {
      if (this.view !== view) return;
      this.view = undefined;
      clearTimeout(this.refreshTimer);
      clearTimeout(this.externalRefreshTimer);
      this.refreshTimer = undefined;
      ++this.refreshGeneration;
    });
    this.rendered = false;
    view.webview.options = { enableScripts: true };
    view.webview.onDidReceiveMessage(message => this._handleMessage(message));
    // 先交付带CSP的页面；连接/密钥失败属于产品状态，不能让宿主判定视图解析失败。
    this._render();
    try {
      await this._refreshSecretState();
      await this.refresh();
    } catch (error) {
      if (this.view === view) this.handleRuntimeError(error);
    }
  }

  handleRuntimeError(error) {
    this.runtimeError = error?.message || String(error);
    this.runtime.output?.appendLine?.("CodeAgent initialization/refresh failed: " + this.runtimeError);
    this._render();
  }

  async refresh(showResult = false) {
    const generation = this.refreshGeneration = (this.refreshGeneration || 0) + 1;
    await this.runtime.ensure();
    const client = this.runtime.client;
    const access = this.workspacePath ? await client.request("workspace/access/get", {workspace: this.workspacePath}) : undefined;
    const result = await client.request("session/list", {});
    const localSessions = result.sessions.filter(item => sameWorkspace(item.workspace, this.workspacePath));
    const sid = localSessions.find(item => item.sessionId === this.current?.sessionId)?.sessionId || localSessions[0]?.sessionId;
    const detail = sid ? await client.request("session/get", {sessionId: sid}) : undefined;
    if (detail?.changesSummary?.available) detail.changesSummary = await client.request("changes/get", {sessionId: sid});
    if (generation !== this.refreshGeneration || client !== this.runtime.client) return;
    this.repositoryAccess = access;
    this.sessions = result.sessions;
    this.current = detail;
    this.runtimeError = undefined;
    this.operationError = undefined;
    this._render();
    clearTimeout(this.externalRefreshTimer);
    if (this.view && detail?.executionState === "external_running") {
      // 重载后的旧 Runtime 仍在清理时，只轮询读取；不启动任务或恢复审批。
      this.externalRefreshTimer = setTimeout(() => this.handleNotification("session/event", {sessionId: detail.sessionId}), 500);
    }
    if (showResult) vscode.window.setStatusBarMessage(`CodeAgent: found ${this.sessions.length} session${this.sessions.length === 1 ? "" : "s"}`, 3000);
  }

  async openSession(sessionId) {
    const summary = this.sessions.find(item => item.sessionId === sessionId);
    if (!summary || !sameWorkspace(summary.workspace, this.workspacePath)) {
      throw new Error("Open this session's workspace before continuing it");
    }
    const generation = this.refreshGeneration = (this.refreshGeneration || 0) + 1;
    await this.runtime.ensure();
    const client = this.runtime.client;
    const detail = await client.request("session/get", {sessionId});
    if (detail.changesSummary?.available) detail.changesSummary = await client.request("changes/get", {sessionId});
    if (generation !== this.refreshGeneration || client !== this.runtime.client) return;
    this.current = detail;
    this._render();
  }

  async _handleMessage(message) {
    let completed = false;
    try {
      if (['run','continue','stop','resolveApproval','openDiff'].includes(message?.type) && message.sessionId !== undefined && message.sessionId !== this.current?.sessionId) {
        throw new Error("会话已切换，请在原会话重新操作");
      }
      if (message?.type === "selectSession" && typeof message.sessionId === "string") {
        await this.openSession(message.sessionId);
      } else if (message?.type === "refresh") {
        await this.refresh(true);
      } else if (message?.type === "newTask") {
        await this.newSession();
      } else if (message?.type === "retryRuntime") {
        this.runtime.restart();
        await this.refresh(true);
      } else if (message?.type === "openOutput") {
        this.runtime.output.show(true);
      } else if (message?.type === "copyText" && typeof message.text === "string") {
        await vscode.env.clipboard.writeText(message.text);
        this.view?.webview.postMessage({type: "copyResult", copyId: message.copyId, ok: true});
      } else if (message?.type === "saveRepositoryAccess") {
        await this.saveRepositoryAccess(message);
      } else if (message?.type === "saveSettings") {
        await this.saveSettings(message.profile, message.apiKey);
      } else if (message?.type === "clearApiKey" && ["deepseek", "glm"].includes(message.provider)) {
        if (this.current?.executionState && this.current.executionState !== "idle") {
          throw new Error("Stop the current task before changing API credentials");
        }
        await this.context.secrets.delete(`codeagent.${message.provider}.apiKey`);
        this.runtime.restart();
        await this._refreshSecretState();
        this._render();
      } else if (message?.type === "run" && typeof message.message === "string" && this.current) {
        await this.runtime.ensure();
        await this.runtime.client.request("session/run", {sessionId: message.sessionId || this.current.sessionId, message: message.message});
        await this.refresh();
      } else if (message?.type === "continue" && this.current) {
        await this.runtime.ensure();
        await this.runtime.client.request("session/continue", {sessionId: message.sessionId || this.current.sessionId});
        await this.refresh();

      } else if (message?.type === "stop" && this.current) {
        await this.runtime.client.request("session/stop", {sessionId: this.current.sessionId});
        await this.refresh();
      } else if (message?.type === "resolveApproval" && typeof message.allow === "boolean" && this.current?.pendingApproval) {
        if (message.approvalId !== this.current.pendingApproval.approvalId) throw new Error("批准请求已变化，请重新核对");
        await this.runtime.client.request("approval/resolve", {
          sessionId: this.current.sessionId,
          approvalId: message.approvalId,
          allow: message.allow
        });
        await this.refresh();
      } else if (message?.type === "openDiff" && typeof message.path === "string" && this.current) {
        await this.openDiff(message.path, message.historical === true);
      } else if (message?.type === "openGit") {
        await vscode.commands.executeCommand("workbench.view.scm");
      } else if (message?.type === "loadProcess" && this.current && message.sessionId === this.current.sessionId) {
        const page = await this.runtime.client.request("session/process", {sessionId:message.sessionId,runId:message.runId,afterSeq:message.afterSeq||0});
        this.view?.webview.postMessage({type:"processPage",sessionId:message.sessionId,...page});
      } else if (message?.type === "undoEdits" && this.current && message.sessionId === this.current.sessionId) {
        const {sessionId} = this.current;
        const edits = this.current.workspaceEdits;
        if (!this.current.availableActions?.canUndo || !edits || message.expectedEditSeq !== edits.lastEditSeq || message.turnId !== edits.turnId) throw new Error("撤销范围已变化，请刷新后重试");
        const choice = await vscode.window.showWarningMessage("撤销本轮受控编辑："+edits.undoFiles.join("、")+"？命令产生的其他修改保留；外部修改冲突时整次拒绝。",{modal:true},"撤销受控编辑");
        if (choice === "撤销受控编辑") {
          await this.runtime.client.request("edits/undo", {sessionId,turnId:message.turnId,expectedEditSeq:message.expectedEditSeq});
          await this.refresh();
        }
      }
      completed = true;
    } catch (error) {
      this.operationError = error.message;
      this._render();
      if (message?.type === "run") {
        this.view?.webview.postMessage({type: "submissionRejected", message: message.message});
      } else if (message?.type === "copyText") {
        this.view?.webview.postMessage({type: "copyResult", copyId: message.copyId, ok: false});
      }
      vscode.window.showErrorMessage(`CodeAgent：${error.message}`);
    } finally {
      this.view?.webview.postMessage({type: "operationComplete", action: message?.type, ok: completed,
        error: completed ? undefined : this.operationError});
    }
  }

  async openDiff(filePath, historical = false) {
    const sessionId = this.current.sessionId;
    await this.runtime.ensure();
    const change = await this.runtime.client.request(historical ? "changes/file" : "edits/file", {sessionId,path:filePath});
    const left = this.changeDocuments.put(sessionId,filePath,"before",change.before);
    const right = this.changeDocuments.put(sessionId,filePath,"after",change.after);
    await vscode.commands.executeCommand("vscode.diff",left,right,`${filePath} (${historical?'历史交付':'受控编辑起点 ↔ 当前文件'})`);
  }

  async newSession() {
    if (!this.workspacePath) throw new Error("Open a workspace before creating a Session");
    if (this.current?.executionState && this.current.executionState !== "idle") {
      throw new Error("Stop the current task before creating another session");
    }
    if (!this.secretState[this.profile.provider]) throw new Error("Configure the selected provider API key first");
    const profile = validateProfile(this.profile);
    ++this.refreshGeneration;
    await this.runtime.ensure();
    this.repositoryAccess = await this.runtime.client.request("workspace/access/get", {workspace: this.workspacePath});
    if (!this.repositoryAccess?.configured) {
      this._render();
      throw new Error("请先保存仓库文件访问设置，可以留空");
    }
    this.current = await this.runtime.client.request("session/create", {
      workspace: this.workspacePath, ...profile,
      reasoningEffort: profile.reasoningEnabled ? profile.reasoningEffort : null,
    });
    if (this.current.provider !== profile.provider || this.current.model !== profile.model) {
      throw new Error(`Session configuration mismatch: expected ${profile.provider}/${profile.model}, got ${this.current.provider}/${this.current.model}`);
    }
    await this.refresh();
  }

  async saveRepositoryAccess(message) {
    if (!this.workspacePath) throw new Error("请先打开仓库");
    if (typeof message.paths !== "string") throw new Error("禁止路径格式错误");
    await this.runtime.ensure();
    this.repositoryAccess = await this.runtime.client.request("workspace/access/set", {
      workspace: this.workspacePath,
      deniedPaths: message.paths.split(/\r?\n/).map(value => value.trim()).filter(Boolean),
      expectedRevision: message.expectedRevision,
    });
    await this.refresh();
    vscode.window.setStatusBarMessage("仓库访问设置已保存，请新建任务使用新规则", 3000);
  }

  async saveSettings(value, apiKey) {
    if (typeof apiKey === "string" && apiKey.trim() && this.current?.executionState && this.current.executionState !== "idle") {
      throw new Error("Stop the current task before changing API credentials");
    }
    this.profile = validateProfile(value);
    await this.context.globalState.update("productProfile", this.profile);
    if (typeof apiKey === "string" && apiKey.trim()) {
      await this.context.secrets.store(`codeagent.${this.profile.provider}.apiKey`, apiKey.trim());
      this.runtime.restart();
    }
    await this._refreshSecretState();
    this.operationError = undefined;
    this._render();
    vscode.window.setStatusBarMessage("CodeAgent: configuration saved for new sessions", 3000);
  }

  async _refreshSecretState() {
    this.secretState = {
      fake: true,
      deepseek: Boolean(await this.context.secrets.get("codeagent.deepseek.apiKey")),
      glm: Boolean(await this.context.secrets.get("codeagent.glm.apiKey"))
    };
  }

  handleNotification(method, params) {
    if (!method.startsWith("session/") && method !== "approval/requested") return;
    if (this.current && params.sessionId !== this.current.sessionId && method !== "session/executionStateChanged") return;
    this.liveDirty = true;
    if (this.refreshTimer || this.liveRefreshing) return;
    this.refreshTimer = setTimeout(async () => {
      this.refreshTimer = undefined;
      this.liveRefreshing = true;
      this.liveDirty = false;
      try { await this.refresh(); }
      catch (error) { vscode.window.showErrorMessage(`CodeAgent live update failed: ${error.message}`); }
      finally {
        this.liveRefreshing = false;
        if (this.liveDirty) this.handleNotification("session/event", {sessionId: this.current?.sessionId});
      }
    }, 80);
  }

  handleConnectionState(state, error) {
    if (state === "unavailable") ++this.refreshGeneration;
    this.runtimeError = state === "unavailable" ? (error?.message || "CodeAgent Runtime is unavailable") : undefined;
    this._render();
  }

  _render() {
    if (!this.view) return;
    const sessions = this.sessions.map(session => ({
      ...session, isCurrentWorkspace: sameWorkspace(session.workspace, this.workspacePath)
    }));
    const productState = {
      detail: this.current, sessions, profile: this.profile,
      secretState: this.secretState, runtimeError: this.runtimeError,
      operationError: this.operationError, workspacePath: this.workspacePath, repositoryAccess: this.repositoryAccess
    };
    const signature = JSON.stringify(productState);
    if (this.rendered && this.lastProductState === signature) return;
    this.lastProductState = signature;
    if (!this.rendered) {
      this.view.webview.html = renderChat(
        this.current, sessions, this.profile, this.secretState, this.runtimeError,
        this.operationError, this.workspacePath, this.repositoryAccess
      );
      this.rendered = true;
    } else {
      this.view.webview.postMessage({type: "productState", state: productState});
    }
  }
}

class RuntimeConnection {
  constructor(context, workspacePath, output) {
    this.context = context;
    this.workspacePath = workspacePath;
    this.output = output;
    this.client = undefined;
    this.signature = undefined;
    this.stopping = Promise.resolve();
  }

  async ensure() {
    await this.stopping;
    const desired = await this._configuration();
    if (!this.client || desired.signature !== this.signature) {
      await this.client?.dispose();
      this.client = new RpcClient(desired.command, desired.args, {
        cwd: desired.cwd,
        env: desired.env,
        onLog: line => this.output.append(line),
        onNotification: (method, params) => { if (this.client === client) this.onNotification?.(method, params); },
        onConnectionState: (state, error) => { if (this.client === client) this.onConnectionState?.(state, error); }
      });
      const client = this.client;
      this.signature = desired.signature;
    }
    await this.client.start();
    const initialization = this.client.initialization;
    if (!initialization?.capabilities?.repositoryAccess) {
      const version = initialization?.runtimeVersion || "未知版本";
      await this.client.dispose();
      this.client = undefined;
      this.signature = undefined;
      throw new Error(`Runtime ${version} 不支持当前插件所需的 workspace/access/get。当前命令：${desired.command}。请使用当前源码 Runtime，或在 F5 中选择 Run CodeAgent UI Demo。`);
    }
  }

  dispose() {
    this.stopping = this.client?.dispose() || this.stopping;
    return this.stopping;
  }

  restart() {
    const old = this.client;
    this.client = undefined;
    this.signature = undefined;
    this.stopping = old?.dispose() || this.stopping;
    return this.stopping;
  }

  async _configuration() {
    const config = vscode.workspace.getConfiguration("codeagent");
    const development = this.context.extensionMode !== undefined && this.context.extensionMode === vscode.ExtensionMode?.Development;
    // 只接受F5开发宿主显式传入的配置；不修改用户全局配置，也不改变正式安装行为。
    const configuredCommand = (development && process.env.CODEAGENT_DEV_RPC_COMMAND) || config.get("rpcCommand", "auto");
    const projectRoot = path.resolve(this.context.extensionPath, "..");
    const developmentPython = path.join(projectRoot, ".venv", "bin", "python");
    const useDevelopmentPython = configuredCommand === "auto" && fs.existsSync(developmentPython);
    const command = useDevelopmentPython ? developmentPython : (configuredCommand === "auto" ? "codeagent-rpc" : configuredCommand);
    const sessionRoot = ((development && process.env.CODEAGENT_DEV_SESSION_ROOT) || config.get("sessionRoot", "")).trim();
    const args = [
      ...(useDevelopmentPython ? ["-m", "codeagent.product.rpc"] : []),
      ...(sessionRoot ? ["--session-root", sessionRoot] : [])
    ];
    const deepseekKey = await this.context.secrets.get("codeagent.deepseek.apiKey");
    const glmKey = await this.context.secrets.get("codeagent.glm.apiKey");
    const env = {};
    if (deepseekKey) env.DEEPSEEK_API_KEY = deepseekKey;
    if (glmKey) env.GLM_API_KEY = glmKey;
    this.output.appendLine?.(`CodeAgent source: ${this.context.extensionPath}; workspace: ${this.workspacePath}; Runtime: ${command}; args: ${JSON.stringify(args)}`);
    return {
      command, args, env, cwd: useDevelopmentPython ? projectRoot : this.workspacePath,
      signature: JSON.stringify({ command, args, deepseekKey: Boolean(deepseekKey), glmKey: Boolean(glmKey) })
    };
  }
}

class ChangeDocumentProvider {
  constructor() { this.documents = new Map(); }
  provideTextDocumentContent(uri) { return this.documents.get(uri.toString()) ?? ""; }
  put(sessionId, filePath, side, content) {
    const uri = vscode.Uri.from({
      scheme: "codeagent-change", authority: sessionId,
      path: `/${side}/${filePath}`, query: String(Date.now())
    });
    this.documents.set(uri.toString(), content);
    return uri;
  }
}

async function activate(context) {
  const output = vscode.window.createOutputChannel("CodeAgent Runtime");
  const workspacePath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  const runtime = new RuntimeConnection(context, workspacePath, output);
  const changeDocuments = new ChangeDocumentProvider();
  const provider = new ChatViewProvider(runtime, workspacePath, context, changeDocuments);
  runtime.onNotification = (method, params) => provider.handleNotification(method, params);
  runtime.onConnectionState = (state, error) => provider.handleConnectionState(state, error);

  context.subscriptions.push(
    output,
    { dispose: () => runtime.dispose() },
    vscode.workspace.registerTextDocumentContentProvider("codeagent-change", changeDocuments),
    vscode.window.registerWebviewViewProvider("codeagent.chat", provider, {
      webviewOptions: { retainContextWhenHidden: true }
    }),
    vscode.commands.registerCommand("codeagent.refreshSessions", () => provider.refresh(true).catch(error => provider.handleRuntimeError(error))),
    vscode.commands.registerCommand("codeagent.newTask", () => {
      provider.newSession().catch(error => vscode.window.showErrorMessage(`CodeAgent：${error.message}`));
    }),
    vscode.workspace.onDidChangeConfiguration(event => {
      if (event.affectsConfiguration("codeagent.rpcCommand") || event.affectsConfiguration("codeagent.sessionRoot")) {
        provider.refresh(true).catch(error => provider.handleRuntimeError(error));
      }
    })
  );
}

function renderChat(detail,sessions,profile=DEFAULT_PROFILE,secretState={},runtimeError,operationError,workspacePath,repositoryAccess) {
  const nonce=crypto.randomBytes(16).toString("hex");
  const initial=safeJson({detail,sessions,profile,secretState,runtimeError,operationError,workspacePath,repositoryAccess});
  const helpers=[uiIcon,renderNavigation,renderWelcome,renderSettings,taskStatus,renderRepositoryAccess,createTranscriptViewState,renderToolFailureHtml,toolActivityLabel,activitySteps,renderActivityHtml,renderAgentMessageHtml,createMarkdownRenderer,renderContextSummaryHtml,renderTruncationNoticeHtml,feeDetails,runName,runState,renderRunDiagnosticsHtml,renderChildRunHtml,renderTaskNoticeHtml,renderDelegationHtml,renderTaskAlertsHtml,renderTurnDiagnosticsHtml,renderSessionDiagnosticsHtml,renderSessionDetailsHtml,renderLiveProgressHtml,renderTurnHtml,validationLabel,renderChangesHtml];
  const declarations=helpers.map(fn=>`const ${fn.name} = ${fn.toString()};`).join("\n");
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}'"><style>${styles()}${fs.readFileSync(path.join(__dirname,'ui.css'),'utf8')}${fs.readFileSync(path.join(__dirname,'repositoryAccess.css'),'utf8')}</style></head><body><div id="app">Loading CodeAgent…</div><script nonce="${nonce}">${markdownSource}\n${declarations}\n(${mountChat.toString()})(${initial},acquireVsCodeApi());</script></body></html>`;
}

function safeJson(value) {
  return JSON.stringify(value).replace(/</g, "\\u003c").replace(/>/g, "\\u003e").replace(/&/g, "\\u0026");
}

function validateProfile(value) {
  const p={...DEFAULT_PROFILE,...value};
  if(!["deepseek","glm","fake"].includes(p.provider))throw new Error("Unknown provider");
  if(typeof p.model!=="string"||!p.model.trim())throw new Error("Model is required");
  if(typeof p.reasoningEnabled!=="boolean")throw new Error("Reasoning enabled must be a boolean");
  const efforts=p.provider==='glm'&&p.model!=='glm-5.3'?['high','max']:['low','high','max'];
  if(!efforts.includes(p.reasoningEffort))throw new Error('Reasoning effort must be '+efforts.join(', '));
  if(p.reasoningEnabled&&!(p.provider==='glm'?['glm-5.2','glm-5.3']:['deepseek-v4-flash','deepseek-v4-pro']).includes(p.model.trim()))throw new Error('Reasoning is not supported for '+p.model);
  if(p.temperature!==null&&(!Number.isFinite(p.temperature)||p.temperature<0||p.temperature>2))throw new Error('Temperature must be between 0 and 2');
  if(p.maxTokens!==null&&(!Number.isInteger(p.maxTokens)||p.maxTokens<1||p.maxTokens>131072))throw new Error('Max output tokens must be between 1 and 131072');
  if(typeof p.delegationEnabled!=='boolean')throw new Error('delegationEnabled must be a boolean');
  if(p.costPerTokenMicrounits!==null&&(!Number.isSafeInteger(p.costPerTokenMicrounits)||p.costPerTokenMicrounits<1))throw new Error('Token price must be a positive integer');
  if(!/^[A-Z]{3}$/.test(p.feeCurrency))throw new Error('Currency must be a three-letter uppercase code');
  return Object.fromEntries(Object.keys(DEFAULT_PROFILE).map(key=>[key,key==='model'?p.model.trim():p[key]]));
}

function styles() {
  return `
.activity-block{margin:6px 0 12px}
.child-task{margin:9px 0}.child-run{border-left:2px solid var(--vscode-editorGroup-border);padding-left:10px}.child-run>summary{display:flex;gap:7px;align-items:baseline;list-style:none;padding:5px 0}.child-run>summary::-webkit-details-marker{display:none}
.child-chevron{flex-shrink:0;color:var(--vscode-descriptionForeground)}.child-run[open]>summary .child-chevron{transform:rotate(90deg)}.child-heading{display:flex;flex:1;min-width:0;flex-direction:column;gap:3px}.child-title{overflow-wrap:anywhere}.child-activity{font-size:.88em;color:var(--vscode-descriptionForeground);overflow-wrap:anywhere}.child-transcript{margin:6px 0 10px 12px}.child-message{margin:10px 0}.run-problem{color:var(--vscode-editorWarning-foreground);overflow-wrap:anywhere}.run-link{display:block;text-align:left;margin:4px 0}.task-arrival{font-size:.9em;color:var(--vscode-descriptionForeground)}.turn-diagnostics{margin:10px 0;color:var(--vscode-descriptionForeground)}

.work-step{margin:3px 0}.work-step>summary{display:flex;align-items:baseline;gap:8px;padding:5px 0;list-style:none;font-size:.92em}
.work-step>summary::-webkit-details-marker{display:none}.work-icon{width:12px;flex-shrink:0;color:var(--vscode-descriptionForeground)}
.work-target{min-width:0;overflow-wrap:anywhere}.work-state{margin-left:auto;flex-shrink:0;font-size:.9em;color:var(--vscode-descriptionForeground)}
.work-step[open]>summary .work-icon{transform:rotate(90deg)}.tool-record{padding:6px 18px}.tool-record p{margin:3px 0;overflow-wrap:anywhere}
.activity-record>summary{font-size:.92em;line-height:1.5;color:var(--vscode-descriptionForeground)}
.current-operation{margin:5px 0;color:var(--vscode-progressBar-background);overflow-wrap:anywhere}
.activity-issue{margin:7px 0;padding:7px 10px;border-left:2px solid var(--vscode-editorWarning-foreground)}
.activity-issue>strong{font-size:.92em}.tool-name{display:block}.tool-args{overflow-wrap:anywhere}
.task-heading,.task-row-heading{display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap;gap:4px 12px}
.task-heading{padding-bottom:6px}.task-row{padding:9px 0;border-top:1px solid var(--vscode-widget-border,var(--vscode-sideBar-border))}
.task-row p{margin:4px 0;overflow-wrap:anywhere}.task-row .task-goal,.task-status{font-size:.92em}.task-goal{color:var(--vscode-descriptionForeground)}
.task-excerpt{font-size:.92em;line-height:1.45}.task-problem{color:var(--vscode-editorWarning-foreground);font-size:.92em;overflow-wrap:anywhere}
.task-row details>summary,.task-accounting>summary,.session-usage>summary,.turn-changes>summary{font-size:.9em}
.task-accounting p,.session-usage p{font-size:.9em;overflow-wrap:anywhere}.run-history{padding:8px 10px;border-left:2px solid var(--vscode-tree-indentGuidesStroke);margin:8px 0}
.run-history p{margin:5px 0}.run-history dd{margin:2px 0 7px 10px;overflow-wrap:anywhere}.run-history pre{white-space:pre-wrap;overflow-wrap:anywhere}
.task-alert{margin:8px 0;color:var(--vscode-editorWarning-foreground);font-size:.92em}.task-alert span{display:block;margin:3px 0}
.session-details{padding:12px}.session-details dd{margin:4px 0 14px;overflow-wrap:anywhere}.session-details h3{font-size:1em}
.activity-record{color:var(--vscode-descriptionForeground)}.activity-record>summary{font-size:.9em}
.delegation{font-size:.95em}.activity-record .delegation{border:0;padding:0;margin:10px 0}
:root{color-scheme:light dark}*{box-sizing:border-box}body{font-family:var(--vscode-font-family);font-size:var(--vscode-font-size);color:var(--vscode-foreground);padding:0;margin:0 0 70px}button,input,textarea,select{font:inherit;color:inherit}header{position:sticky;top:0;z-index:3;display:flex;align-items:center;gap:4px;min-height:42px;padding:6px 8px;background:var(--vscode-sideBar-background);border-bottom:1px solid var(--vscode-sideBar-border)}.toolbar-spacer{flex:1}.empty-toolbar{min-height:38px;border-bottom-color:transparent}.title{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:600}.icon{border:0;background:transparent;padding:5px 8px;border-radius:5px;cursor:pointer}.icon:hover{background:var(--vscode-toolbar-hoverBackground)}.welcome{display:flex;min-height:calc(100vh - 110px);max-width:360px;margin:0 auto;padding:72px 24px 28px;flex-direction:column;align-items:center;text-align:center}.welcome-mark{display:flex;width:48px;height:48px;align-items:center;justify-content:center;margin-bottom:18px;border:1px solid var(--vscode-widget-border,var(--vscode-sideBar-border));border-radius:14px;background:var(--vscode-editorWidget-background,var(--vscode-editor-background));color:var(--vscode-textLink-foreground);font-family:var(--vscode-editor-font-family);font-size:17px;font-weight:700;box-shadow:0 5px 18px rgba(0,0,0,.12)}.welcome h1{margin:0 0 10px;font-size:20px;line-height:1.25}.welcome-copy{max-width:310px;margin:0 0 24px;color:var(--vscode-descriptionForeground);line-height:1.5}.welcome-context{width:100%;padding:11px 12px;border:1px solid var(--vscode-widget-border,var(--vscode-sideBar-border));border-radius:8px;background:var(--vscode-editorWidget-background,var(--vscode-editor-background));text-align:left}.welcome-context span{display:block;margin-bottom:3px;color:var(--vscode-descriptionForeground);font-size:11px;text-transform:uppercase;letter-spacing:.04em}.welcome-context strong{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:var(--vscode-editor-font-family);font-size:12px}.welcome-context.warning strong{white-space:normal;color:var(--vscode-editorWarning-foreground)}.welcome-model{display:flex;width:100%;align-items:center;gap:7px;padding:10px 3px 3px;color:var(--vscode-descriptionForeground);font-size:12px}.model-dot{width:7px;height:7px;border-radius:50%;background:var(--vscode-testing-iconPassed)}.welcome-actions{display:flex;width:100%;flex-direction:column;gap:8px;margin-top:18px}.welcome-actions button{width:100%;min-height:34px;padding:7px 12px;border-radius:6px;cursor:pointer}.welcome-actions button:disabled{cursor:not-allowed;opacity:.55}.welcome-primary{border:1px solid var(--vscode-button-border,transparent);background:var(--vscode-button-background);color:var(--vscode-button-foreground)}.welcome-primary:hover:not(:disabled){background:var(--vscode-button-hoverBackground)}.welcome-secondary{border:1px solid var(--vscode-button-secondaryBackground);background:transparent;color:var(--vscode-foreground)}.welcome-secondary:hover{background:var(--vscode-button-secondaryHoverBackground)}.welcome-trust{margin:18px 0 0;color:var(--vscode-descriptionForeground);font-size:11px;line-height:1.45}.welcome-error{max-width:100%;margin:16px 0 0;color:var(--vscode-editorError-foreground);font-family:var(--vscode-editor-font-family);font-size:11px;overflow-wrap:anywhere}.delegation{margin:10px 0;padding:8px;border:1px solid var(--vscode-widget-border);border-radius:6px}.delegated-run{margin-top:10px;padding:8px;border-left:2px solid var(--vscode-progressBar-background)}.delegated-run p{white-space:pre-wrap;overflow-wrap:anywhere}.delegated-run dd{margin-left:10px;overflow-wrap:anywhere}.delegated-run pre{white-space:pre-wrap;overflow-wrap:anywhere}main{padding:8px}.turn{padding:10px 0 18px;border-bottom:1px solid var(--vscode-sideBarSectionHeader-border)}.user-row{display:flex;justify-content:flex-end;margin:4px 0 14px}.user-bubble{max-width:82%;padding:8px 11px;border-radius:12px 12px 3px 12px;background:var(--vscode-inputOption-activeBackground);white-space:pre-wrap;overflow-wrap:anywhere}.agent-markdown{line-height:1.5;overflow-wrap:anywhere}.agent-markdown p{margin:8px 0}.agent-markdown h2,.agent-markdown h3,.agent-markdown h4{font-size:1em;margin:12px 0 6px}.agent-markdown pre{padding:8px;overflow:auto;background:var(--vscode-textCodeBlock-background);border-radius:5px}.agent-markdown code{font-family:var(--vscode-editor-font-family)}details{margin:7px 0;color:var(--vscode-descriptionForeground)}summary{cursor:pointer;user-select:none;padding:5px 0}.status{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:7px;background:var(--vscode-testing-iconPassed)}.status.submitted{background:var(--vscode-descriptionForeground)}.status.failed{background:var(--vscode-testing-iconFailed)}.status.running{background:var(--vscode-progressBar-background)}.status.denied{background:var(--vscode-editorWarning-foreground)}.tools{margin-left:5px;padding-left:10px;border-left:1px solid var(--vscode-tree-indentGuidesStroke)}.tool{display:flex;gap:8px;padding:6px 0;color:var(--vscode-foreground)}.tool-icon{width:12px}.tool-body{min-width:0;flex:1}.tool-name{font-family:var(--vscode-editor-font-family);font-weight:600;color:var(--vscode-symbolIcon-functionForeground)}.tool-args{margin-top:2px;color:var(--vscode-descriptionForeground);font-family:var(--vscode-editor-font-family);font-size:.9em;white-space:pre-wrap;overflow-wrap:anywhere}.small{font-size:.9em}.muted{color:var(--vscode-descriptionForeground)}.notice{margin:7px 0;padding:7px;border-left:2px solid var(--vscode-editorWarning-foreground);background:var(--vscode-textBlockQuote-background)}.notice.error{border-color:var(--vscode-editorError-foreground)}.result-card{margin-top:10px;border:1px solid var(--vscode-sideBar-border);border-radius:6px;overflow:hidden}.result-title{font-weight:600;padding:7px 9px;background:var(--vscode-sideBarSectionHeader-background)}.file{padding:5px 9px;border-top:1px solid var(--vscode-sideBar-border);font-family:var(--vscode-editor-font-family);font-size:.9em}.validation{margin:8px;padding:0 8px;color:var(--vscode-descriptionForeground)}.validation.passed{color:var(--vscode-testing-iconPassed)}.validation.failed{color:var(--vscode-testing-iconFailed)}.running-indicator{padding:10px;color:var(--vscode-progressBar-background)}.composer{position:fixed;bottom:0;left:0;right:0;z-index:4;display:flex;gap:6px;padding:8px;background:var(--vscode-sideBar-background);border-top:1px solid var(--vscode-sideBar-border)}.composer textarea{flex:1;resize:none;padding:7px;background:var(--vscode-input-background);border:1px solid var(--vscode-input-border);border-radius:6px}.composer button{border:0;border-radius:6px;padding:7px 11px;background:var(--vscode-button-background);color:var(--vscode-button-foreground)}.composer button:disabled,.composer textarea:disabled{opacity:.6}.continue{width:100%}.history{padding:10px}.history-search{display:flex;align-items:center;gap:7px;padding:0 9px;border:1px solid var(--vscode-input-border,transparent);border-radius:6px;background:var(--vscode-input-background)}.history-search:focus-within{border-color:var(--vscode-focusBorder)}.history-search span{color:var(--vscode-descriptionForeground);font-size:16px}.history-search input{width:100%;min-width:0;margin:0;padding:8px 0;border:0;outline:0;background:transparent}.scope-tabs{display:grid;grid-template-columns:1fr 1.35fr;gap:3px;margin:10px 0;padding:3px;border-radius:7px;background:var(--vscode-editorWidget-background,var(--vscode-editor-background))}.scope-tabs button{padding:6px;border:0;border-radius:5px;background:transparent;color:var(--vscode-descriptionForeground);cursor:pointer;font-size:12px}.scope-tabs button.selected{background:var(--vscode-list-activeSelectionBackground);color:var(--vscode-list-activeSelectionForeground)}.scope-tabs span{margin-left:3px;opacity:.75}.history-filter{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:10px 2px 14px;color:var(--vscode-descriptionForeground);font-size:12px}.history-filter select{max-width:170px;padding:4px 24px 4px 7px;border:1px solid var(--vscode-dropdown-border,transparent);border-radius:4px;background:var(--vscode-dropdown-background);color:var(--vscode-dropdown-foreground)}.session-group{margin:8px 0 16px}.group-title{display:flex;justify-content:space-between;padding:4px 8px;color:var(--vscode-descriptionForeground);font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.035em}.group-title span{font-weight:400}.session{display:flex;width:100%;gap:8px;align-items:flex-start;text-align:left;border:0;background:transparent;padding:9px 8px;border-radius:6px;cursor:pointer}.session>span{flex:1;min-width:0}.session>small{display:flex;flex-direction:column;align-items:flex-end;gap:5px;color:var(--vscode-descriptionForeground)}.session-title,.session-meta{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.session-meta{margin-top:3px;color:var(--vscode-descriptionForeground);font-size:11px}.session:hover{background:var(--vscode-list-hoverBackground)}.session.active{background:var(--vscode-list-activeSelectionBackground);color:var(--vscode-list-activeSelectionForeground)}.session.remote-session{cursor:not-allowed;opacity:.62}.workspace-lock{font-size:11px}.history-empty{display:flex;padding:42px 12px;flex-direction:column;gap:5px;align-items:center;color:var(--vscode-descriptionForeground);text-align:center}.history-empty strong{color:var(--vscode-foreground)}.history-hint{margin:18px 8px;color:var(--vscode-descriptionForeground);font-size:11px;line-height:1.45}`;
}

function deactivate() {}

module.exports = {
  activate, deactivate, renderChat, safeJson, styles, ChatViewProvider, RuntimeConnection, sameWorkspace,
  renderToolFailureHtml, renderContextSummaryHtml, renderTruncationNoticeHtml,
  renderChangesHtml, renderDelegationHtml, renderChildRunHtml, renderTaskNoticeHtml, runName, runState, renderActivityHtml, toolActivityLabel, renderTurnHtml, activitySteps, renderTaskAlertsHtml, renderSessionDetailsHtml, renderSessionDiagnosticsHtml, renderLiveProgressHtml, validationLabel, validateProfile,
};
