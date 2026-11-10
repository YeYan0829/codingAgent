"use strict";

const test = require('node:test');
const assert = require('node:assert/strict');
const MarkdownIt = require('../vendor/markdown-it-15.0.3.min');
const {createMarkdownRenderer} = require('../markdown');

test('agent Markdown handles paragraphs, nested lists, emphasis, quotes, tables and links', () => {
  const markdown = createMarkdownRenderer(MarkdownIt, () => '');
  const result = markdown('# 调查结果\n\n区别在于：**主任务是否等待**。\n\n- 前台\n  - 等待结果\n- 后台 *并行*\n\n1. 审查\n2. 交付\n\n> 只读调查\n\n| 类型 | 权限 |\n| --- | --- |\n| 子任务 | `read_file` |\n\n[文档](https://example.com/docs?a=1&b=2)');
  assert.match(result, /<h1>调查结果<\/h1>/);
  assert.match(result, /<strong>主任务是否等待<\/strong>/);
  assert.match(result, /<ul>[\s\S]*<li>前台\n<ul>[\s\S]*等待结果/);
  assert.match(result, /<em>并行<\/em>/);
  assert.match(result, /<ol>[\s\S]*<li>审查<\/li>/);
  assert.match(result, /<blockquote>\n<p>只读调查<\/p>/);
  assert.match(result, /class="markdown-table"><table>[\s\S]*<code>read_file<\/code>/);
  assert.match(result, /href="https:\/\/example.com\/docs\?a=1&amp;b=2" rel="noopener noreferrer"/);
});

test('fenced and indented code copy the exact code without rendering its Markdown or HTML', () => {
  const copies = [];
  const markdown = createMarkdownRenderer(MarkdownIt, (text, label, className) => {
    copies.push({text, label, className}); return '<button>复制</button>';
  });
  const code = 'def 示例():\n    return "<b>**原样**</b>"\n';
  const result = markdown('```python\n' + code + '```\n\n    echo "原样"\n');
  assert.match(result, /class="code-toolbar"><span>python<\/span>/);
  assert.match(result, /&lt;b&gt;\*\*原样\*\*&lt;\/b&gt;/);
  assert.doesNotMatch(result, /<b>|<strong>/);
  assert.deepEqual(copies, [
    {text: code, label: '复制代码', className: 'code-copy'},
    {text: 'echo "原样"\n', label: '复制代码', className: 'code-copy'},
  ]);
  assert.match(markdown('```js\nconst n = 1;'), /<pre><code>const n = 1;<\/code>/);
});

test('model HTML and privileged links stay inert and remote images are never loaded', () => {
  const markdown = createMarkdownRenderer(MarkdownIt, () => '');
  const result = markdown('<script>alert(1)</script>\n\n<img src="x" onerror="alert(1)">\n\n'
    + '[命令](command:workbench.action.reloadWindow) [脚本](javascript:alert%281%29) '
    + '[文件](file:///etc/passwd) [数据](data:text/html,test)\n\n![示意图](https://example.com/track.png)\n\n'
    + '[安全链接](https://example.com)');
  assert.doesNotMatch(result, /<script|<img|href="(?:command|javascript|file|data):/);
  assert.match(result, /&lt;script&gt;/);
  assert.match(result, /<span class="markdown-image">示意图<\/span>/);
  assert.match(result, /href="https:\/\/example.com"/);
});
