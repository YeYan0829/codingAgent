"use strict";

// 与宿主主题一致的纯渲染组件；图标形态参照已核对的Claude控件，路径由项目自有。
function uiIcon(name) {
  const paths={
    plus:'<path d="M12 5v14M5 12h14"/>',
    new:'<path d="M5 18l-2 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4v7a4 4 0 0 1-4 4Z"/><path d="M12 7v7M8.5 10.5h7"/>',
    back:'<path d="m12 5-7 7 7 7M5 12h14"/>',
    history:'<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>',
    send:'<path d="M12 19V5m-6 6 6-6 6 6"/>',
    stop:'<rect x="6" y="6" width="12" height="12" rx="1"/>',
    close:'<path d="m6 6 12 12M18 6 6 18"/>',
    command:'<rect x="3" y="3" width="18" height="18" rx="3"/><path d="m15 6-6 12"/>',
    chevron:'<path d="m9 5 7 7-7 7"/>',
    agents:'<circle cx="9" cy="8" r="3"/><path d="M3 20v-3a6 6 0 0 1 12 0v3M16 5a3 3 0 0 1 0 6M18 14a5 5 0 0 1 3 5"/>',
    copy:'<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/>',
    refresh:'<path d="M20 7v5h-5M4 17v-5h5M6 6a8 8 0 0 1 13 3M18 18A8 8 0 0 1 5 15"/>',
    undo:'<path d="m8 4-5 5 5 5M3 9h10a7 7 0 0 1 7 7v3"/>',
    diff:'<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6M8 12h8M12 9v6M8 18h8"/>',
    external:'<path d="M14 3h7v7M21 3l-9 9M10 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-5"/>',
    branch:'<circle cx="6" cy="5" r="3"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="19" r="3"/><path d="M6 8v8M18 9a9 9 0 0 1-9 9"/>'
  };
  return '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+(paths[name]||paths.chevron)+'</svg>';
}

function renderNavigation(title, busy=false) {
  return '<header class="session-header"><span class="title">'+title+'</span><button id="nav-history" class="icon" title="会话历史" aria-label="会话历史">'+uiIcon('history')+'</button><button id="nav-new" class="icon" title="'+(busy?'请先停止当前任务':'新建会话')+'" aria-label="新建会话" '+(busy?'disabled':'')+'>'+uiIcon('new')+'</button></header>';
}

function renderWelcome(state,esc) {
  const configured=Boolean(state.secretState?.[state.profile.provider]);
  const workspace=String(state.workspacePath||'').split(/[\\/]/).pop();
  return '<section class="welcome"><h1>'+(!workspace?'先打开一个代码仓库':'从一个具体任务开始')+'</h1><p class="welcome-copy">描述要修复的问题或想了解的代码。</p>'
    +(workspace?'<p class="muted">'+esc(workspace)+'</p>':'<p>支持 Linux 和 WSL2。</p>')
    +(state.runtimeError?'<p class="form-error">'+esc(state.runtimeError)+'</p><button id="retry-runtime" class="primary-action">重新连接</button>':workspace?'<button id="'+(configured?'new-empty':'configure-empty')+'" class="primary-action">'+(configured?'开始新任务':'连接模型，开始任务')+'</button>':'')
    +(state.operationError?'<p class="form-error" role="alert">'+esc(state.operationError)+'</p>':'')
    +'<p class="welcome-trust">编辑直接写入当前项目，受控文件编辑可以撤销。</p></section>';
}

function renderSettings(p,secretState,esc,error='',pending=false) {
  const selected=(a,b)=>a===b?'selected':'';
  return '<section class="page-heading"><h1>模型连接与设置</h1><p>保存后用于新会话，当前会话保留原配置。</p></section><form id="settings-form" class="settings"><fieldset><legend>模型连接</legend>'
    +'<label>模型服务<select id="provider">'+[['deepseek','DeepSeek'],['glm','GLM'],['fake','本地演练（免费）']].map(([id,label])=>'<option value="'+id+'" '+selected(id,p.provider)+'>'+label+'</option>').join('')+'</select></label>'
    +'<label>模型名称<input id="model" required value="'+esc(p.model)+'"></label><div id="key-fields" '+(p.provider==='fake'?'hidden':'')+'><label>API Key<input id="api-key" type="password" autocomplete="off" value="'+esc(p.apiKey||'')+'" placeholder="'+(secretState[p.provider]?'已保存，留空继续使用':'输入 API Key')+'"></label><div class="secret-row"><span id="key-status" class="muted">'+(secretState[p.provider]?'已安全保存':'尚未配置')+'</span><button id="clear-key" type="button" class="quiet-button" '+(secretState[p.provider]?'':'hidden')+'>移除密钥</button></div></div><p id="fixture-hint" class="muted small" '+(p.provider==='fake'?'':'hidden')+'>固定免费响应，仅用于流程演练。</p></fieldset>'
    +'<details data-setting-section="model"><summary>生成参数</summary><div class="field-stack"><label class="toggle"><input id="reasoning-enabled" type="checkbox" '+(p.reasoningEnabled?'checked':'')+'>启用模型推理</label><label>推理强度<select id="reasoning-effort">'+(p.provider==='glm'&&p.model!=='glm-5.3'?['high','max']:['low','high','max']).map(v=>'<option '+selected(v,p.reasoningEffort)+'>'+v+'</option>').join('')+'</select></label><label>Temperature<input id="temperature" type="number" min="0" max="2" step="0.1" value="'+esc(p.temperature??'')+'"></label><label>单次输出上限（Token）<input id="max-tokens" type="number" min="1" max="131072" value="'+esc(p.maxTokens??'')+'" placeholder="使用模型默认值"></label></div></details>'
    +'<details data-setting-section="execution"><summary>子任务与用量</summary><div class="field-stack"><label class="toggle"><input id="delegation-enabled" type="checkbox" '+(p.delegationEnabled?'checked':'')+'>允许委派子任务</label><p class="muted small">默认关闭。启用后可探索代码、执行命令或处理综合任务；主方同步等待，子任务使用当前共享项目。</p><label>每 Token 估算单价（百万分之一币种）<input id="token-price" type="number" min="1" value="'+esc(p.costPerTokenMicrounits??'')+'"></label><label>币种<input id="fee-currency" required pattern="[A-Z]{3}" value="'+esc(p.feeCurrency)+'"></label><p class="muted small">用量与费用只记录，不构成执行限额。</p></div></details>'
    +'<div id="settings-error" class="form-error" role="alert" '+(error?'':'hidden')+'>'+esc(error)+'</div><div class="settings-footer"><button type="submit" class="primary-action save" '+(pending?'disabled':'')+'>'+(pending?'正在保存…':'保存设置')+'</button></div></form>';
}

function taskStatus(detail,unavailable) {
  if(unavailable)return '状态待确认 · 本地服务未连接';
  const live={running:'正在处理',stopping:'正在停止并回收资源',external_running:'任务由其他窗口运行',awaiting_approval:'等待你的批准'}[detail?.executionState];
  if(live)return live;
  if(detail&&!Array.isArray(detail.turns))return {Ready:'可以继续追问','Continue available':'有未完成轮次','Validation failed':'验证记录有失败','Manual recovery required':'需要核对现场'}[detail.attentionSummary]||'查看历史记录';
  return detail?.availableActions?.canContinue?'有未完成轮次':detail?.turns?.length?'可以继续追问':'等待你的任务';
}

module.exports={uiIcon,renderNavigation,renderWelcome,renderSettings,taskStatus};
