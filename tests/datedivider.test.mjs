// Day dividers (agent_ptt/static/datedivider.js): labels, UTC parsing, placement.
// Pin the viewer's timezone before any Date use: Stockholm is UTC+1 in November.
process.env.TZ = 'Europe/Stockholm';
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const source = await readFile(new URL('../agent_ptt/static/datedivider.js', import.meta.url), 'utf8');
vm.runInThisContext(source);
const {parseTimestamp, dayKey, dayLabel, needsDivider, syncDividers, onMidnight} = globalThis.AgentPttDates;
const NOW = new Date('2026-11-13T15:00:00+01:00'); // Fri 13 Nov 2026, local afternoon

test('labels: today, yesterday, older this year, another year', () => {
  assert.equal(dayLabel('2026-11-13T08:00:00+01:00', NOW), 'Today');
  assert.equal(dayLabel('2026-11-12T23:59:00+01:00', NOW), 'Yesterday');
  assert.equal(dayLabel('2026-11-11T12:00:00Z', NOW), 'Wed 11 Nov');
  assert.equal(dayLabel('2026-01-02T12:00:00Z', NOW), 'Fri 2 Jan');
  assert.equal(dayLabel('2025-12-29T12:00:00Z', NOW), 'Mon 29 Dec 2025');
  assert.equal(dayLabel('2026-11-13T08:00:00Z', NOW, {relative: false}), 'Fri 13 Nov');
  assert.equal(dayLabel('2026-11-13T08:00:00Z', NOW, {relative: false, year: true}), 'Fri 13 Nov 2026');
  assert.equal(dayLabel('not a date', NOW), '');
});

test('yesterday across a month and a year boundary', () => {
  assert.equal(dayLabel('2026-11-30T12:00:00Z', new Date('2026-12-01T09:00:00+01:00')), 'Yesterday');
  assert.equal(dayLabel('2025-12-31T12:00:00Z', new Date('2026-01-01T09:00:00+01:00')), 'Yesterday');
});

test('timestamps without an offset are UTC, so late-evening UTC lands on the next local day', () => {
  // 23:30 UTC on the 12th is 00:30 on the 13th in Stockholm.
  for (const raw of ['2026-11-12 23:30:00', '2026-11-12T23:30:00', '2026-11-12T23:30:00.123456']) {
    assert.equal(parseTimestamp(raw).toISOString().slice(0, 16), '2026-11-12T23:30', raw);
    assert.equal(dayKey(raw), '2026-11-13', raw);
    assert.equal(dayLabel(raw, NOW), 'Today', raw);
  }
  assert.equal(dayKey('2026-11-12T23:30:00+00:00'), '2026-11-13');
  assert.equal(dayKey('2026-11-12T23:30:00-05:00'), '2026-11-13');
  assert.equal(dayKey('2026-11-12T22:30:00Z'), '2026-11-12');
  assert.equal(dayKey(Date.UTC(2026, 10, 12, 23, 30)), '2026-11-13');
  assert.equal(dayKey(null), null);
});

test('needsDivider: first message, same day, day change', () => {
  assert.equal(needsDivider(null, '2026-11-13T08:00:00Z'), true);
  assert.equal(needsDivider('2026-11-13T08:00:00Z', '2026-11-13T20:00:00Z'), false);
  assert.equal(needsDivider('2026-11-13T20:00:00Z', '2026-11-13T23:30:00Z'), true); // local midnight passed
  assert.equal(needsDivider('2026-11-12T10:00:00Z', 'garbage'), false);
});

// Minimal DOM: just enough of Element for syncDividers.
class El {
  constructor(tag = 'div') { this.tag = tag; this.children = []; this.dataset = {}; this.attrs = {}; this.textContent = ''; this.className = ''; }
  setAttribute(k, v) { this.attrs[k] = v; }
  append(...nodes) { for (const n of nodes) { n.parent = this; this.children.push(n); } }
  insertBefore(node, next) { node.parent = this; this.children.splice(this.children.indexOf(next), 0, node); }
  remove() { const c = this.parent.children; c.splice(c.indexOf(this), 1); }
}
const doc = {createElement: tag => new El(tag)};
const row = ts => { const r = new El(); r.dataset.ts = ts; r.className = 'row'; return r; };
const view = box => box.children.map(n => n.dataset.day !== undefined ? `--${n.textContent}--` : n.dataset.ts ?? n.className);
const sync = (box, now = NOW) => syncDividers(box, {timeOf: n => n.dataset.ts ?? null, now, doc});

