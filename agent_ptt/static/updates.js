/* Outdated plugin badge and the "Update agents" dialog (index.html).
   No build, no deps. A classic script: it sets globalThis.AgentPttUpdates so
   node tests can evaluate it directly (tests/updates.test.mjs).

   Data: the roster's GET /projects/{p}/usage rows carry plugin_version,
   plugin_latest and outdated; the dialog lists POST /announce-update?dry_run=1
   and sends with POST /announce-update (docs/api-reference.md). */
(function (root) {
  'use strict';
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const HARNESS_NAMES = {claude: 'Claude', codex: 'Codex', cursor: 'Cursor', antigravity: 'Antigravity', agy: 'Antigravity'};
  const harnessName = h => HARNESS_NAMES[String(h || '').toLowerCase()] || (h ? String(h) : '?');
  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

  // "plugin 0.5.6 < latest 0.6.2" for a roster row that is behind; "" otherwise.
  function badgeTitle(member) {
    if (!member || !member.outdated || !member.plugin_latest) return '';
    return `plugin ${member.plugin_version || '?'} < latest ${member.plugin_latest}`;
  }

  function badgeHtml(member) {
    const title = badgeTitle(member);
    return title ? `<span class="outdated-badge" title="${esc(title)}">outdated</span>` : '';
  }

  // Dry-run members sorted by project, then handle.
  function sortedMembers(body) {
    return [...(body?.members || [])].sort((a, b) =>
      String(a.project || '').localeCompare(String(b.project || '')) ||
      String(a.handle || '').localeCompare(String(b.handle || '')));
  }

  function listHtml(body) {
    const latest = body?.latest || {};
    const versions = Object.keys(latest).sort().map(h => `${harnessName(h)} ${latest[h]}`).join(' · ');
    if (!Object.keys(latest).length) {
      return `<p class="side-note">Update nudges are off on this server (${esc(body?.source || 'off')}).</p>`;
    }
    const head = `<p class="side-note">Latest: ${esc(versions)} <span class="dim">(${esc(body.source || '')})</span></p>`;
    const members = sortedMembers(body);
    if (!members.length) return head + '<p>No agent needs a nudge: every current agent is up to date or was already told.</p>';
    const rows = members.map(m =>
      `<tr><td>${esc(m.handle)}</td><td>${esc(harnessName(m.harness))}</td><td>${esc(m.machine || '—')}</td>` +
      `<td><span class="old">${esc(m.plugin_version)}</span> → ${esc(m.latest)}</td><td>${esc(m.project || '—')}</td>` +
      `<td>${m.nudged ? `told ${esc(m.nudged)}` : ''}</td></tr>`).join('');
    return head +
      '<table class="update-list"><thead><tr><th>Agent</th><th>Harness</th><th>Machine</th><th>Version</th><th>Project</th><th></th></tr></thead>' +
      `<tbody>${rows}</tbody></table>`;
  }

  function confirmText(body) {
    const n = body?.total || 0;
    return `Send ${plural(n, 'agent')} a private note telling them to update their Agent PTT plugin?`;
  }

  function resultText(result) {
    const total = result?.total || 0;
    if (!total) return 'Nobody needed a nudge.';
    const parts = (result.projects || []).map(p => `${p.project || p.project_id}: ${p.nudged}`);
    return `Sent ${plural(total, 'nudge')} (${parts.join(', ')}).`;
  }

  root.AgentPttUpdates = {badgeTitle, badgeHtml, sortedMembers, listHtml, confirmText, resultText, harnessName};
})(globalThis);
