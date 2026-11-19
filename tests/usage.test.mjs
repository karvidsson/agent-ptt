// Token usage ticker, chips and staleness (agent_ptt/static/usage.js).
// Pin the viewer's timezone before any Date use: Stockholm is UTC+1 in November.
process.env.TZ = 'Europe/Stockholm';
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const source = await readFile(new URL('../agent_ptt/static/usage.js', import.meta.url), 'utf8');
vm.runInThisContext(source);
const U = globalThis.AgentPttUsage;
const NOW = new Date('2026-11-19T12:00:00+01:00');

const bucket = (input, cache_read, cache_write, output, extra = {}) => ({
  input, cache_read, cache_write, output, reasoning: 0, headline: input + cache_write + output, ...extra,
});
const USAGE = {
  session: bucket(2, 48653, 1485, 240),
  today: bucket(100, 2_000_000, 40_000, 300_000),
  total: bucket(1000, 90_000_000, 200_000, 1_000_000, {est_usd: 31.5}),
  by_model: {'claude-opus-5-5': {...bucket(1000, 90_000_000, 200_000, 1_000_000), reasoning: 5000, est_usd: null}},
};

test('token counts are compact', () => {
  const cases = [[0, '0'], [999, '999'], [1000, '1k'], [1234, '1.2k'], [9949, '9.9k'], [10_400, '10k'],
    [340_000, '340k'], [999_999, '1M'], [1_200_000, '1.2M'], [12_340_000, '12M'], [1_500_000_000, '1.5B'],
    [null, '0'], [-5, '0']];
  for (const [n, text] of cases) assert.equal(U.formatTokens(n), text, `${n}`);
});

test('ticker: headline and today, dash without data', () => {
  assert.equal(U.tickerText(USAGE), '1.2M · today 340k');
  assert.equal(U.tickerText(USAGE, {estimate: true}), '1.2M · today 340k · ≈$31.50');
  assert.equal(U.tickerText(null), '—');
  assert.equal(U.tickerText({}), '—');
});

test('tooltip breaks down by part and model; no-data wording depends on the harness', () => {
  const title = U.tickerTitle(USAGE, 'claude');
  assert.match(title, /session: 1\.7k \(in 2 · cache read 49k · cache write 1\.5k · out 240\)/);
  assert.match(title, /claude-opus-5-5: in 1k · cache read 90M · cache write 200k · out 1M \(reasoning 5k\)/);
  assert.doesNotMatch(title, /≈/);
  assert.match(U.tickerTitle(USAGE, 'claude', {estimate: true}), /no price/);
  assert.equal(U.tickerTitle(null, 'agy'), "harness doesn't report usage");
  assert.equal(U.tickerTitle(null, null), "harness doesn't report usage");
  assert.equal(U.tickerTitle(null, 'codex'), 'no usage reported yet');
});

test('dollars', () => {
  assert.equal(U.formatUsd(0.001), '<$0.01');
  assert.equal(U.formatUsd(4.1), '$4.10');
  assert.equal(U.formatUsd(1234.5), '$1235');
  assert.equal(U.formatUsd(null), '');
});

test('staleness: older than 30 minutes, or unknown', () => {
  assert.equal(U.isStale('2026-11-19T10:45:00Z', NOW), false); // 15 min
  assert.equal(U.isStale('2026-11-19T10:29:00Z', NOW), true); // 31 min
  assert.equal(U.isStale('2026-11-19 10:45:00', NOW), false); // naive = UTC
  assert.equal(U.isStale(null, NOW), true);
});

test('subscription chips', () => {
  const claude = {harness: 'claude', machine: 'kbot', plan: null, source: 'claude-statusline',
    windows: [{name: '5h', used_pct: 41, resets_at: '2026-11-19T13:00:00Z'}, {name: '7d', used_pct: 12.4}],
    reported_at: '2026-11-19T10:55:00Z', reported_by: {handle: 'Ada'}};
  const chip = U.limitChip(claude, NOW);
  assert.equal(chip.text, 'Claude (kbot) 5h 41% · 7d 12% · resets 14:00');
  assert.equal(chip.stale, false);
  assert.match(chip.title, /as of 11:55 · from Ada/);

  const codex = {harness: 'codex', machine: 'kbot', plan: 'prolite',
    windows: [{name: 'wk', used_pct: 4, resets_at: '2026-11-21T08:00:00Z'}], reported_at: '2026-11-19T09:00:00Z'};
  const old = U.limitChip(codex, NOW);
  assert.equal(old.stale, true);
  assert.equal(old.text, 'Codex prolite (kbot) wk 4% · resets Sat 09:00 · as of 10:00');
});

test('strip: totals plus chips, escaped', () => {
  const html = U.stripHtml({today: bucket(1, 0, 0, 999), total: bucket(0, 0, 0, 2_000_000),
    limits: [{harness: '<b>', windows: [], reported_at: null}]}, {now: NOW});
  assert.match(html, /today 1k · total 2M/);
  assert.match(html, /usage-chip stale/);
  assert.match(html, /&lt;b&gt; no windows · never reported/);
  assert.equal(U.stripHtml(null), '');
});

test('estimate toggle is remembered, off by default', () => {
  const store = new Map();
  const storage = {getItem: k => store.get(k) ?? null, setItem: (k, v) => store.set(k, v), removeItem: k => store.delete(k)};
  assert.equal(U.estimateOn(storage), false);
  U.setEstimate(true, storage);
  assert.equal(U.estimateOn(storage), true);
  U.setEstimate(false, storage);
  assert.equal(U.estimateOn(storage), false);
});

test('members match channel participants case-insensitively', () => {
  const map = U.byHandle({members: [{handle: 'Ada', usage: null}]});
  assert.ok(map.get('ada'));
  assert.equal(U.byHandle(null).size, 0);
});
