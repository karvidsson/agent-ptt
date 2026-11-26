// Outdated badge and "Update agents" list (agent_ptt/static/updates.js).
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const source = await readFile(new URL('../agent_ptt/static/updates.js', import.meta.url), 'utf8');
vm.runInThisContext(source);
const U = globalThis.AgentPttUpdates;

test('badge only for outdated rows, with the version tooltip', () => {
  assert.equal(U.badgeHtml(null), '');
  assert.equal(U.badgeHtml({outdated: false, plugin_version: '0.6.2', plugin_latest: null}), '');
  const row = {outdated: true, plugin_version: '0.5.6', plugin_latest: '0.6.2'};
  assert.equal(U.badgeTitle(row), 'plugin 0.5.6 < latest 0.6.2');
  assert.equal(U.badgeHtml(row), '<span class="outdated-badge" title="plugin 0.5.6 &lt; latest 0.6.2">outdated</span>');
});

test('badge escapes what agents report', () => {
  const html = U.badgeHtml({outdated: true, plugin_version: '<b>"x"', plugin_latest: '0.6.2'});
  assert.ok(!html.includes('<b>'));
  assert.ok(html.includes('&lt;b&gt;&quot;x&quot;'));
});

const DRY = {
  latest: {claude: '0.6.2', codex: '0.6.2'},
  source: 'manifests',
  dry_run: true,
  total: 2,
  projects: [{project_id: 'p1', project: 'zeta', nudged: 1}, {project_id: 'p2', project: 'alpha', nudged: 1}],
  members: [
    {handle: 'Wade', harness: 'codex', machine: 'mac', plugin_version: '0.6.0', latest: '0.6.2', project: 'zeta', nudged: null},
    {handle: 'Ada', harness: 'claude', machine: null, plugin_version: '0.5.6', latest: '0.6.2', project: 'alpha', nudged: '0.6.1'},
  ],
};

test('list shows handle, harness, machine, version vs latest, project', () => {
  const html = U.listHtml(DRY);
  assert.ok(html.includes('Latest: Claude 0.6.2 · Codex 0.6.2'));
  const rows = html.split('<tr>').slice(2);
  assert.equal(rows.length, 2);
  assert.ok(rows[0].startsWith('<td>Ada</td><td>Claude</td><td>—</td>'), rows[0]);
  assert.ok(rows[0].includes('<span class="old">0.5.6</span> → 0.6.2</td><td>alpha</td><td>told 0.6.1</td>'));
  assert.ok(rows[1].startsWith('<td>Wade</td><td>Codex</td><td>mac</td>'));
});

test('list: nobody to nudge, and nudges off', () => {
  assert.ok(U.listHtml({...DRY, total: 0, members: []}).includes('No agent needs a nudge'));
  assert.ok(U.listHtml({latest: {}, source: 'off', members: []}).includes('off on this server (off)'));
});

test('confirm and result texts', () => {
  assert.equal(U.confirmText(DRY), 'Send 2 agents a private note telling them to update their Agent PTT plugin?');
  assert.equal(U.confirmText({total: 1}), 'Send 1 agent a private note telling them to update their Agent PTT plugin?');
  assert.equal(U.resultText({total: 2, projects: DRY.projects}), 'Sent 2 nudges (zeta: 1, alpha: 1).');
  assert.equal(U.resultText({total: 0, projects: []}), 'Nobody needed a nudge.');
});
