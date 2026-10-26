/** Observe visible chat metadata through an already enabled local CDP port. */
import { readFile, writeFile, rename, unlink, mkdir, stat } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import process from 'node:process';
import { pathToFileURL } from 'node:url';
import { setTimeout as sleep } from 'node:timers/promises';

const POLL_MS = 650;
const TIMEOUT_MS = 1500;
// Keep this function self-contained: its source runs read-only inside the app page.
export function collectSnapshot(doc) {
  const snapshot = {
    focused: doc.hasFocus(), visible: doc.visibilityState === 'visible',
    chatVisible: false, conversationId: null, selected: [],
  };
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const surfaceSelector = 'main[data-app-shell-main-surface="default"]';
  const pageSelector = '[data-app-shell-active-page]';
  const inactiveSelector = '[data-app-shell-active-page="false"]';
  const shown = (node, requireBox = false) => {
    if (!node || node.closest(inactiveSelector)) return false;
    for (let element = node; element; element = element.parentElement) {
      if (element.hasAttribute('hidden') || element.hasAttribute('inert') ||
          element.getAttribute('aria-hidden') === 'true') return false;
      const style = doc.defaultView.getComputedStyle(element);
      if (style.display === 'none' || style.visibility === 'hidden' ||
          style.visibility === 'collapse' || style.opacity === '0') return false;
    }
    return !requireBox || Array.from(node.getClientRects()).some(rect => rect.width > 0 && rect.height > 0);
  };
  // A modal covers the chat even when its retained DOM still has a selected row.
  const dialogs = doc.querySelectorAll('dialog[open], [role="dialog"], [role="alertdialog"]');
  if (Array.from(dialogs).some(dialog => shown(dialog, true))) return snapshot;

  const conversations = Array.from(doc.querySelectorAll('[data-thread-find-target="conversation"]'));
  const portals = Array.from(doc.querySelectorAll('[data-above-composer-portal][data-above-composer-conversation-id]'));
  const ids = new Set();
  for (const conversation of conversations) {
    const surface = conversation.closest(surfaceSelector);
    const page = conversation.closest(pageSelector);
    if (!surface || page?.getAttribute('data-app-shell-active-page') !== 'true' ||
        !shown(conversation, true)) continue;
    // Timeline and composer are sibling branches, not descendants of one another.
    for (const portal of portals) {
      if (portal.closest(surfaceSelector) !== surface || portal.closest(pageSelector) !== page ||
          portal.closest(inactiveSelector) || !shown(portal.parentElement)) continue;
      // The empty portal itself is display:none; only its containing view must be shown.
      ids.add(portal.getAttribute('data-above-composer-conversation-id'));
    }
  }
  if (ids.size !== 1) return snapshot;
  const id = ids.values().next().value;
  if (typeof id !== 'string' || !uuid.test(id)) return snapshot;
  snapshot.chatVisible = true;
  snapshot.conversationId = id;
  // Sidebar metadata is optional and is used only for a matching chat's title.
  snapshot.selected = Array.from(doc.querySelectorAll('[data-app-action-sidebar-thread-selected="true"]'))
    .filter(row => !row.closest(inactiveSelector))
    .map(row => ({
      id: row.getAttribute('data-app-action-sidebar-thread-id'),
      hostId: row.getAttribute('data-app-action-sidebar-thread-host-id'),
      kind: row.getAttribute('data-app-action-sidebar-thread-kind'),
      title: row.getAttribute('data-app-action-sidebar-thread-title') || '',
    }));
  return snapshot;
}

export const SNAPSHOT_EXPRESSION = `(${collectSnapshot.toString()})(document)`;

export function resolveThread(selected, bindings) {
  if (!selected || selected.hostId !== 'local' || selected.kind !== 'local') return null;
  if (typeof selected.id !== 'string' || !selected.id.startsWith('local:')) return null;
  const clientId = selected.id.slice('local:'.length);
  const bound = bindings?.[clientId];
  const candidate = typeof bound === 'string' ? bound : clientId;
  // IDs containing a client prefix must be explicitly mapped. Never choose by title/recency.
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(candidate)
    ? candidate : null;
}

