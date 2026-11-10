"use strict";
const test = require('node:test');
const assert = require('node:assert/strict');
const {createTranscriptViewState} = require('../viewState');

function browser(stored = undefined) {
  const handlers = {};
  const frames = [];
  let session = 'one';
  let state = stored;
  const elements = [{dataset: {disclosure: 'child-one:r', scrollAnchor: 'child-one:r'}, open: false,
    contentTop: 400, getBoundingClientRect() {return {top: this.contentTop - window.scrollY, bottom: this.contentTop - window.scrollY + 60};}}];
  const document = {body: {scrollHeight: 2000},
    querySelector: () => ({dataset: {chatSession: session}}),
    querySelectorAll: selector => selector === 'details[data-disclosure]' ? elements : elements,
    addEventListener: (name, fn) => {handlers[name] = fn;}};
  const window = {scrollY: 0, innerHeight: 600,
    scrollTo(x, y) {this.scrollY = Math.max(0, Math.min(y, document.body.scrollHeight - this.innerHeight));},
    addEventListener: (name, fn) => {handlers[name] = fn;}};
  const vscode = {getState: () => state, setState: value => {state = structuredClone(value);},
    postMessage() {throw Error('layout must not invoke Runtime');}};
  const view = createTranscriptViewState({document, window, vscode, requestAnimationFrame: fn => frames.push(fn), sessionId: () => session});
  return {view, document, window, elements, handlers, state: () => state,
    select: id => {session = id;}, flush: () => {while (frames.length) frames.shift()();}};
}

test('expanded child survives updates, completion and webview reload without moving the reading anchor', () => {
  const b = browser();
  b.view.restore(); b.flush();
  b.window.scrollY = 300;
  b.elements[0].open = true;
  b.handlers.toggle({target: {tagName: 'DETAILS'}});
  assert.deepEqual(b.state().sessions.one.open, ['child-one:r']);
  // 新内容插入读者当前行之前；DOM 被重新生成，details 初始关闭。
  b.elements[0].contentTop += 250;
  b.document.body.scrollHeight += 250;
  b.elements[0].open = false;
  b.view.restore(); b.flush();
  assert.ok(b.elements[0].open);
  assert.equal(b.window.scrollY, 550);
  assert.equal(b.elements[0].getBoundingClientRect().top, 100);
  b.handlers.scroll();
  const reopened = browser(b.state());
  reopened.elements[0].contentTop = 650;
  reopened.document.body.scrollHeight = 2250;
  reopened.view.restore(); reopened.flush();
  assert.ok(reopened.elements[0].open);
  assert.equal(reopened.window.scrollY, 550);
});

test('switching sessions retains independent disclosure and position choices', () => {
  const b = browser();
  b.view.restore(); b.flush();
  b.window.scrollY = 300; b.elements[0].open = true; b.view.capture();
  b.select('two'); b.elements[0].dataset = {disclosure: 'child-two:r', scrollAnchor: 'child-two:r'};
  b.view.restore(); b.flush();
  assert.equal(b.elements[0].open, false);
  b.window.scrollY = 250; b.elements[0].open = true; b.view.capture();
  b.select('one'); b.elements[0].dataset = {disclosure: 'child-one:r', scrollAnchor: 'child-one:r'};
  b.view.restore(); b.flush();
  assert.ok(b.elements[0].open);
  assert.equal(b.window.scrollY, 300);
});

test('only layout is restored, with bottom following and missing-anchor fallback', () => {
  const b = browser({layoutVersion: 1, pendingApproval: {allow: true}, canRun: true, sessions: {
    one: {open: [], scrollY: 200, followBottom: false, anchor: {key: 'missing', offset: 12}},
  }});
  b.view.restore(); b.flush();
  assert.equal(b.window.scrollY, 200);
  b.handlers.scroll();
  assert.deepEqual(Object.keys(b.state()).sort(), ['layoutVersion', 'sessions']);
  b.window.scrollY = 1400; b.view.capture();
  b.document.body.scrollHeight = 2400;
  b.view.restore(); b.flush();
  assert.equal(b.window.scrollY, 1800);
});
