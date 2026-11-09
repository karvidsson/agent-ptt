import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

// board.js exports its pure logic as globalThis.BoardLogic and skips the DOM
// code when there is no document, so it loads in a bare vm context.
const source = fs.readFileSync(new URL('../agent_ptt/static/board.js', import.meta.url), 'utf8');
const ctx = vm.createContext({ URLSearchParams, console });
vm.runInContext(source, ctx);
const B = ctx.BoardLogic;
const plain = (v) => JSON.parse(JSON.stringify(v));

const NOW = Date.parse('2026-11-09T12:00:00Z');
const iso = (minutesAgo) => new Date(NOW - minutesAgo * 60000).toISOString();
const finn = { member_id: 'm-finn', handle: 'Finn', harness: 'claude' };
const rex = { member_id: 'm-rex', handle: 'Rex', harness: 'codex' };
function item(o) {
  return {
    item_id: o.id, project_id: o.project || 'p1', project_name: o.projectName || 'agent-ptt',
    kind: o.kind || 'instruction', to: 'project', from: o.from || finn, title: o.title || o.id,
    body: o.title || o.id, context: null, reply_to: o.reply_to || null, state: o.state || 'pending',
    assignee: o.assignee || null, created_at: iso(o.created ?? 10), updated_at: iso(o.updated ?? 5),
    last_event: o.last_event || null, channel_id: 'c1', notice_message_id: null, open_questions: 0,
  };
}
const ids = (list) => Array.from(list, (i) => i.item_id);

test('states map to the five columns', () => {
  const expect = {
    pending: 'open', delivered: 'open', acked: 'open', claimed: 'progress',
    waiting: 'waiting', review: 'review', done: 'done', declined: 'done',
  };
  for (const [s, col] of Object.entries(expect)) assert.equal(B.columnFor(s), col, s);
  assert.equal(B.columnFor('bogus'), null);
  assert.deepEqual(plain(B.COLUMNS.map((c) => c.title)), ['Open', 'In progress', 'Waiting', 'Review', 'Done']);
});

test('only top-level instructions, handoffs and questions are cards', () => {
  assert.equal(B.isCard(item({ id: 'a', kind: 'instruction' })), true);
  assert.equal(B.isCard(item({ id: 'b', kind: 'handoff' })), true);
  assert.equal(B.isCard(item({ id: 'c', kind: 'question' })), true);
  assert.equal(B.isCard(item({ id: 'd', kind: 'question', reply_to: 'a' })), false);
  for (const kind of ['answer', 'note', 'claim', 'release']) assert.equal(B.isCard(item({ id: kind, kind })), false, kind);
  const cols = B.buildColumns([item({ id: 'a' }), item({ id: 'n', kind: 'note' }), item({ id: 'q', kind: 'question', reply_to: 'a' })], {}, NOW);
  assert.deepEqual(ids(cols.open), ['a']);
});

test('a waiting card finds its open question, newest first, ignoring answered ones', () => {
  const items = [
    item({ id: 'w', state: 'waiting' }),
    item({ id: 'q1', kind: 'question', reply_to: 'w', title: 'old', created: 30, state: 'delivered' }),
    item({ id: 'q2', kind: 'question', reply_to: 'w', title: 'newest', created: 2, state: 'pending' }),
    item({ id: 'q3', kind: 'question', reply_to: 'w', title: 'answered', created: 1, state: 'done' }),
    item({ id: 'x', kind: 'note', reply_to: 'w', created: 0 }),
  ];
  const open = B.openQuestionsFor(items, 'w');
  assert.deepEqual(ids(open), ['q2', 'q1']);
  assert.equal(open[0].title, 'newest');
  assert.deepEqual(ids(B.buildColumns(items, {}, NOW).waiting), ['w']);
});

test('done and declined cards drop off after 7 days; open ones never do', () => {
  const day = 24 * 60;
  const items = [
    item({ id: 'recent', state: 'done', updated: 6 * day }),
    item({ id: 'old', state: 'done', updated: 8 * day }),
    item({ id: 'olddecl', state: 'declined', updated: 7 * day + 1 }),
    item({ id: 'stale-open', state: 'pending', updated: 30 * day }),
  ];
  const cols = B.buildColumns(items, {}, NOW);
  assert.deepEqual(ids(cols.done), ['recent']);
  assert.deepEqual(ids(cols.open), ['stale-open']);
});

test('project and agent filters (agent matches sender or assignee)', () => {
  const items = [
    item({ id: 'a', project: 'p1', from: finn }),
    item({ id: 'b', project: 'p2', from: finn, assignee: rex, state: 'claimed' }),
    item({ id: 'c', project: 'p2', from: { member_id: 'm-kim', handle: 'Kim' } }),
  ];
  const all = (f) => Object.values(B.buildColumns(items, f, NOW)).flat().map((i) => i.item_id).sort();
  assert.deepEqual(all({}), ['a', 'b', 'c']);
  assert.deepEqual(all({ project: 'p2' }), ['b', 'c']);
  assert.deepEqual(all({ agent: 'm-rex' }), ['b']);
  assert.deepEqual(all({ agent: 'm-finn' }), ['a', 'b']);
  assert.deepEqual(all({ project: 'p1', agent: 'm-rex' }), []);
  const opts = B.filterOptions(items);
  assert.deepEqual(plain(opts.agents.map((a) => a.label)), ['Finn', 'Kim', 'Rex']);
  assert.deepEqual(plain(opts.projects.map((p) => p.id)), ['p1', 'p2']);
});

