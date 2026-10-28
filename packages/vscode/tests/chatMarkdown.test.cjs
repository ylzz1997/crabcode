const assert = require('node:assert/strict');
const { test } = require('node:test');
const { JSDOM, VirtualConsole } = require('jsdom');
const { loadPanel } = require('./helpers/chatPanelHarness.cjs');

function webview() {
  const h = loadPanel();
  const errors = [];
  const posted = [];
  const dom = new JSDOM(h.panel.getHtmlForWebview({}), {
    runScripts: 'dangerously', pretendToBeVisual: true,
    virtualConsole: new VirtualConsole().on('jsdomError', error => errors.push(error)),
    beforeParse(window) {
      window.acquireVsCodeApi = () => ({ postMessage: data => posted.push(data), getState: () => ({}), setState() {} });
      window.HTMLElement.prototype.scrollIntoView = function () {};
    },
  });
  const send = data => dom.window.dispatchEvent(new dom.window.MessageEvent('message', { data }));
  const show = text => send({ type: 'newMessage', message: { id: 'answer', role: 'assistant', text } });
  return { h, dom, doc: dom.window.document, send, show, errors, posted };
}

test('restored CJK emphasis matches Desktop and copy preserves the original Markdown', () => {
  const w = webview();
  try {
    const text = '自动执行**“销毁程序（析构）”**，一刀斩断。\n\n**中文句号。**后面继续。\n\nこれは**「強調」**です。\n\n이것은**강조(괄호)**입니다.';
    w.send({ type: 'history', items: [{ kind: 'message', message: { id: 'answer', role: 'assistant', text } }] });
    assert.deepEqual(Array.from(w.doc.querySelectorAll('#msg-answer .md-text strong'), node => node.textContent), [
      '“销毁程序（析构）”', '中文句号。', '「強調」', '강조(괄호)',
    ]);
    w.doc.querySelector('#msg-answer .message-actions [data-copy-text]').click();
    assert.equal(w.posted.find(item => item.type === 'copyText').text, text);
    assert.deepEqual(w.errors, []);
  } finally { w.dom.window.close(); }
});

test('streaming closes CJK bold and retains literal code, escaped markers and safe text', () => {
  const w = webview();
  try {
    w.show('自动执行**“销毁程序（析构）”');
    assert.equal(w.doc.querySelector('#msg-answer strong'), null);
    w.send({ type: 'appendText', id: 'answer', chunk: '**，继续。' });
    assert.equal(w.doc.querySelector('#msg-answer strong').textContent, '“销毁程序（析构）”');
    w.send({ type: 'appendText', id: 'answer', chunk: '\n\n`执行**“字面量”**`\n\n```text\n执行**“代码块”**\n```\n\n执行\\*\\*“转义”\\*\\*\n\na**"foo"**\n\n<script>window.injected = true</script>\n<img src=x onerror="window.injected = true">\n[bad](javascript:alert(1))' });
    assert.equal(w.doc.querySelectorAll('#msg-answer .assistant-markdown strong').length, 1);
    assert.deepEqual(Array.from(w.doc.querySelectorAll('#msg-answer code'), node => node.textContent), ['执行**“字面量”**', '执行**“代码块”**']);
    assert.ok(w.doc.querySelector('#msg-answer .text').textContent.includes('执行**“转义”**'));
    assert.ok(w.doc.querySelector('#msg-answer .text').textContent.includes('a**"foo"**'));
    assert.equal(w.doc.querySelector('#msg-answer script, #msg-answer img, #msg-answer a'), null);
    assert.equal(w.dom.window.injected, undefined);
    assert.deepEqual(w.errors, []);
  } finally { w.dom.window.close(); }
});

test('table cells render CJK emphasis while table and code copy controls keep raw content', () => {
  const w = webview();
  try {
    const table = '| **“标题”** | 值 |\n|---|---|\n| 执行**“析构”**后继续 | `**literal**` |';
    w.show(table + '\n\n```python\nprint("**literal**")\n```');
    assert.equal(w.doc.querySelector('#msg-answer th strong').textContent, '“标题”');
    assert.equal(w.doc.querySelector('#msg-answer td strong').textContent, '“析构”');
    assert.equal(w.doc.querySelector('#msg-answer td code').textContent, '**literal**');
    w.doc.querySelector('#msg-answer .message-table-shell [data-copy-text]').click();
    w.doc.querySelector('#msg-answer .message-code-shell [data-copy-text]').click();
    assert.deepEqual(w.posted.filter(item => item.type === 'copyText').map(item => item.text), [table, 'print("**literal**")']);
    assert.deepEqual(w.errors, []);
  } finally { w.dom.window.close(); }
});

