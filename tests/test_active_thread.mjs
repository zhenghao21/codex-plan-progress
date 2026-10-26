import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import vm from 'node:vm';
import { collectSnapshot, SNAPSHOT_EXPRESSION, resolveThread, selectView, isMainTarget, safeSocketUrl, BindingReader } from '../plugin/scripts/active_thread.mjs';

const id = '00000000-0000-4000-8000-000000000001';
const client = 'client-new-thread:00000000-0000-4000-8000-000000000002';
const selected = { id: `local:${client}`, hostId: 'local', kind: 'local', title: '示例会话' };
const mapping = { [client]: id };
const secondId = '00000000-0000-4000-8000-000000000003';
const focused = { focused: true, visible: true, chatVisible: true, conversationId: id, selected: [selected] };

class Element {
  constructor(tag = 'div', attrs = {}, style = {}, box = { width: 600, height: 300 }) {
    this.tag = tag;
    this.attrs = attrs;
    this.style = { display: 'block', visibility: 'visible', opacity: '1', ...style };
    this.box = box;
    this.children = [];
    this.parentElement = null;
  }
  get textContent() { throw new Error('Chat text must never be read'); }
  getAttribute(key) { return this.attrs[key] ?? null; }
  hasAttribute(key) { return Object.hasOwn(this.attrs, key); }
  getClientRects() { return this.box ? [this.box] : []; }
  append(...children) {
    for (const child of children) { child.parentElement = this; this.children.push(child); }
    return this;
  }
  matches(selector) {
    const tag = selector.match(/^[a-z]+/)?.[0];
    if (tag && tag !== this.tag) return false;
    return [...selector.matchAll(/\[([^=\]]+)(?:="([^"]*)")?\]/g)]
      .every(([, key, value]) => value === undefined ? this.hasAttribute(key) : this.getAttribute(key) === value);
  }
  closest(selector) {
    for (let node = this; node; node = node.parentElement) if (node.matches(selector)) return node;
    return null;
  }
}

function documentFor(...children) {
  const root = new Element('body').append(...children);
  return {
    root, hasFocus: () => true, visibilityState: 'visible',
    defaultView: { getComputedStyle: element => element.style },
    querySelectorAll(selector) {
      const selectors = selector.split(',').map(s => s.trim());
      const found = [];
      const visit = node => {
        if (selectors.some(s => node.matches(s))) found.push(node);
        node.children.forEach(visit);
      };
      root.children.forEach(visit);
      return found;
    },
  };
}

function chatPage(conversationId = id, active = 'true') {
  const page = new Element('div', { 'data-app-shell-active-page': active }, { display: 'contents' });
  const surface = new Element('main', { 'data-app-shell-main-surface': 'default' });
  const conversation = new Element('div', { 'data-thread-find-target': 'conversation' });
  const footer = new Element('div');
  const portal = new Element('div', {
    'data-above-composer-portal': 'true', 'data-above-composer-conversation-id': conversationId,
  }, { display: 'none' }, null);
  footer.append(portal);
  surface.append(conversation, footer);
  page.append(surface);
  return { page, surface, conversation, footer, portal };
}

function sidebarRow(row = selected) {
  return new Element('div', {
    'data-app-action-sidebar-thread-selected': 'true',
    'data-app-action-sidebar-thread-id': row.id,
    'data-app-action-sidebar-thread-host-id': row.hostId,
    'data-app-action-sidebar-thread-kind': row.kind,
    'data-app-action-sidebar-thread-title': row.title,
  });
}

test('DOM gate pairs sibling timeline and empty portal without reading chat text', () => {
  const { page, conversation, portal } = chatPage();
  const doc = documentFor(page, sidebarRow());
  assert.equal(conversation.children.includes(portal), false);
  const snapshot = collectSnapshot(doc);
  assert.equal(snapshot.chatVisible, true);
  assert.equal(snapshot.conversationId, id);
  assert.equal(selectView([snapshot], mapping).thread_id, id);
  // The exact CDP expression must remain self-contained, including its DOM gate.
  const serialized = vm.runInNewContext(SNAPSHOT_EXPRESSION, { document: doc });
  assert.equal(serialized.conversationId, id);
});

test('DOM gate works with sidebar removed or hidden', () => {
  const { page } = chatPage();
  assert.equal(collectSnapshot(documentFor(page)).conversationId, id);
  const hiddenSidebar = new Element('aside', { hidden: '' }).append(sidebarRow());
  const snapshot = collectSnapshot(documentFor(page, hiddenSidebar));
  assert.equal(selectView([snapshot], mapping).thread_id, id);
});

test('DOM gate hides extensions, plugins, settings and home despite retained sidebar selection', () => {
  for (const destination of ['extensions', 'plugins', 'settings', 'home']) {
    const old = chatPage(id, 'false');
    const page = new Element('div', { 'data-app-shell-active-page': 'true', 'data-test-destination': destination })
      .append(new Element('main', { 'data-app-shell-main-surface': 'default' }));
    const snapshot = collectSnapshot(documentFor(old.page, page, sidebarRow()));
    assert.equal(snapshot.chatVisible, false, destination);
    assert.equal(selectView([snapshot], mapping).thread_id, null, destination);
  }
});

test('DOM gate rejects missing, inactive or hidden chat containers', () => {
  for (const scenario of ['no-page-marker', 'inactive', 'hidden', 'inert', 'aria-hidden', 'display-none', 'zero-size']) {
    const chat = chatPage();
    if (scenario === 'no-page-marker') delete chat.page.attrs['data-app-shell-active-page'];
    if (scenario === 'inactive') chat.page.attrs['data-app-shell-active-page'] = 'false';
    if (scenario === 'hidden' || scenario === 'inert') chat.surface.attrs[scenario] = '';
    if (scenario === 'aria-hidden') chat.surface.attrs['aria-hidden'] = 'true';
    if (scenario === 'display-none') chat.surface.style.display = 'none';
    if (scenario === 'zero-size') chat.conversation.box = { width: 600, height: 0 };
    assert.equal(collectSnapshot(documentFor(chat.page)).conversationId, null, scenario);
  }
});

test('DOM gate requires portal and timeline in the same active page and main surface', () => {
  const chat = chatPage();
  chat.surface.children = [chat.conversation];
  const otherSurface = new Element('main', { 'data-app-shell-main-surface': 'default' }).append(chat.footer);
  chat.page.append(otherSurface);
  assert.equal(collectSnapshot(documentFor(chat.page)).conversationId, null);
  chat.page.children = [chat.surface];
  const otherPage = new Element('div', { 'data-app-shell-active-page': 'true' }).append(otherSurface);
  assert.equal(collectSnapshot(documentFor(chat.page, otherPage)).conversationId, null);
});

test('DOM gate deduplicates one UUID and rejects genuinely different or nonlocal IDs', () => {
  const chat = chatPage();
  const duplicate = new Element('div', { ...chat.portal.attrs }, { display: 'none' }, null);
  chat.footer.append(duplicate);
  assert.equal(collectSnapshot(documentFor(chat.page)).conversationId, id);
  duplicate.attrs['data-above-composer-conversation-id'] = secondId;
  assert.equal(collectSnapshot(documentFor(chat.page)).conversationId, null);
  for (const invalid of [`chatgpt:${id}`, client, '', null]) {
    const invalidChat = chatPage(invalid);
    assert.equal(collectSnapshot(documentFor(invalidChat.page)).conversationId, null);
  }
});

test('DOM gate ignores hidden and retained portals while visible modal dialogs hide the chat', () => {
  const chat = chatPage();
  const hidden = new Element('div', { hidden: '' }).append(new Element('div', {
    'data-above-composer-portal': 'true', 'data-above-composer-conversation-id': secondId,
  }));
  chat.surface.append(hidden);
  assert.equal(collectSnapshot(documentFor(chat.page, chatPage(secondId, 'false').page)).conversationId, id);
  const dialog = new Element('div', { role: 'dialog', 'aria-modal': 'true' });
  assert.equal(collectSnapshot(documentFor(chat.page, dialog)).conversationId, null);
  dialog.style.display = 'none';
  assert.equal(collectSnapshot(documentFor(chat.page, dialog)).conversationId, id);
});

test('uses exact composer ID and only matching sidebar title metadata', () => {
  assert.equal(resolveThread(selected, mapping), id);
  const view = selectView([focused], mapping);
  assert.equal(view.thread_id, id);
  assert.equal(view.title, selected.title);
  assert.equal(view.available, true);
});

test('sidebar absence or unmapped metadata never overrides composer identity', () => {
  assert.equal(selectView([{ ...focused, selected: [] }], mapping).thread_id, id);
  assert.equal(selectView([focused], {}).thread_id, id);
  assert.equal(selectView([focused], {}).title, '');
  assert.equal(selectView([{ ...focused, conversationId: secondId }], mapping).title, '');
  assert.equal(resolveThread({ ...selected, hostId: 'remote-1' }, mapping), null);
  assert.equal(resolveThread({ ...selected, kind: 'remote' }, mapping), null);
  assert.equal(resolveThread({ ...selected, id: `local:${id}` }, {}), id);
});

test('switching selects only the focused visible page and ambiguity hides', () => {
  const other = { ...focused, conversationId: secondId, selected: [{ ...selected, id: `local:${secondId}` }] };
  assert.equal(selectView([{ ...focused, focused: false }, other], mapping).thread_id, secondId);
  assert.equal(selectView([focused, other], mapping).thread_id, null);
  assert.equal(selectView([{ ...focused, visible: false }], mapping).thread_id, null);
  assert.equal(selectView([], mapping).available, false);
});

test('duplicate sidebar placements are harmless and old snapshot schemas hide', () => {
  assert.equal(selectView([{ ...focused, selected: [selected, selected] }], mapping).thread_id, id);
  assert.equal(selectView([{ ...focused, selected: [selected, { ...selected, id: `local:${id}` }] }], mapping).thread_id, id);
  for (const chatVisible of [undefined, false, 'true']) {
    assert.equal(selectView([{ ...focused, chatVisible }], mapping).thread_id, null);
  }
  for (const conversationId of [undefined, null, client, `chatgpt:${id}`]) {
    assert.equal(selectView([{ ...focused, conversationId }], mapping).thread_id, null);
  }
});

test('only exact Codex main pages and loopback port are eligible', () => {
  assert.equal(isMainTarget({ type: 'page', url: 'app://-/index.html' }), true);
  for (const url of ['app://-/avatar-overlay.html', 'https://chatgpt.com/', 'app://other/index.html']) {
    assert.equal(isMainTarget({ type: 'page', url }), false);
  }
  assert.equal(safeSocketUrl('ws://127.0.0.1:9229/devtools/page/abc', 9229), true);
  assert.equal(safeSocketUrl('ws://example.com:9229/devtools/page/abc', 9229), false);
  assert.equal(safeSocketUrl('ws://127.0.0.1:9000/devtools/page/abc', 9229), false);
});

test('reads only the explicit client binding map, fails closed on damaged state', async () => {
  const folder = await mkdtemp(path.join(os.tmpdir(), 'codex-view-bindings-'));
  try {
    const reader = new BindingReader(folder);
    assert.deepEqual(await reader.read(), {});
    await writeFile(path.join(folder, '.codex-global-state.json'), JSON.stringify({
      'electron-persisted-atom-state': { 'client-thread-bindings-v1': mapping },
    }));
    assert.deepEqual(await reader.read(), mapping);
    await writeFile(path.join(folder, '.codex-global-state.json'), '{');
    assert.deepEqual(await reader.read(), {});
  } finally { await rm(folder, { recursive: true, force: true }); }
});
