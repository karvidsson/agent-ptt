// Roster order (agent_ptt/static/rostersort.js): groups, recency, stickiness.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const source = await readFile(new URL('../agent_ptt/static/rostersort.js', import.meta.url), 'utf8');
vm.runInThisContext(source);
const {channelGroup, channelRecency, workspaceGroup, workspaceRecency, sortRoster, toMs} = globalThis.AgentPttRoster;

const T = (min) => new Date(Date.UTC(2026, 10, 27, 12, min)).toISOString();
const handles = (people) => people.map((p) => p.handle);

test('channel groups: member presence first, seat state as fallback', () => {
  assert.equal(channelGroup({state: 'active'}, {presence: 'active'}), 0);
  assert.equal(channelGroup({state: 'active'}, {presence: 'listening'}), 1);
  assert.equal(channelGroup({state: 'active'}, {presence: 'idle'}), 2);
  assert.equal(channelGroup({state: 'away'}, {presence: 'away'}), 3);
  assert.equal(channelGroup({state: 'offline'}, {presence: 'away'}), 4);
  assert.equal(channelGroup({state: 'offline'}, {presence: 'listening'}), 1, 'a live wait beats a restored seat');
  assert.equal(channelGroup({state: 'active'}), 0, 'no project member: the seat decides');
  assert.equal(channelGroup({state: 'away'}), 3);
  assert.equal(channelGroup({state: 'offline'}), 4);
  assert.equal(channelGroup({state: 'active'}, {presence: 'active', gone: true}), 0, 'a gone namesake is ignored');
  assert.equal(channelGroup({}, undefined), 0, 'humans in the web UI have no state: active');
});

test('order: active, listening, idle, away, offline; newest first within a group', () => {
  const people = [
    {handle: 'Off', state: 'offline', last_active: T(50)},
    {handle: 'Away', state: 'away', last_active: T(40)},
    {handle: 'IdleOld', state: 'active', last_active: T(1)},
    {handle: 'Listen', state: 'active', last_active: T(2)},
    {handle: 'IdleNew', state: 'active', last_active: T(30)},
    {handle: 'Busy', state: 'active', last_active: T(3)},
  ];
  const members = {IdleOld: {presence: 'idle'}, IdleNew: {presence: 'idle'}, Listen: {presence: 'listening'}, Busy: {presence: 'active'}};
  const sorted = sortRoster(people, {
    groupOf: (p) => channelGroup(p, members[p.handle]),
    recencyOf: (p) => channelRecency(p, members[p.handle]),
  });
  assert.deepEqual(handles(sorted), ['Busy', 'Listen', 'IdleNew', 'IdleOld', 'Away', 'Off']);
});

test('recency uses the newest of seat activity and member last_seen', () => {
  assert.equal(channelRecency({last_active: T(1)}, {last_seen: T(9)}), toMs(T(9)));
  assert.equal(channelRecency({last_active: T(5)}), toMs(T(5)));
  assert.equal(toMs('2026-11-27 12:00:00'), Date.UTC(2026, 10, 27, 12, 0), 'SQLite timestamps are UTC');
  assert.equal(toMs(null), 0);
  assert.equal(toMs('nonsense'), 0);
});

test('sticky: activity changes inside a group keep the order; a group change re-sorts', () => {
  const memo = {};
  const groups = {A: 0, B: 0, C: 2};
  const at = {A: 10, B: 20, C: 30};
  const sort = (people) => sortRoster(people, {groupOf: (p) => groups[p.handle], recencyOf: (p) => at[p.handle], memo});
  const people = [{handle: 'A'}, {handle: 'B'}, {handle: 'C'}];
  assert.deepEqual(handles(sort(people)), ['B', 'A', 'C']);
  at.A = 99; // A was active again: same groups, so no jump
  assert.deepEqual(handles(sort(people)), ['B', 'A', 'C']);
  assert.deepEqual(handles(sort([...people].reverse())), ['B', 'A', 'C'], 'input order does not matter');
  groups.C = 0; // C became active: re-sort with fresh recency
  assert.deepEqual(handles(sort(people)), ['A', 'C', 'B']);
});

test('a newcomer or a leaver re-sorts', () => {
  const memo = {};
  const groups = {A: 1, B: 1, N: 0};
  const at = {A: 10, B: 20, N: 5};
  const sort = (people) => sortRoster(people, {groupOf: (p) => groups[p.handle], recencyOf: (p) => at[p.handle], memo});
  assert.deepEqual(handles(sort([{handle: 'A'}, {handle: 'B'}])), ['B', 'A']);
  at.A = 30;
  assert.deepEqual(handles(sort([{handle: 'A'}, {handle: 'B'}])), ['B', 'A'], 'sticky');
  assert.deepEqual(handles(sort([{handle: 'A'}, {handle: 'B'}, {handle: 'N'}])), ['N', 'A', 'B'], 'newcomer: full re-sort');
  assert.deepEqual(handles(sort([{handle: 'A'}, {handle: 'B'}])), ['A', 'B'], 'leaver: full re-sort');
});

test('ties: by name, then input order; no memo means a plain sort', () => {
  const sorted = sortRoster([{handle: 'b'}, {handle: 'a'}, {handle: 'c'}], {groupOf: () => 1});
  assert.deepEqual(handles(sorted), ['a', 'b', 'c']);
  assert.deepEqual(sortRoster([]), []);
});

test('hosted workspace: sessions and check-ins', () => {
  assert.equal(workspaceGroup({kind: 'human'}, [{effective_state: 'working'}, {effective_state: 'idle'}]), 0);
  assert.equal(workspaceGroup({kind: 'human'}, [{effective_state: 'listening'}]), 1);
  assert.equal(workspaceGroup({kind: 'human'}, [{effective_state: 'idle'}]), 2);
  assert.equal(workspaceGroup({kind: 'human'}, [{effective_state: 'offline'}]), 4);
  assert.equal(workspaceGroup({kind: 'human'}, []), 3, 'nothing reported');
  assert.equal(workspaceGroup({kind: 'agent', online_status: 'online'}, []), 1);
  assert.equal(workspaceGroup({kind: 'agent', online_status: 'online'}, [{effective_state: 'thinking'}]), 0);
  assert.equal(workspaceGroup({kind: 'agent', online_status: 'offline'}, []), 4);
  assert.equal(workspaceGroup({kind: 'agent', online_status: 'unknown'}, []), 3);
  assert.equal(workspaceRecency({last_seen: T(1)}, [{last_refresh: T(7)}]), toMs(T(7)));
});