test('the packaged Markdown script loads before the chat script under the existing nonce policy', () => {
  const { getRawWebviewHtml } = loadPanel();
  const html = getRawWebviewHtml({ asWebviewUri: () => 'https://webview.example/dist/chatMarkdown.js' });
  const document = new JSDOM(html).window.document;
  const scripts = document.querySelectorAll('script');
  assert.equal(scripts.length, 2);
  assert.equal(scripts[0].src, 'https://webview.example/dist/chatMarkdown.js');
  assert.equal(scripts[0].nonce, scripts[1].nonce);
  assert.ok(document.querySelector('meta[http-equiv="Content-Security-Policy"]').content.includes("script-src 'nonce-" + scripts[0].nonce + "'"));
});

test('restored headings, nested lists, quotes and GFM blocks render as structure rather than raw markers', () => {
  const w = webview();
  try {
    const title = '三、如果接下来直接动笔写正文，可以直接按以下 4 幕推进：';
    const text = '### ' + title + '\n\n3. **第一幕**\n   - 嵌套情节\n4. 第二幕\n\n> 引用\n>\n> - 引用内列表\n\n- [x] 完成\n- [ ] 待写\n\n~~旧版本~~ [参考](https://example.com/story)\n\n---\n\n另一标题\n===\n\n~~~text\n### 保留字面量\n~~~\n\n| 人物 | 内容 |\n|:---|---:|\n| 天麻 | a\\|b |';
    w.send({ type: 'history', items: [{ kind: 'message', message: { id: 'answer', role: 'assistant', text } }] });
    assert.equal(w.doc.querySelector('#msg-answer h3').textContent, title);
    assert.equal(w.doc.querySelector('#msg-answer h1').textContent, '另一标题');
    assert.equal(w.doc.querySelector('#msg-answer ol').start, 3);
    assert.equal(w.doc.querySelector('#msg-answer ol li ul li').textContent, '嵌套情节');
    assert.equal(w.doc.querySelector('#msg-answer blockquote ul').textContent, '引用内列表');
    assert.deepEqual(Array.from(w.doc.querySelectorAll('#msg-answer input[type=checkbox]'), n => [n.checked, n.disabled]), [[true, true], [false, true]]);
    assert.equal(w.doc.querySelector('#msg-answer del').textContent, '旧版本');
    assert.equal(w.doc.querySelector('#msg-answer a').href, 'https://example.com/story');
    assert.ok(w.doc.querySelector('#msg-answer hr'));
    assert.equal(w.doc.querySelector('#msg-answer pre code').textContent, '### 保留字面量');
    assert.equal(w.doc.querySelector('#msg-answer tbody td:last-child').textContent, 'a|b');
    assert.equal(w.doc.querySelector('#msg-answer tbody td:last-child').style.textAlign, 'right');
    assert.equal(w.doc.querySelector('#msg-answer .message-actions [data-copy-text]').dataset.copyText, text);
    assert.deepEqual(w.errors, []);
  } finally { w.dom.window.close(); }
});

test('streamed headings and inline images use the same block parser and image preview controls', () => {
  const w = webview();
  try {
    w.show('### 三、如果接下来');
    w.send({ type: 'appendText', id: 'answer', chunk: '直接动笔\n\n正文 ![场景](https://example.com/scene.png) 后文\n\n[引用][ref]\n\n[ref]: https://example.com/reference' });
    assert.equal(w.doc.querySelector('#msg-answer h3').textContent, '三、如果接下来直接动笔');
    const image = w.doc.querySelector('#msg-answer img');
    assert.equal(image.src, 'https://example.com/scene.png');
    assert.ok(image.hasAttribute('data-image-preview'));
    assert.equal(image.tabIndex, 0);
    assert.equal(w.doc.querySelector('#msg-answer a').href, 'https://example.com/reference');
    assert.deepEqual(w.errors, []);
  } finally { w.dom.window.close(); }
});
