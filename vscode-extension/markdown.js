"use strict";

// 由扩展内置的同一 Markdown 发行包驱动，不请求 CDN 或执行回复中的 HTML。
function createMarkdownRenderer(MarkdownIt, copyButton) {
  const md = new MarkdownIt({html: false, linkify: false, breaks: false});
  const escape = md.utils.escapeHtml;
  md.validateLink = href => /^(?:https?:|mailto:|#)/i.test(href);
  const linkOpen = md.renderer.rules.link_open || ((tokens, index, options, env, self) => self.renderToken(tokens, index, options));
  md.renderer.rules.link_open = (tokens, index, options, env, self) => {
    tokens[index].attrSet('rel', 'noopener noreferrer');
    return linkOpen(tokens, index, options, env, self);
  };
  // 不加载模型引用的外部图片，避免额外网络请求；保留其说明文字。
  md.renderer.rules.image = (tokens, index) => '<span class="markdown-image">' + escape(tokens[index].content || '图片') + '</span>';
  const codeBlock = (tokens, index) => {
    const token = tokens[index];
    const language = (token.info || '').trim().split(/\s+/)[0];
    return '<div class="code-block"><div class="code-toolbar"><span>' + escape(language) + '</span>'
      + copyButton(token.content, '复制代码', 'code-copy') + '</div><pre><code>'
      + escape(token.content) + '</code></pre></div>\n';
  };
  md.renderer.rules.fence = codeBlock;
  md.renderer.rules.code_block = codeBlock;
  md.renderer.rules.table_open = () => '<div class="markdown-table"><table>\n';
  md.renderer.rules.table_close = () => '</table></div>\n';
  return text => md.render(String(text ?? ''));
}

function renderAgentMessageHtml(text, key, esc, markdown, copyButton, className = '') {
  const body = markdown(text);
  const action = copyButton(text, '复制回复');
  return '<div class="message-shell agent-message' + (className ? ' ' + esc(className) : '') + '" data-scroll-anchor="' + esc(key) + '">'
    + '<div class="agent-markdown">' + body + '</div>'
    + (action ? '<div class="message-actions">' + action + '</div>' : '') + '</div>';
}

module.exports = {createMarkdownRenderer, renderAgentMessageHtml};
