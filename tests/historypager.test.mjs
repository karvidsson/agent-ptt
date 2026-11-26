// Paged transcripts (agent_ptt/static/historypager.js): pager state, gap fill, prepending
// pages, and the day dividers (datedivider.js) at the seam between pages.
process.env.TZ = 'Europe/Stockholm';
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

for (const file of ['datedivider.js', 'historypager.js']) {
  vm.runInThisContext(await readFile(new URL(`../agent_ptt/static/${file}`, import.meta.url), 'utf8'));
}
const H = globalThis.AgentPttHistory;
const {syncDividers} = globalThis.AgentPttDates;
const NOW = new Date('2026-11-13T15:00:00+01:00');

const msg = (id, ts = '2026-11-13T09:00:00Z') => ({message_id: id, timestamp: ts});
const ids = list => list.map(m => m.message_id);

test('historyUrl: the latest page, and an older page before a cursor', () => {
  assert.equal(H.historyUrl('http://h', 'c1'), 'http://h/channels/c1/history?limit=100');
  assert.equal(H.historyUrl('http://h', 'c1', {limit: 5, before: 'a b'}), 'http://h/channels/c1/history?limit=5&before=a%20b');
});

test('pager: first page sets the cursor; older pages move it back until the start', () => {
  const pager = H.createPager(), seen = new Set();
  assert.equal(H.edgeText(pager), '');
  let {fresh, gapBefore} = H.acceptLatest(pager, {messages: [msg('m3'), msg('m4')], has_more: true}, seen);
  assert.deepEqual(ids(fresh), ['m3', 'm4']); assert.equal(gapBefore, null);
  fresh.forEach(m => seen.add(m.message_id));
  assert.equal(pager.oldestId, 'm3'); assert.equal(pager.hasMore, true); assert.equal(H.edgeText(pager), '');
  pager.loading = true;
  assert.equal(H.edgeText(pager), 'loading older…');
  pager.loading = false;
  const older = H.acceptOlder(pager, {messages: [msg('m1'), msg('m2')], has_more: false}, seen);
  assert.deepEqual(ids(older), ['m1', 'm2']);
  assert.equal(pager.oldestId, 'm1'); assert.equal(pager.hasMore, false);
  assert.equal(H.edgeText(pager), 'start of the conversation');
});

test('pager: an empty channel waits for its first message to start the view', () => {
  const pager = H.createPager(), seen = new Set();
  H.acceptLatest(pager, {messages: [], has_more: false}, seen);
  assert.equal(H.edgeText(pager), ''); // no "start of the conversation" over nothing
  const {fresh, gapBefore} = H.acceptLatest(pager, {messages: [msg('m1')], has_more: false}, seen);
  assert.deepEqual(ids(fresh), ['m1']); assert.equal(gapBefore, null);
  assert.equal(pager.oldestId, 'm1');
});

test('polls: overlapping pages append only the unseen tail; no overlap means a gap', () => {
  const pager = H.createPager(), seen = new Set(['m1', 'm2']);
  H.acceptLatest(pager, {messages: [msg('m1'), msg('m2')], has_more: false}, new Set());
  let r = H.acceptLatest(pager, {messages: [msg('m2'), msg('m3')], has_more: true}, seen);
  assert.deepEqual(ids(r.fresh), ['m3']); assert.equal(r.gapBefore, null);
  seen.add('m3');
  r = H.acceptLatest(pager, {messages: [msg('m7'), msg('m8')], has_more: true}, seen);
  assert.equal(r.gapBefore, 'm7');
  // Page back from m7: [m5 m6] then [m3 m4] reaches m3, already shown.
  const pages = [{messages: [msg('m5'), msg('m6')], has_more: true}, {messages: [msg('m3'), msg('m4')], has_more: true}];
  assert.equal(H.gapClosed(pages[0], seen), false);
  assert.equal(H.gapClosed(pages[1], seen), true);
  assert.deepEqual(ids(H.fillGap(pages, r.fresh, seen)), ['m4', 'm5', 'm6', 'm7', 'm8']);
  assert.equal(pager.oldestId, 'm1'); // live polls never move the older-page cursor
});

test('acceptOlder drops messages already shown (a page racing a poll)', () => {
  const pager = H.createPager();
  H.acceptLatest(pager, {messages: [msg('m3')], has_more: true}, new Set());
  const older = H.acceptOlder(pager, {messages: [msg('m2'), msg('m3')], has_more: true}, new Set(['m3']));
  assert.deepEqual(ids(older), ['m2']);
});