export function selectView(snapshots, bindings, observedAt = new Date().toISOString()) {
  const valid = snapshots.filter(s => s && typeof s.focused === 'boolean' && typeof s.visible === 'boolean');
  const base = { thread_id: null, title: '', observed_at: observedAt, available: valid.length > 0 };
  const focused = valid.filter(s => s.focused && s.visible);
  if (focused.length !== 1) return base;
  const current = focused[0];
  const id = current.conversationId;
  if (current.chatVisible !== true || typeof id !== 'string' ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id)) return base;
  const titles = new Set((Array.isArray(current.selected) ? current.selected : [])
    .filter(row => resolveThread(row, bindings) === id && typeof row.title === 'string' && row.title)
    .map(row => row.title.slice(0, 500)));
  return { ...base, thread_id: id, title: titles.size === 1 ? titles.values().next().value : '' };
}

export function isMainTarget(target) {
  if (target?.type !== 'page' || typeof target.url !== 'string') return false;
  try {
    const url = new URL(target.url);
    return url.protocol === 'app:' && url.hostname === '-' && url.pathname === '/index.html';
  } catch { return false; }
}

export function safeSocketUrl(value, port) {
  try {
    const url = new URL(value);
    return url.protocol === 'ws:' && ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)
      && url.port === String(port) && !url.username && !url.password;
  } catch { return false; }
}

class CdpPage {
  constructor(url) {
    this.url = url;
    this.socket = null;
    this.sequence = 0;
    this.pending = new Map();
    this.connecting = null;
  }

  async connect() {
    if (this.socket?.readyState === WebSocket.OPEN) return;
    if (this.connecting) return this.connecting;
    this.connecting = new Promise((resolve, reject) => {
      const socket = this.socket = new WebSocket(this.url);
      const timeout = setTimeout(() => { reject(new Error('CDP connection timeout')); this.close(); }, TIMEOUT_MS);
      socket.addEventListener('open', () => { clearTimeout(timeout); resolve(); }, { once: true });
      socket.addEventListener('error', () => { clearTimeout(timeout); reject(new Error('CDP connection error')); this.close(); }, { once: true });
      socket.addEventListener('close', () => {
        clearTimeout(timeout);
        reject(new Error('CDP connection closed'));
        for (const pending of this.pending.values()) {
          clearTimeout(pending.timeout);
          pending.reject(new Error('CDP connection closed'));
        }
        this.pending.clear();
      });
      socket.addEventListener('message', event => {
        let message;
        try { message = JSON.parse(event.data); } catch { return; }
        const pending = this.pending.get(message.id);
        if (!pending) return;
        this.pending.delete(message.id);
        clearTimeout(pending.timeout);
        if (message.error) pending.reject(new Error('CDP read failed'));
        else pending.resolve(message.result);
      });
    }).finally(() => { this.connecting = null; });
    return this.connecting;
  }

  async snapshot() {
    await this.connect();
    const id = ++this.sequence;
    const response = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error('CDP read timeout'));
        this.close();
      }, TIMEOUT_MS);
      this.pending.set(id, { resolve, reject, timeout });
      this.socket.send(JSON.stringify({ id, method: 'Runtime.evaluate', params: {
        expression: SNAPSHOT_EXPRESSION, returnByValue: true, silent: true,
        timeout: TIMEOUT_MS, disableBreaks: true,
      } }));
    });
    if (response?.exceptionDetails) throw new Error('Chat view read unavailable');
    return response?.result?.value ?? null;
  }

  close() {
    const socket = this.socket;
    this.socket = null;
    if (socket && socket.readyState !== WebSocket.CLOSED) socket.close();
  }
}

export class BindingReader {
  constructor(codexHome) {
    this.filename = path.join(codexHome, '.codex-global-state.json');
    this.signature = null;
    this.bindings = {};
  }
  async read() {
    try {
      const info = await stat(this.filename);
      const signature = `${info.mtimeMs}:${info.size}`;
      if (signature !== this.signature) {
        const state = JSON.parse(await readFile(this.filename, 'utf8'));
        const value = state?.['electron-persisted-atom-state']?.['client-thread-bindings-v1'];
        this.bindings = value && typeof value === 'object' && !Array.isArray(value) ? value : {};
        this.signature = signature;
      }
      return this.bindings;
    } catch {
      // A replace in progress must not map a new view using an uncertain binding.
      return {};
    }
  }
}

