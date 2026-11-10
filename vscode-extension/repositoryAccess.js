"use strict";

function renderRepositoryAccess(access, draft, esc, pending = false, error = '') {
  const first = access?.configured === false;
  const value = draft === null ? (access?.deniedPaths || []).join('\n') : draft;
  return '<section class="repository-access"><h2>仓库文件访问</h2>'
    + '<p class="muted">'+(first?'开始任务前，请选择允许 Agent 访问的范围。':'设置仅用于当前仓库。')+'可以留空，之后随时修改。</p>'
    + '<form id="repository-access-form" class="settings"><label for="denied-paths">禁止访问的文件或目录（每行一项）</label>'
    + '<textarea id="denied-paths" rows="4" placeholder=".env&#10;private/" spellcheck="false">'+esc(value)+'</textarea>'
    + '<p class="muted small">填写已存在的仓库相对路径；目录包括全部子文件。不支持通配符。不自动识别秘密。</p>'
    + '<p class="muted small">禁止项同时限制读取和修改；启用后，命令也不能读取仓库 Git 历史。修改设置前需停止任务，保存后请新建任务；无法撤回此前已发送给模型的内容。</p>'
    + '<div role="alert" id="repository-access-error" class="form-error" '+(error?'':'hidden')+'>'+esc(error)+'</div>'
    + '<button type="submit" id="save-repository-access" class="primary-action" '+(pending?'disabled':'')+'>'+(pending?'正在保存…':first?'保存访问设置并继续':'保存仓库访问设置')+'</button><button type="button" id="refresh-repository-access" class="quiet-button">刷新仓库设置</button></form></section>';
}
module.exports = {renderRepositoryAccess};