test('URL filters parse and serialize round trip', () => {
  assert.deepEqual(plain(B.parseFilters('')), { project: '', agent: '', demo: false });
  assert.equal(B.serializeFilters({ project: '', agent: '' }), '');
  const f = { project: 'p 1/&x', agent: 'm-rex', demo: true };
  const s = B.serializeFilters(f);
  assert.ok(s.startsWith('?project='));
  assert.deepEqual(plain(B.parseFilters(s)), f);
  assert.deepEqual(plain(B.parseFilters('?agent=m-finn&other=1')), { project: '', agent: 'm-finn', demo: false });
});

test('merging an update batch moves a card and keeps order stable', () => {
  const a = item({ id: 'a', created: 30 }), b = item({ id: 'b', created: 20 }), c = item({ id: 'c', created: 10 });
  const items = [a, b, c];
  const claimed = { ...b, state: 'claimed', assignee: rex, updated_at: iso(0) };
  const fresh = item({ id: 'd', created: 0 });
  const merged = B.mergeItems(items, [claimed, fresh]);
  assert.deepEqual(ids(merged), ['a', 'b', 'c', 'd']);
  assert.equal(merged[1].state, 'claimed');
  assert.equal(items[1].state, 'pending', 'input not mutated');
  const before = B.buildColumns(items, {}, NOW), after = B.buildColumns(merged, {}, NOW);
  assert.deepEqual(ids(before.open), ['c', 'b', 'a']);
  assert.deepEqual(ids(after.open), ['d', 'c', 'a']);
  assert.deepEqual(ids(after.progress), ['b']);
  assert.deepEqual(ids(B.mergeItems(merged, [])), ids(merged));
  assert.deepEqual(ids(B.mergeItems(merged, [null, {}])), ids(merged));
});

test('relative age and last-update strings', () => {
  const at = (sec) => new Date(NOW - sec * 1000).toISOString();
  assert.equal(B.shortAge(at(3), NOW), 'just now');
  assert.equal(B.shortAge(at(42), NOW), '42s');
  assert.equal(B.shortAge(at(3 * 60), NOW), '3m');
  assert.equal(B.shortAge(at(5 * 3600), NOW), '5h');
  assert.equal(B.shortAge(at(2 * 86400), NOW), '2d');
  assert.equal(B.shortAge(at(21 * 86400), NOW), '3w');
  assert.equal(B.shortAge(at(-60), NOW), 'just now', 'clock skew clamps to now');
  assert.equal(B.ago(at(3 * 60), NOW), '3m ago');
  assert.equal(B.ago(at(1), NOW), 'just now');
  const it = item({ id: 'a', last_event: { action: 'claimed', by: { member_id: 'm-rex', handle: 'Rex' }, at: at(180) } });
  assert.equal(B.lastUpdateText(it, NOW), 'Rex claimed · 3m ago');
  it.last_event = { action: 'lease_expired', by: null, at: at(60) };
  assert.equal(B.lastUpdateText(it, NOW), 'lease expired · 1m ago');
  it.last_event = { action: 'done', by: rex, at: at(7200) };
  assert.equal(B.lastUpdateText(it, NOW), 'Rex finished · 2h ago');
});

test('thread: ancestors oldest first, the item, then descendants by time', () => {
  const items = [
    item({ id: 'root', created: 50 }),
    item({ id: 'mid', kind: 'handoff', reply_to: 'root', created: 40 }),
    item({ id: 'r2', kind: 'note', reply_to: 'mid', created: 10 }),
    item({ id: 'r1', kind: 'question', reply_to: 'mid', created: 20 }),
    item({ id: 'r1a', kind: 'answer', reply_to: 'r1', created: 15 }),
    item({ id: 'other', created: 5 }),
  ];
  assert.deepEqual(ids(B.buildThread(items, 'mid')), ['root', 'mid', 'r1', 'r1a', 'r2']);
  assert.deepEqual(ids(B.buildThread(items, 'nope')), []);
});

test('demo items fill every column and carry no secrets', () => {
  const demo = B.demoItems(NOW);
  const cols = B.buildColumns(demo, {}, NOW);
  for (const c of B.COLUMNS) assert.ok(cols[c.key].length > 0, c.key);
  const text = JSON.stringify(demo);
  assert.ok(!/key_id|member_key/.test(text));
  assert.equal(B.resourceClaims(demo, {}).length, 1);
});