async function publish(dataDir, view) {
  const filename = path.join(dataDir, 'active-view.json');
  const temporary = path.join(dataDir, `.active-view-${process.pid}.tmp`);
  try {
    await writeFile(temporary, JSON.stringify(view), 'utf8');
    for (let attempt = 0; ; attempt++) {
      try { await rename(temporary, filename); break; }
      catch (error) {
        if (attempt >= 3 || !['EPERM', 'EACCES', 'EBUSY'].includes(error.code)) throw error;
        await sleep(20);
      }
    }
  } finally { await unlink(temporary).catch(() => {}); }
}

function parentAlive(pid) {
  if (pid == null) return true;
  try { process.kill(pid, 0); return true; } catch { return false; }
}

export async function observe({ dataDir, codexHome, port = 9229, parentPid = null, signal }) {
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid local CDP port');
  await mkdir(dataDir, { recursive: true });
  const bindings = new BindingReader(codexHome);
  const pages = new Map();
  let stopped = false;
  let nextDiscovery = 0;
  const stop = () => { stopped = true; for (const page of pages.values()) page.close(); };
  signal?.addEventListener('abort', stop, { once: true });
  const unavailable = () => ({ thread_id: null, title: '', observed_at: new Date().toISOString(), available: false });
  try {
    await publish(dataDir, unavailable());
    while (!stopped && !signal?.aborted && parentAlive(parentPid)) {
      let view;
      try {
        if (Date.now() >= nextDiscovery) {
          const response = await fetch(`http://127.0.0.1:${port}/json/list`, {
            signal: AbortSignal.timeout(TIMEOUT_MS), redirect: 'error',
          });
          if (!response.ok) throw new Error('Local CDP unavailable');
          const all = await response.json();
          if (!Array.isArray(all)) throw new Error('Invalid local target list');
          const mainTargets = all.filter(isMainTarget).filter(t => safeSocketUrl(t.webSocketDebuggerUrl, port));
          if (mainTargets.length > 8) throw new Error('Ambiguous app windows');
          const wanted = new Set(mainTargets.map(t => t.webSocketDebuggerUrl));
          for (const [url, page] of pages) if (!wanted.has(url)) { page.close(); pages.delete(url); }
          for (const url of wanted) if (!pages.has(url)) pages.set(url, new CdpPage(url));
          nextDiscovery = Date.now() + 2000;
        }
        const [snapshots, mapping] = await Promise.all([
          Promise.all([...pages.values()].map(page => page.snapshot().catch(() => null))), bindings.read(),
        ]);
        view = selectView(snapshots, mapping);
      } catch {
        for (const page of pages.values()) page.close();
        pages.clear();
        nextDiscovery = Date.now() + 1000;
        view = unavailable();
      }
      if (stopped || !parentAlive(parentPid)) break;
      await publish(dataDir, view);
      await sleep(POLL_MS, undefined, { signal }).catch(() => {});
    }
  } finally {
    stop();
    signal?.removeEventListener('abort', stop);
    await publish(dataDir, unavailable()).catch(() => {});
  }
}

async function main() {
  const args = process.argv.slice(2);
  const options = { dataDir: null, codexHome: process.env.CODEX_HOME || path.join(os.homedir(), '.codex'),
    port: Number(process.env.CODEX_PLAN_PROGRESS_CDP_PORT || 9229), parentPid: null };
  for (let index = 0; index < args.length; index++) {
    const key = args[index];
    if (key === '--data-dir') options.dataDir = args[++index];
    else if (key === '--codex-home') options.codexHome = args[++index];
    else if (key === '--parent-pid') options.parentPid = Number(args[++index]);
    else throw new Error(`Unknown argument: ${key}`);
  }
  if (!options.dataDir || !options.codexHome || options.parentPid !== null &&
      (!Number.isInteger(options.parentPid) || options.parentPid <= 0)) throw new Error('Invalid observer arguments');
  const controller = new AbortController();
  process.once('SIGINT', () => controller.abort());
  process.once('SIGTERM', () => controller.abort());
  await observe({ ...options, signal: controller.signal });
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch(error => { console.error(`Plan progress view observer: ${error.message}`); process.exitCode = 1; });
}