// Minimal DOM: children, fragments, insertBefore(node, null) appends, scroll metrics from row count.
class El {
  constructor(tag = 'div') { this.tag = tag; this.children = []; this.dataset = {}; this.attrs = {}; this.textContent = ''; this.className = ''; this.scrollTop = 0; }
  get firstChild() { return this.children[0] ?? null; }
  get nextSibling() { const c = this.parent?.children ?? []; return c[c.indexOf(this) + 1] ?? null; }
  get scrollHeight() { return this.children.reduce((h, n) => h + (n.dataset.day !== undefined ? 30 : n.className === 'history-edge' ? 0 : 20), 0); }
  setAttribute(k, v) { this.attrs[k] = v; }
  appendChild(n) { return this.insertBefore(n, null); }
  append(...nodes) { for (const n of nodes) this.appendChild(n); }
  insertBefore(node, next) {
    const nodes = node.fragment ? node.children.splice(0) : [node];
    for (const n of nodes) { if (n.parent) n.remove(); n.parent = this; }
    const at = next ? this.children.indexOf(next) : this.children.length;
    this.children.splice(at, 0, ...nodes);
    return node;
  }
  remove() { const c = this.parent.children; c.splice(c.indexOf(this), 1); this.parent = null; }
}
const doc = {createElement: tag => new El(tag), createDocumentFragment: () => Object.assign(new El(), {fragment: true})};
const row = (id, ts) => { const r = new El(); r.dataset.ts = ts; r.className = 'row'; r.id = id; return r; };
const view = box => box.children.map(n => n.dataset.day !== undefined ? `--${n.textContent}--` : n.id ?? n.className);
const sync = box => syncDividers(box, {timeOf: n => n.dataset.ts ?? null, now: NOW, doc});
const transcript = () => { const box = new El(); const edge = new El(); edge.className = 'history-edge'; box.append(edge); return {box, edge}; };

test('prependRows inserts after the edge marker in order and keeps the view in place', () => {
  const {box, edge} = transcript();
  box.append(row('c', '2026-11-13T09:00:00Z'), row('d', '2026-11-13T10:00:00Z'));
  box.scrollTop = 5;
  H.prependRows(box, [row('a', '2026-11-13T07:00:00Z'), row('b', '2026-11-13T08:00:00Z')], {after: edge, doc});
  assert.deepEqual(view(box), ['history-edge', 'a', 'b', 'c', 'd']);
  assert.equal(box.scrollTop, 45); // two 20px rows above the view
  H.prependRows(box, [], {after: edge, doc}); // an empty page is a no-op
  assert.equal(box.children.length, 5);
});

test('seam on the same day: one divider, moved to the top of the prepended page', () => {
  const {box, edge} = transcript();
  box.append(row('c', '2026-11-13T09:00:00Z'), row('d', '2026-11-13T10:00:00Z'));
  sync(box);
  assert.deepEqual(view(box), ['history-edge', '--Today--', 'c', 'd']);
  H.prependRows(box, [row('a', '2026-11-13T07:00:00Z'), row('b', '2026-11-13T08:00:00Z')], {after: edge, doc});
  sync(box);
  assert.deepEqual(view(box), ['history-edge', '--Today--', 'a', 'b', 'c', 'd']);
  assert.equal(box.children.filter(n => n.dataset.day !== undefined).length, 1);
});

test('seam on a day change: the older day gets its own divider, the newer keeps its label', () => {
  const {box, edge} = transcript();
  box.append(row('c', '2026-11-13T09:00:00Z'));
  sync(box);
  H.prependRows(box, [row('a', '2026-11-11T07:00:00Z'), row('b', '2026-11-12T22:00:00Z')], {after: edge, doc});
  sync(box);
  assert.deepEqual(view(box), ['history-edge', '--Wed 11 Nov--', 'a', '--Yesterday--', 'b', '--Today--', 'c']);
});

test('three pages prepended in turn: every day divided once, in order, labels unchanged', () => {
  const {box, edge} = transcript();
  const days = ['2026-11-10', '2026-11-11', '2026-11-12', '2026-11-13'];
  const all = [];
  days.forEach((d, i) => { for (let h = 8; h < 14; h++) all.push(row(`${i}-${h}`, `${d}T${String(h).padStart(2, '0')}:00:00Z`)); });
  // Pages of 5 rows, newest page first, like scrolling up; page seams fall inside days.
  box.append(...all.slice(-5)); sync(box);
  for (let end = all.length - 5; end > 0; end -= 5) {
    H.prependRows(box, all.slice(Math.max(0, end - 5), end), {after: edge, doc});
    sync(box);
  }
  const dividers = box.children.filter(n => n.dataset.day !== undefined);
  assert.deepEqual(dividers.map(d => d.textContent), ['Tue 10 Nov', 'Wed 11 Nov', 'Yesterday', 'Today']);
  assert.deepEqual(box.children.filter(n => n.className === 'row').map(n => n.id), all.map(n => n.id));
  for (let i = 1; i < box.children.length; i++) {
    assert.ok(!(box.children[i].dataset.day !== undefined && box.children[i - 1].dataset.day !== undefined), 'adjacent dividers');
  }
});
