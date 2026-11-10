"use strict";

// 只保存查看位置与展开选择，不读取或恢复 Runtime 执行／批准状态。
function createTranscriptViewState({document, window, vscode, requestAnimationFrame, sessionId}) {
  const persisted = vscode.getState?.();
  const sessions = persisted?.layoutVersion === 1 && persisted.sessions && typeof persisted.sessions === 'object'
    ? {...persisted.sessions} : {};
  let activeSession;
  let open = new Set();
  let savedY = 0;
  let follow = true;
  let anchor = null;
  let restoring = false;
  const nodes = selector => [...document.querySelectorAll(selector)];
  const chatVisible = () => !document.querySelector || document.querySelector('main[data-chat-session]')?.dataset.chatSession === activeSession;
  function activate() {
    const next = String(sessionId() || '');
    if (next === activeSession) return;
    activeSession = next;
    const stored = sessions[next] || {};
    open = new Set(Array.isArray(stored.open) ? stored.open.filter(value => typeof value === 'string') : []);
    savedY = Number.isFinite(stored.scrollY) ? Math.max(0, stored.scrollY) : 0;
    follow = stored.followBottom !== false;
    anchor = stored.anchor && typeof stored.anchor.key === 'string' && Number.isFinite(stored.anchor.offset) ? stored.anchor : null;
  }
  function save() {
    if (!activeSession) return;
    sessions[activeSession] = {open: [...open], scrollY: savedY, followBottom: follow, anchor};
    vscode.setState?.({layoutVersion: 1, sessions});
  }
  function capture() {
    if (restoring || String(sessionId() || '') !== activeSession || !chatVisible()) return;
    nodes('details[data-disclosure]').forEach(el => el.open ? open.add(el.dataset.disclosure) : open.delete(el.dataset.disclosure));
    savedY = window.scrollY;
    follow = window.innerHeight + savedY >= document.body.scrollHeight - 24;
    const visible = !follow && nodes('[data-scroll-anchor]').map(el => ({el, rect: el.getBoundingClientRect?.()}))
      .filter(value => value.rect && value.rect.bottom > 0 && value.rect.top < window.innerHeight);
    const selected = visible && (visible.find(value => value.rect.top >= 0) || visible.at(-1));
    anchor = selected ? {key: selected.el.dataset.scrollAnchor, offset: selected.rect.top} : null;
    save();
  }
  function restore() {
    activate();
    if (!chatVisible()) return;
    restoring = true;
    nodes('details[data-disclosure]').forEach(el => {el.open = open.has(el.dataset.disclosure);});
    const targetSession = activeSession;
    requestAnimationFrame(() => {
      if (targetSession !== activeSession) return;
      if (follow) window.scrollTo(0, document.body.scrollHeight);
      else {
        const target = anchor && nodes('[data-scroll-anchor]').find(el => el.dataset.scrollAnchor === anchor.key);
        const top = target?.getBoundingClientRect?.().top;
        window.scrollTo(0, Number.isFinite(top) ? window.scrollY + top - anchor.offset : savedY);
      }
      restoring = false;
    });
  }
  activate();
  document.addEventListener?.('toggle', event => {if (event.target?.tagName === 'DETAILS') capture();}, true);
  window.addEventListener('scroll', capture, {passive: true});
  window.addEventListener('beforeunload', capture);
  return {capture, restore};
}

module.exports = {createTranscriptViewState};
