const assert = require('node:assert/strict');
const { test } = require('node:test');
const { JSDOM, VirtualConsole } = require('jsdom');
const { loadPanel } = require('./helpers/chatPanelHarness.cjs');

const html = loadPanel().panel.getHtmlForWebview({});

function webview(t) {
  const errors = [], reads = [], sent = [];
  const dom = new JSDOM(html, {
    runScripts: 'dangerously', pretendToBeVisual: true,
    virtualConsole: new VirtualConsole().on('jsdomError', error => errors.push(error)),
    beforeParse(window) {
      window.acquireVsCodeApi = () => ({ postMessage: message => sent.push(message), getState: () => ({}), setState() {} });
      window.HTMLElement.prototype.scrollIntoView = function() {};
      const FileReader = window.FileReader;
      window.FileReader = class extends FileReader {
        readAsDataURL(file) {
          reads.push(new Promise(resolve => this.addEventListener('loadend', resolve, { once: true })));
          super.readAsDataURL(file);
        }
        readAsText(file) {
          reads.push(new Promise(resolve => this.addEventListener('loadend', resolve, { once: true })));
          super.readAsText(file);
        }
      };
    },
  });
  t.after(() => {
    dom.window.close();
    assert.deepEqual(errors, []);
  });
  const doc = dom.window.document;
  const input = doc.getElementById('input');
  function paste(items, files, text = '') {
    const event = new dom.window.Event('paste', { bubbles: true, cancelable: true });
    Object.defineProperty(event, 'clipboardData', { value: { items, files, getData: () => text } });
    input.dispatchEvent(event);
    return event;
  }
  function select(node) {
    const range = doc.createRange();
    range.selectNodeContents(node);
    dom.window.getSelection().removeAllRanges();
    dom.window.getSelection().addRange(range);
  }
  return { window: dom.window, doc, input, reads, sent, paste, select };
}

for (const nullItem of [false, true]) {
  test(`clipboard files attach images and text when items are unavailable (null item: ${nullItem})`, async t => {
    const view = webview(t);
    const image = new view.window.File(['image'], 'clipboard.png', { type: 'image/png' });
    const file = new view.window.File(['notes'], 'notes.txt', { type: 'text/plain' });
    const items = nullItem ? [{ kind: 'file', type: image.type, getAsFile: () => null }] : [];

    const event = view.paste(items, [image, file]);
    await Promise.all(view.reads);

    assert.equal(event.defaultPrevented, true);
    assert.equal(view.doc.querySelectorAll('.attachment-thumb').length, 1);
    assert.equal(view.doc.querySelectorAll('.text-file-chip').length, 1);
    view.doc.getElementById('send-btn').click();
    const message = view.sent.find(message => message.type === 'sendMessage');
    assert.equal(message.images.length, 1);
    assert.equal(message.images[0].data, Buffer.from('image').toString('base64'));
    assert.ok(message.text.includes('notes'));
  });
}

test('each paste attaches separate images once even when both APIs expose identical metadata', async t => {
  const view = webview(t);
  const options = { type: 'image/png', lastModified: 1 };
  const images = ['first', 'other'].map(data => new view.window.File([data], 'image.png', options));
  const items = images.map(image => ({ kind: 'file', type: image.type, getAsFile: () => image }));
  const files = images.map(image => new view.window.File([image], image.name, options));

  for (let i = 0; i < 2; i++) {
    view.paste(items, files);
    await Promise.all(view.reads);
  }

  assert.equal(view.doc.querySelectorAll('.attachment-thumb').length, 4);
  view.doc.getElementById('send-btn').click();
  const message = view.sent.find(message => message.type === 'sendMessage');
  assert.deepEqual(Array.from(message.images, image => image.data), ['first', 'other', 'first', 'other'].map(data => Buffer.from(data).toString('base64')));
});

test('empty clipboard content preserves the selected draft', t => {
  const view = webview(t);
  view.input.textContent = 'keep this draft';
  view.select(view.input);

  view.paste([], []);

  assert.equal(view.input.textContent, 'keep this draft');
});

test('plain text paste replaces the selected draft without interpreting HTML', t => {
  const view = webview(t);
  view.input.textContent = 'replace this';
  view.select(view.input);

  const event = view.paste([], [], '<b>plain text</b>\nnext line');

  assert.equal(event.defaultPrevented, true);
  assert.equal(view.input.textContent, '<b>plain text</b>\nnext line');
  assert.equal(view.input.querySelector('b'), null);
});

test('paste does not modify a selection outside the composer or crossing its boundary', t => {
  const view = webview(t);
  const outside = view.doc.createElement('p');
  outside.textContent = 'keep this message';
  view.input.after(outside);
  view.select(outside);

  view.paste([], [], 'pasted text');

  assert.equal(outside.textContent, 'keep this message');
  view.input.textContent = 'keep this draft';
  const range = view.doc.createRange();
  range.setStart(view.input.firstChild, 0);
  range.setEnd(outside.firstChild, outside.textContent.length);
  view.window.getSelection().removeAllRanges();
  view.window.getSelection().addRange(range);

  view.paste([], [], 'pasted text');

  assert.equal(view.input.textContent, 'keep this draft');
  assert.equal(outside.textContent, 'keep this message');
});
