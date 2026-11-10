"use strict";

// 只管理Webview交互；所有项目操作仍通过宿主和Runtime入口。
function mountChat(initialState, vscode) {
  let state=initialState, page='task', overlay=null, selectedRun=null, fullProcess=false;
  let settingsDraft=null, settingsError='', settingsPending=false, accessDraft=null, accessError='';
  let historyQuery='', historyScope='workspace', feedback='', submission=null;
  let returnFocus=null, renderSignature='', overlaySignature='';
  const pending=new Set(), processes=new Map(), disclosures=new Set();
  const persisted=vscode.getState?.()||{};
  const drafts=new Map(Object.entries(persisted.drafts||{}).filter(([,v])=>typeof v==='string'));
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const app=document.getElementById('app');
  app.innerHTML='<div id="content"></div><div id="overlays"></div><div id="copy-status" class="copy-status" role="status" aria-live="polite"></div>';
  const content=document.getElementById('content'), overlays=document.getElementById('overlays');
  const sid=()=>state.detail?.sessionId||'';
  const transcript=createTranscriptViewState({document,window,vscode:{getState:()=>vscode.getState?.(),setState:value=>vscode.setState?.({...value,drafts:Object.fromEntries(drafts)})},requestAnimationFrame,sessionId:sid});
  const copyPayloads=[];
  const copyButton=(text,label,className='message-copy')=>{
    const id=copyPayloads.push(String(text??''))-1;
    return '<button type="button" class="'+className+'" data-copy="'+id+'" aria-label="'+esc(label)+'" title="'+esc(label)+'">'+uiIcon('copy')+'</button>';
  };
  const markdown=createMarkdownRenderer(globalThis.markdownit,copyButton);
  const processMarkdown=createMarkdownRenderer(globalThis.markdownit,()=> '');
  const runs=()=> (state.detail?.orchestration?.tasks||[]).flatMap(task=>task.runs.map(run=>({...run,task})));
  const busy=()=>Boolean(state.detail&&state.detail.executionState!=='idle');
  const disabled=()=>Boolean(state.runtimeError);
  function sendAction(message) {
    if(['run','continue','stop','resolveApproval','openDiff'].includes(message.type))message={...message,sessionId:sid()};
    if(message.type==='resolveApproval')message.approvalId=state.detail?.pendingApproval?.approvalId;
    if(pending.has(message.type))return;
    feedback='';
    pending.add(message.type);vscode.postMessage(message);syncPending();
  }
  function syncPending() {
    const selectors={newTask:'#nav-new,#new-empty',selectSession:'[data-session]',resolveApproval:'#allow-approval,#reject-approval',stop:'#stop,#end-turn',undoEdits:'#undo-edits',saveRepositoryAccess:'#save-repository-access',loadProcess:'#load-process'};
    for(const type of pending)document.querySelectorAll(selectors[type]||'[data-never]').forEach(el=>{el.disabled=true;el.setAttribute('aria-busy','true');});
  }
  function remember() {
    const prompt=document.getElementById('prompt');if(prompt)drafts.set(sid(),prompt.value);
    const form=document.getElementById('settings-form');if(form)settingsDraft=readSettings();
    const paths=document.getElementById('denied-paths');if(paths)accessDraft=paths.value;
    document.querySelectorAll('details[data-disclosure],details[data-setting-section]').forEach(el=>{
      const key=el.dataset.disclosure||'settings:'+el.dataset.settingSection;
      if(el.open)disclosures.add(key);else disclosures.delete(key);
    });
    transcript.capture();
    const old=vscode.getState?.()||{};
    vscode.setState?.({layoutVersion:1,sessions:old.sessions||{},drafts:Object.fromEntries(drafts)});
  }
  function readSettings() {
    const val=id=>document.getElementById(id)?.value||'';
    const optional=id=>val(id).trim()===''?null:Number(val(id));
    return {provider:val('provider'),model:val('model').trim(),apiKey:val('api-key'),reasoningEnabled:document.getElementById('reasoning-enabled').checked,
      reasoningEffort:val('reasoning-effort'),temperature:optional('temperature'),maxTokens:optional('max-tokens'),delegationEnabled:document.getElementById('delegation-enabled').checked,
      costPerTokenMicrounits:optional('token-price'),feeCurrency:val('fee-currency')};
  }
  function showOverlay(kind,runId) {
    remember();returnFocus=document.activeElement?.id||null;overlay=kind;selectedRun=runId||null;fullProcess=false;overlaySignature='';renderOverlay();
  }
  function closeOverlay() {
    overlay=null;selectedRun=null;fullProcess=false;overlaySignature='';overlays.innerHTML='';
    document.getElementById(returnFocus)?.focus({preventScroll:true});
  }
  function openPage(next) {remember();closeOverlay();page=next;feedback='';renderSignature='';render();requestAnimationFrame(()=>document.getElementById(next==='settings'?'provider':'prompt')?.focus());}
  function elapsed(run) {
    if(!run.startedAt)return '';
    return '<span class="elapsed" data-start="'+esc(run.startedAt)+'" data-end="'+esc(run.completedAt||'')+'"></span>';
  }
  function renderOverlay() {
    if(!overlay){overlays.innerHTML='';return;}
    const oldFocus=document.activeElement?.id, start=document.activeElement?.selectionStart, end=document.activeElement?.selectionEnd;
    const scroll=overlays.querySelector('.dialog-body')?.scrollTop||0;
    let body='',title='',extra='';
    if(overlay==='menu') {
      title='会话操作';
      body=[['new','新建会话'],['history','会话历史'],['settings','模型连接与设置'],['info','会话信息'],['logs','运行日志']].map(([id,label])=>'<button class="menu-item" data-menu="'+id+'" '+((id==='new'&&(busy()||submission))||id==='info'&&!state.detail?'disabled':'')+'>'+esc(label)+'</button>').join('');
    } else if(overlay==='history') {
      title='会话历史';
      const values=(state.sessions||[]).filter(s=>(historyScope==='all'||s.isCurrentWorkspace)&&(!historyQuery||[s.title,s.workspaceLabel,s.workspace].some(v=>String(v||'').toLowerCase().includes(historyQuery.toLowerCase()))));
      body='<label class="history-search"><input id="search" aria-label="搜索任务或仓库" placeholder="搜索任务或仓库" value="'+esc(historyQuery)+'"></label><div class="scope-tabs"><button data-scope="workspace" class="'+(historyScope==='workspace'?'selected':'')+'">当前仓库</button><button data-scope="all" class="'+(historyScope==='all'?'selected':'')+'">全部仓库</button></div>'
        +(values.length?values.map(s=>'<button class="session" data-session="'+esc(s.sessionId)+'" '+(!s.isCurrentWorkspace?'disabled title="请先打开对应仓库"':'')+'><span><strong class="session-title">'+esc(s.title)+'</strong><span class="session-meta">'+esc(s.workspaceLabel)+(s.isCurrentWorkspace?'':' · 请先打开此仓库')+' · '+esc(taskStatus(s,false))+'</span></span></button>').join(''):'<p class="history-empty">'+(state.sessions?.length?'没有符合条件的任务':'还没有任务记录')+'</p>')+'<p class="muted small">其他仓库的记录需打开对应仓库后继续。</p>';
      extra='<button id="refresh" class="icon" aria-label="刷新历史">'+uiIcon('refresh')+'</button>';
    } else if(overlay==='agents') {
      const values=runs(), run=values.find(r=>r.runId===selectedRun);
      title=run?runName(run)+(fullProcess?' · 只读过程':''):'子任务概览';
      if(!run)body='<div class="agent-main">主 Agent · '+esc(taskStatus(state.detail,state.runtimeError))+'</div><div class="agent-tree">'+values.map(r=>'<button class="agent-row" data-agent="'+esc(r.runId)+'"><span><strong>'+esc(runName(r))+'</strong><small>'+esc(r.role||'子任务')+' · '+esc(state.runtimeError?'状态待确认':runState(r))+'</small></span>'+elapsed(r)+'</button>').join('')+'</div>';
      else {
        const owner=state.detail.pendingApproval?.owner;
        const waiting=owner?.runId===run.runId;
        const active=['queued','running'].includes(run.status)&&!state.runtimeError&&['running','awaiting_approval','stopping'].includes(state.detail.executionState);
        const key=sid()+':'+run.runId;
        const loaded=processes.get(key);
        const stale=loaded&&loaded.snapshotSeq!==state.detail.lastSeq;
        const source=fullProcess&&loaded&&!stale?loaded.items:run.items||[];
        body='<p class="muted">'+esc(waiting?'等待你的批准':state.runtimeError?'状态待确认':runState(run))+' · '+esc(run.role||'子任务')+' · '+elapsed(run)+'</p><p class="muted small">主 Agent 委派'+(active?'，主 Agent 正在等待结果':'')+'</p>'
          +(active?'<p class="current-operation">'+esc(waiting?'当前操作等待批准':run.currentActivity?toolActivityLabel(run.currentActivity):run.activity?.label||'正在处理')+'</p>':'')
          +(run.error?'<p class="form-error">'+esc(run.error)+'</p>':'');
        if(fullProcess)body+=source.map(item=>item.type==='markdown'?'<div class="agent-markdown">'+processMarkdown(item.text)+'</div>':item.type==='activityGroup'?renderActivityHtml(item,'full-'+run.runId+':'+item.eventSeq,esc,'',null,active?'running':'idle',null,processMarkdown):item.type==='notice'?'<p class="notice">'+esc(item.message)+'</p>':'').join('')
          +(loaded?.hasMore||!loaded||stale?'<button id="load-process" class="secondary-action" data-run="'+esc(run.runId)+'" data-after="'+esc(stale?0:loaded?.lastSeq||0)+'">'+(stale?'过程已更新，加载最新完整记录':loaded?'加载更多过程':'加载完整公开过程')+'</button>':'<p class="muted small">已显示已采集的公开过程。过长单项仍有采集/展示上限。</p>');
        else body+=(run.findings?'<div class="agent-markdown result-preview">'+processMarkdown(run.findings)+'</div>':'')
          +'<details data-disclosure="goal-'+esc(run.runId)+'"><summary>任务提示</summary><pre class="tool-output">'+esc(run.goal)+'</pre></details>'
          +'<details data-disclosure="tools-'+esc(run.runId)+'"><summary>工具调用（'+source.flatMap(i=>i.tools||[]).length+'）</summary>'+source.filter(i=>i.type==='activityGroup').map(item=>renderActivityHtml(item,'card-'+run.runId+':'+item.eventSeq,esc,'',null,active?'running':'idle',null,processMarkdown)).join('')+'</details>'
          +'<button id="open-transcript" class="secondary-action">打开完整过程</button>'+renderRunDiagnosticsHtml(run,run.task,esc);
        extra='<button id="agent-back" class="icon" aria-label="返回子任务'+(fullProcess?'详情':'概览')+'">'+uiIcon('back')+'</button>';
      }
    }
    const signature=JSON.stringify([overlay,selectedRun,fullProcess,title,body,extra]);
    if(signature===overlaySignature)return;
    overlaySignature=signature;
    const existed=Boolean(overlays.querySelector('dialog'));
    overlays.innerHTML='<dialog class="panel-dialog '+(overlay==='menu'?'command-dialog':'')+'" aria-labelledby="dialog-title"><div class="dialog-heading">'+extra+'<strong id="dialog-title" tabindex="-1">'+esc(title)+'</strong><button id="close-overlay" class="icon" aria-label="关闭">'+uiIcon('close')+'</button></div><div class="dialog-body">'+body+'</div></dialog>';
    const dialog=overlays.querySelector('dialog');dialog.showModal();
    dialog.addEventListener('cancel',e=>{e.preventDefault();closeOverlay();});
    dialog.querySelector('.dialog-body').scrollTop=scroll;
    for(const el of dialog.querySelectorAll('details[data-disclosure]'))el.open=disclosures.has(el.dataset.disclosure);
    const focus=oldFocus&&document.getElementById(oldFocus);
    if(focus&&!focus.disabled){focus.focus();if(Number.isInteger(start)&&focus.setSelectionRange)focus.setSelectionRange(start,end);}
    else if(!existed)(document.getElementById(overlay==='history'?'search':'dialog-title'))?.focus();
    syncPending();tick();
  }
  function approvalCard(value) {
    if(!value)return '';
    const summary=value.summary||{}, permissions=summary.permissions||[];
    const run=runs().find(r=>r.runId===value.owner?.runId);
    const canApprove=!disabled()&&state.detail.executionState==='awaiting_approval'&&state.detail.availableActions?.canResolveApproval;
    return '<section class="approval-card" id="approval-card" tabindex="0" aria-label="权限请求"><strong>'+(value.kind==='command_permissions'?'是否允许这条命令使用额外权限？':'是否允许此操作？')+'</strong><p class="muted small">申请者：'+esc(run?runName(run):'主 Agent')+'</p>'
      +(summary.command?'<pre class="tool-output">'+esc(summary.command)+'</pre>':'<pre class="tool-output">'+esc(JSON.stringify(summary.arguments||{tool:summary.tool},null,2))+'</pre>')
      +(summary.cwd?'<p class="muted small">目录：'+esc(summary.cwd)+'</p>':'')
      +permissions.map(p=>'<p class="permission-row"><strong>'+esc(p.capability==='network'?'访问网络':p.capability)+'</strong><span>'+esc(p.scope==='once'?'仅一次':p.scope==='session'?'本会话':p.scope)+'</span><small>'+esc(p.reason||'')+'</small></p>').join('')
      +'<div class="approval-options"><button id="allow-approval" class="primary-action" '+(canApprove?'':'disabled')+'>1 '+(permissions.length===1&&permissions[0].scope==='once'?'仅允许一次':'允许所列操作')+'</button><button id="reject-approval" class="secondary-action" '+(canApprove?'':'disabled')+'>2 拒绝请求</button></div></section>';
  }
  function composer() {
    const d=state.detail, actions=d.availableActions||{}, live=busy(), values=runs();
    const waiting=d.pendingApproval, active=values.filter(r=>!r.terminalAck&&['queued','running'].includes(r.status)).length;
    const badge=values.length?'<button id="agents-pill" class="model-pill agents-pill" title="已记录 '+new Set(values.map(r=>r.agentId)).size+' 个子任务，运行中 '+active+' 个'+(waiting?'；有操作等待批准':'')+'"><span class="status '+(waiting?'denied':active?'running':'idle')+'"></span>'+new Set(values.map(r=>r.agentId)).size+' 个子任务</button>':'';
    const promptDisabled=submission||disabled()||!actions.canRun&&!live;
    return '<div class="composer-wrap">'+approvalCard(waiting)
      +(actions.canContinue&&!live?'<div class="resume-row"><button id="continue" class="link-action">继续未完成轮次</button><span class="muted small">也可输入新指令</span>'+(actions.canStop?'<button id="end-turn" class="link-action">结束本轮</button>':'')+'</div>':'')
      +'<div class="composer"><label class="copy-status" for="prompt">任务描述</label><textarea id="prompt" rows="3" placeholder="'+(live?'可以先写草稿，任务结束后发送':'描述要完成的代码任务…')+'" '+(promptDisabled?'disabled':'')+'></textarea><div class="composer-footer"><button id="command-menu" class="icon" aria-label="会话操作" title="会话操作 (/)">'+uiIcon('command')+'</button>'+badge
      +'<button id="model-pill" class="model-pill" title="配置用于新会话，当前模型不变" '+(live?'disabled':'')+'>'+esc(d.model)+'</button><span class="toolbar-spacer"></span>'
      +(live?'<button id="stop" class="submit-button" title="停止任务（包含子任务）" aria-label="停止任务" '+(!actions.canStop||disabled()?'disabled':'')+'>'+uiIcon('stop')+'</button>':'<button id="send" class="submit-button" title="发送任务" aria-label="发送任务" disabled>'+uiIcon('send')+'</button>')
      +'</div></div><p class="composer-hint">'+esc(disabled()?'连接恢复后可操作；已有记录与草稿保留。':d.executionState==='stopping'?'正在回收执行资源，已有修改保留。':'编辑直接写入项目 · Enter 发送 · Shift + Enter 换行')+'</p></div>';
  }
  function render() {
    copyPayloads.length=0;
    const focus=document.activeElement, focusId=focus?.id, start=focus?.selectionStart, end=focus?.selectionEnd;
    const d=state.detail;
    const title=page==='settings'?'模型连接与设置':page==='info'?'会话信息':d?.title&&!['New task','Untitled session'].includes(d.title)?d.title:'CodeAgent';
    let body='';
    if(page!=='task')body='<button id="back-task" class="link-action back-task">'+uiIcon('back')+'返回会话</button>';
    if(page==='settings')body+=(state.repositoryAccess?renderRepositoryAccess(state.repositoryAccess,accessDraft,esc,pending.has('saveRepositoryAccess'),accessError):'')+renderSettings(settingsDraft||state.profile,state.secretState,esc,settingsError,settingsPending);
    else if(page==='info'&&d)body+=renderSessionDetailsHtml(d,esc);
    else if(state.repositoryAccess?.configured===false)body+=renderRepositoryAccess(state.repositoryAccess,accessDraft,esc,pending.has('saveRepositoryAccess'),accessError);
    else if(!d)body+=renderWelcome(state,esc)+'<div class="welcome-menu"><button id="command-menu" class="icon" aria-label="会话操作">'+uiIcon('command')+'</button></div>';
    else {
      const attention=state.runtimeError?{title:'本地服务未连接',message:state.runtimeError}:d.globalAttention;
      body+='<div class="task-context"><span>'+esc(String(d.workspace||state.workspacePath||'').split(/[\\/]/).pop())+'</span><span class="task-status" role="status">'+esc(taskStatus(d,state.runtimeError))+'</span></div>'
        +(attention?'<section class="global-attention"><strong>'+esc(attention.title)+'</strong><p>'+esc(attention.message)+'</p><button id="retry-runtime" class="secondary-action">重新连接</button><button id="open-output" class="link-action">运行日志</button></section>':'')
        +(state.operationError?'<p class="form-error" role="alert">'+esc(state.operationError)+'</p>':'')
        +'<main data-chat-session="'+esc(d.sessionId)+'">'+(!d.turns?.length?'<section class="task-intro"><h1>这次要完成什么？</h1><p>描述目标和预期结果。</p><div class="prompt-examples"><button data-example="梳理这个项目的主要功能和运行方式，先不要修改代码。">了解这个项目</button><button data-example="定位失败测试的原因，完成修复并运行相关验证。">修复失败的测试</button></div></section>':'')
        +(d.turns||[]).map(t=>renderTurnHtml(t,d,esc,markdown,copyButton,disabled(),processMarkdown)).join('')+renderLiveProgressHtml(disabled()?{...d,executionState:'unavailable'}:d,esc)
        +renderChangesHtml(d.workspaceEdits,d,esc)+(d.changesSummary?.available?renderChangesHtml(d.changesSummary,d,esc):'')+'</main>'+composer();
    }
    const html=renderNavigation(esc(title),busy()||Boolean(submission))+(feedback?'<p class="feedback" role="status">'+esc(feedback)+'</p>':'')+body;
    if(html!==renderSignature){renderSignature=html;content.innerHTML=html;
      const prompt=document.getElementById('prompt');if(prompt){prompt.value=drafts.get(sid())||'';const send=document.getElementById('send');if(send)send.disabled=disabled()||busy()||!d.availableActions?.canRun||!prompt.value.trim()||Boolean(submission);}
      document.querySelectorAll('details[data-setting-section]').forEach(el=>el.open=disclosures.has('settings:'+el.dataset.settingSection));
      transcript.restore();syncSettings();
      const target=focusId&&document.getElementById(focusId);if(target&&!target.disabled){target.focus({preventScroll:true});if(Number.isInteger(start)&&target.setSelectionRange&&['text','password','textarea',''].includes(target.type||''))target.setSelectionRange(start,end);}
    }
    syncPending();renderOverlay();tick();measure();
  }
  function measure(){const height=document.querySelector('.composer-wrap')?.getBoundingClientRect().height||0;document.documentElement.style.setProperty('--composer-height',height+'px');}
  function tick(){document.querySelectorAll('[data-start]').forEach(el=>{const start=Date.parse(el.dataset.start),end=Date.parse(el.dataset.end)||Date.now(),seconds=Math.max(0,Math.floor((end-start)/1000));el.textContent=Number.isFinite(seconds)?String(Math.floor(seconds/60)).padStart(2,'0')+':'+String(seconds%60).padStart(2,'0'):'';});}
  function syncSettings(){if(!document.getElementById('settings-form'))return;const provider=document.getElementById('provider').value;const configured=Boolean(state.secretState[provider]);document.getElementById('key-fields').hidden=provider==='fake';document.getElementById('fixture-hint').hidden=provider!=='fake';document.getElementById('clear-key').hidden=!configured;document.getElementById('key-status').textContent=configured?'已安全保存':'尚未配置';document.getElementById('reasoning-enabled').disabled=provider==='fake';document.getElementById('reasoning-effort').disabled=provider==='fake'||!document.getElementById('reasoning-enabled').checked;}
  document.addEventListener('toggle',e=>{if(e.target.tagName==='DETAILS'){const key=e.target.dataset.disclosure||'settings:'+e.target.dataset.settingSection;if(e.target.open)disclosures.add(key);else disclosures.delete(key);}},true);
  document.addEventListener('input',e=>{
    if(e.target.id==='prompt'){drafts.set(sid(),e.target.value);const send=document.getElementById('send');if(send)send.disabled=disabled()||busy()||Boolean(submission)||!state.detail.availableActions?.canRun||!e.target.value.trim();if(e.target.value==='/'){e.target.value='';drafts.set(sid(),'');showOverlay('menu');}}
    if(e.target.id==='search'){historyQuery=e.target.value;renderOverlay();}
    if(e.target.closest('#settings-form'))settingsDraft=readSettings();
    remember();
  });
  document.addEventListener('change',e=>{
    if(e.target.id==='provider'){
      const provider=e.target.value;document.getElementById('model').value={fake:'c3-fixture',glm:'glm-5.3',deepseek:'deepseek-v4-flash'}[provider];document.getElementById('api-key').value='';document.getElementById('reasoning-enabled').checked=provider==='glm';document.getElementById('reasoning-effort').innerHTML=['low','high','max'].map(v=>'<option '+(v==='high'?'selected':'')+'>'+v+'</option>').join('');
    }
    if(e.target.closest('#settings-form')){syncSettings();settingsDraft=readSettings();}
  });
  document.addEventListener('submit',e=>{
    if(e.target.id==='repository-access-form'){e.preventDefault();accessDraft=document.getElementById('denied-paths').value;sendAction({type:'saveRepositoryAccess',paths:accessDraft,expectedRevision:state.repositoryAccess?.revision});}
    if(e.target.id==='settings-form'){e.preventDefault();if(settingsPending)return;settingsDraft=readSettings();const {apiKey,...profile}=settingsDraft;if(profile.provider!=='fake'&&!state.secretState[profile.provider]&&!apiKey.trim()){settingsError='请填写当前服务的 API Key';remember();renderSignature='';render();return;}settingsPending=true;settingsError='';sendAction({type:'saveSettings',profile,apiKey});remember();renderSignature='';render();}
  });
  function submit(){const prompt=document.getElementById('prompt');const message=prompt?.value.trim();if(!message||busy()||disabled()||submission||!state.detail?.availableActions?.canRun)return;submission={message,sessionId:sid()};drafts.set(sid(),'');prompt.value='';sendAction({type:'run',message});remember();renderSignature='';render();}
  document.addEventListener('keydown',e=>{
    if(e.isComposing||e.keyCode===229||e.ctrlKey||e.metaKey||e.altKey)return;
    if(e.key==='Enter'&&!e.shiftKey&&e.target.id==='prompt'&&!e.isComposing&&e.keyCode!==229){e.preventDefault();submit();}
    if(e.target.closest('#approval-card')&&['1','2','Escape'].includes(e.key)){e.preventDefault();if(!disabled())sendAction({type:'resolveApproval',allow:e.key==='1'});}
    else if(e.key==='Escape'&&!overlay&&busy()&&state.detail?.availableActions?.canStop&&!disabled()&&e.target.closest('#content')){e.preventDefault();sendAction({type:'stop'});}
  });
  document.addEventListener('click',e=>{
    const button=e.target.closest('button');if(!button||button.disabled)return;
    const id=button.id;
    if(id==='nav-history')showOverlay('history');
    else if(id==='nav-new'||id==='new-empty'){remember();sendAction({type:'newTask'});}
    else if(id==='configure-empty'||id==='model-pill')openPage('settings');
    else if(id==='command-menu')showOverlay('menu');
    else if(id==='close-overlay')closeOverlay();
    else if(id==='back-task')openPage('task');
    else if(id==='agents-pill')showOverlay('agents');
    else if(id==='agent-back'){if(fullProcess)fullProcess=false;else selectedRun=null;overlaySignature='';renderOverlay();}
    else if(id==='open-transcript'){fullProcess=true;overlaySignature='';renderOverlay();}
    else if(id==='load-process')sendAction({type:'loadProcess',sessionId:sid(),runId:button.dataset.run,afterSeq:Number(button.dataset.after)});
    else if(id==='send')submit();
    else if(id==='stop'||id==='end-turn')sendAction({type:'stop'});
    else if(id==='continue')sendAction({type:'continue'});
    else if(id==='allow-approval'||id==='reject-approval')sendAction({type:'resolveApproval',allow:id==='allow-approval'});
    else if(id==='undo-edits'){const edit=state.detail.workspaceEdits;sendAction({type:'undoEdits',sessionId:sid(),turnId:edit.turnId,expectedEditSeq:edit.lastEditSeq});}
    else if(id==='open-git')sendAction({type:'openGit'});
    else if(id==='retry-runtime')sendAction({type:'retryRuntime'});
    else if(id==='open-output')vscode.postMessage({type:'openOutput'});
    else if(id==='clear-key')sendAction({type:'clearApiKey',provider:document.getElementById('provider').value});
    else if(id==='refresh'||id==='refresh-repository-access')sendAction({type:'refresh'});
    else if(button.dataset.menu){const next=button.dataset.menu;if(next==='history')showOverlay('history');else if(next==='settings'||next==='info')openPage(next);else if(next==='logs'){closeOverlay();vscode.postMessage({type:'openOutput'});}else {closeOverlay();sendAction({type:'newTask'});}}
    else if(button.dataset.session){remember();sendAction({type:'selectSession',sessionId:button.dataset.session});}
    else if(button.dataset.scope){historyScope=button.dataset.scope;renderOverlay();}
    else if(button.dataset.agent||button.dataset.openRun){if(overlay==='agents'){selectedRun=button.dataset.agent||button.dataset.openRun;fullProcess=false;renderOverlay();}else showOverlay('agents',button.dataset.agent||button.dataset.openRun);}
    else if(button.dataset.changeFile)vscode.postMessage({type:'openDiff',sessionId:sid(),path:button.dataset.changeFile,historical:button.dataset.historical==='true'});
    else if(button.dataset.example){drafts.set(sid(),button.dataset.example);renderSignature='';render();document.getElementById('prompt')?.focus();}
    else if(button.dataset.copy!==undefined)vscode.postMessage({type:'copyText',copyId:Number(button.dataset.copy),text:copyPayloads[Number(button.dataset.copy)]});
  });
  window.addEventListener('message',e=>{
    const data=e.data;
    if(data?.type==='productState'){
      remember();const oldSid=sid();state=data.state;if(oldSid!==sid()){overlay=null;selectedRun=null;page='task';renderSignature='';overlaySignature='';}
      if(submission&&state.detail?.turns?.some(t=>t.userMessage===submission.message||t.message===submission.message))submission=null;
      render();
    } else if(data?.type==='submissionRejected'){
      if(submission){drafts.set(submission.sessionId,submission.message);submission=null;}renderSignature='';render();
    } else if(data?.type==='processPage'&&data.sessionId===sid()){
      const key=sid()+':'+data.runId, old=data.afterSeq?processes.get(key):null;processes.set(key,{...data,items:[...(old?.items||[]),...data.items].filter((v,i,a)=>a.findIndex(x=>x.eventSeq===v.eventSeq)===i)});renderOverlay();
    } else if(data?.type==='operationComplete'){
      pending.delete(data.action);
      if(data.action==='run'&&data.ok){submission=null;renderSignature='';}
      if(data.action==='selectSession'&&data.ok)closeOverlay();
      if(data.action==='newTask'&&data.ok)requestAnimationFrame(()=>document.getElementById('prompt')?.focus());
      if(data.action==='saveSettings'){settingsPending=false;if(data.ok){settingsDraft=null;settingsError='';page='task';feedback='设置已保存，之后的新任务将使用此配置。';}else settingsError=data.error||'保存失败，输入已保留';renderSignature='';}
      if(data.action==='saveRepositoryAccess'){accessError=data.ok?'':data.error||'保存失败';if(data.ok)accessDraft=null;renderSignature='';}
      if(!data.ok){feedback=data.error||'操作失败，现场已保留';renderSignature='';}
      // 解除同一快照下的待提交禁用状态。
      renderSignature='';overlaySignature='';render();
    } else if(data?.type==='copyResult'){
      const status=document.getElementById('copy-status');status.textContent=data.ok?'已复制':'复制失败，请重试';
    }
  });
  window.addEventListener('resize',measure);
  window.addEventListener('beforeunload',remember);
  if(typeof ResizeObserver!=='undefined')new ResizeObserver(measure).observe(content);
  setInterval(tick,1000);
  render();
}

module.exports={mountChat};