test('placement: divider before the first message and between days, none within a day', () => {
  const box = new El();
  box.append(row('2026-11-11T10:00:00Z'), row('2026-11-11T11:00:00Z'), row('2026-11-12 23:30:00'), row('2026-11-13T09:00:00Z'));
  sync(box);
  assert.deepEqual(view(box), ['--Wed 11 Nov--', '2026-11-11T10:00:00Z', '2026-11-11T11:00:00Z',
    '--Today--', '2026-11-12 23:30:00', '2026-11-13T09:00:00Z']);
  const divider = box.children[0];
  assert.equal(divider.attrs.role, 'separator');
  assert.equal(divider.attrs['aria-label'], 'Wed 11 Nov');
  assert.equal(divider.className, 'day-divider');
  sync(box); // idempotent: keeps the same nodes
  assert.equal(box.children[0], divider);
  assert.equal(box.children.length, 6);
});

test('placement: rows without a timestamp are ignored and a system row shares its day', () => {
  const box = new El();
  const empty = new El(); empty.className = 'empty';
  box.append(empty);
  sync(box);
  assert.deepEqual(view(box), ['empty']);
  box.append(row('2026-11-13T09:00:00Z'), row('2026-11-13T09:00:05.000Z'), row('2026-11-13T09:01:00Z'));
  sync(box);
  assert.deepEqual(view(box), ['empty', '--Today--', '2026-11-13T09:00:00Z', '2026-11-13T09:00:05.000Z', '2026-11-13T09:01:00Z']);
});

test('live append across midnight adds a divider and relabels the old day', () => {
  const box = new El();
  box.append(row('2026-11-13T22:50:00Z')); // 23:50 local on the 13th
  sync(box, new Date('2026-11-13T23:55:00+01:00'));
  assert.deepEqual(view(box), ['--Today--', '2026-11-13T22:50:00Z']);
  // Midnight passes, then a new message arrives.
  box.append(row('2026-11-13T23:05:00Z')); // 00:05 local on the 14th
  sync(box, new Date('2026-11-14T00:06:00+01:00'));
  assert.deepEqual(view(box), ['--Yesterday--', '2026-11-13T22:50:00Z', '--Today--', '2026-11-13T23:05:00Z']);
});

test('a row inserted out of order moves the divider with it', () => {
  const box = new El();
  const late = row('2026-11-13T10:00:00Z');
  box.append(row('2026-11-12T10:00:00Z'), late);
  sync(box);
  assert.deepEqual(view(box), ['--Yesterday--', '2026-11-12T10:00:00Z', '--Today--', '2026-11-13T10:00:00Z']);
  // An earlier-day message lands between the Today divider and its row (insert-by-id path).
  box.insertBefore(row('2026-11-12T11:00:00Z'), late);
  sync(box);
  assert.deepEqual(view(box), ['--Yesterday--', '2026-11-12T10:00:00Z', '2026-11-12T11:00:00Z', '--Today--', '2026-11-13T10:00:00Z']);
  // Removing every row of a day removes its divider too.
  box.children[1].remove(); box.children[1].remove();
  sync(box);
  assert.deepEqual(view(box), ['--Today--', '2026-11-13T10:00:00Z']);
});

test('onMidnight schedules just after the next local midnight and reschedules', () => {
  const timers = []; let calls = 0;
  onMidnight(() => { calls++; }, {now: () => NOW, setTimer: (cb, ms) => timers.push({cb, ms})});
  assert.equal(timers.length, 1);
  assert.equal(timers[0].ms, 9 * 3600 * 1000 + 1000); // 15:00 → 00:00:01
  timers[0].cb();
  assert.equal(calls, 1);
  assert.equal(timers.length, 2);
});
