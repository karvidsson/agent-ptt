/* Token usage ticker, totals strip and subscription chips (index.html, board).
   No build, no deps. A classic script: it sets globalThis.AgentPttUsage so the
   pages share it, and node tests can evaluate it directly (tests/usage.test.mjs).

   Data comes from GET /projects/{p}/usage (docs/api-reference.md). Headline =
   input + cache write + output; cache reads are shown in the tooltip only. */
(function (root) {
  'use strict';
  const STALE_MS = 30 * 60 * 1000;
  const ESTIMATE_STORE = 'agentPttUsageEstimate';
  const REPORTING = new Set(['claude', 'codex', 'cursor']);
  const HARNESS_NAMES = {claude: 'Claude', codex: 'Codex', cursor: 'Cursor', agy: 'Antigravity'};
  const pad = n => String(n).padStart(2, '0');
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

  // 0, 999, 1.2k, 340k, 1.2M, 12M, 1.5B: one decimal below 10, whole numbers above.
  function formatTokens(n) {
    n = Math.max(0, Number(n) || 0);
    if (n < 999.5) return String(Math.round(n));
    const units = [[1e3, 'k'], [1e6, 'M'], [1e9, 'B'], [1e12, 'T']];
    for (let i = 0; i < units.length; i++) {
      const [div, unit] = units[i];
      const v = n / div;
      const text = v < 9.95 ? (Math.round(v * 10) / 10).toFixed(1).replace(/\.0$/, '') : String(Math.round(v));
      if (Number(text) < 1000 || i === units.length - 1) return text + unit;
    }
    return String(n);
  }

  function formatUsd(v) {
    if (v === null || v === undefined) return '';
    if (v < 0.01) return '<$0.01';
    return '$' + (v < 100 ? v.toFixed(2) : String(Math.round(v)));
  }

  function parseTime(value) {
    if (!value) return null;
    let text = String(value);
    if (/^\d{4}-\d{2}-\d{2}[ T]\d/.test(text) && !/(?:Z|[+-]\d{2}:?\d{2})$/i.test(text)) text = text.replace(' ', 'T') + 'Z';
    const t = Date.parse(text);
    return Number.isNaN(t) ? null : new Date(t);
  }

  // "14:00" today, "Fri 14:00" on another day (viewer's local time).
  function clock(value, now = new Date()) {
    const d = parseTime(value);
    if (!d) return '';
    const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    const same = d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
    return same ? hm : `${['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][d.getDay()]} ${hm}`;
  }

  function isStale(reportedAt, now = new Date(), maxAgeMs = STALE_MS) {
    const d = parseTime(reportedAt);
    return !d || now - d > maxAgeMs;
  }

  // "1.2M · today 340k" (+ " · ≈$4.10" with the estimate on); "—" without data.
  function tickerText(usage, {estimate = false} = {}) {
    if (!usage || !usage.total) return '—';
    let text = `${formatTokens(usage.total.headline)} · today ${formatTokens(usage.today?.headline)}`;
    if (estimate && usage.total.est_usd !== undefined && usage.total.est_usd !== null) text += ` · ≈${formatUsd(usage.total.est_usd)}`;
    return text;
  }

  function partsLine(c) {
    return `in ${formatTokens(c.input)} · cache read ${formatTokens(c.cache_read)} · cache write ${formatTokens(c.cache_write)} · out ${formatTokens(c.output)}`;
  }

  // Tooltip: totals per bucket, then a line per model (input/cache read/cache write/output).
  function tickerTitle(usage, harness, {estimate = false} = {}) {
    if (!usage || !usage.total) {
      return harness && REPORTING.has(String(harness).toLowerCase()) ? 'no usage reported yet' : "harness doesn't report usage";
    }
    const lines = ['tokens = input + cache write + output (cache reads listed apart)'];
    for (const [label, bucket] of [['session', usage.session], ['today', usage.today], ['total', usage.total]]) {
      if (!bucket) continue;
      let line = `${label}: ${formatTokens(bucket.headline)} (${partsLine(bucket)})`;
      if (estimate && bucket.est_usd !== undefined && bucket.est_usd !== null) line += ` ≈${formatUsd(bucket.est_usd)}`;
      lines.push(line);
    }
    const models = Object.entries(usage.by_model || {});
    if (models.length) lines.push('per model (total):');
    for (const [model, raw] of models) {
      const c = raw.total || raw;
      let line = `  ${model}: ${partsLine(c)}`;
      if (c.reasoning) line += ` (reasoning ${formatTokens(c.reasoning)})`;
      if (estimate) line += c.est_usd === null || c.est_usd === undefined ? ' · no price' : ` ≈${formatUsd(c.est_usd)}`;
      lines.push(line);
    }
    if (estimate) lines.push('≈$ is an estimate at API list prices, not what a subscription costs');
    return lines.join('\n');
  }

  // "Claude (kbot) 5h 41% · 7d 12% · resets 14:00", "Codex prolite (kbot) wk 4%".
  function limitLabel(limit, now = new Date()) {
    const name = HARNESS_NAMES[limit.harness] || limit.harness || '?';
    const head = [name, limit.plan, limit.machine ? `(${limit.machine})` : ''].filter(Boolean).join(' ');
    const windows = (limit.windows || []).map(w => `${w.name} ${Math.round(Number(w.used_pct) || 0)}%`);
    const reset = (limit.windows || []).find(w => w.resets_at);
    const parts = windows.length ? [windows.join(' · ')] : ['no windows'];
    if (reset) parts.push(`resets ${clock(reset.resets_at, now)}`);
    return `${head} ${parts.join(' · ')}`;
  }

  function limitChip(limit, now = new Date()) {
    const stale = isStale(limit.reported_at, now);
    const asOf = limit.reported_at ? `as of ${clock(limit.reported_at, now)}` : 'never reported';
    const title = `${limit.source || 'subscription usage'} · ${asOf}` + (limit.reported_by ? ` · from ${limit.reported_by.handle}` : '');
    return {text: limitLabel(limit, now) + (stale ? ` · ${asOf}` : ''), title, stale};
  }

  // The strip above the roster: project today/total, then one chip per subscription.
  function stripHtml(payload, {estimate = false, now = new Date()} = {}) {
    if (!payload) return '';
    const money = bucket => (estimate && bucket?.est_usd !== null && bucket?.est_usd !== undefined ? ` ≈${formatUsd(bucket.est_usd)}` : '');
    const totals = `<span class="usage-total" title="${esc(`project tokens (input + cache write + output)\ntoday: ${partsLine(payload.today || {})}\ntotal: ${partsLine(payload.total || {})}`)}">` +
      `today ${esc(formatTokens(payload.today?.headline))}${esc(money(payload.today))} · total ${esc(formatTokens(payload.total?.headline))}${esc(money(payload.total))}</span>`;
    const chips = (payload.limits || []).map(limit => {
      const chip = limitChip(limit, now);
      return `<span class="usage-chip${chip.stale ? ' stale' : ''}" title="${esc(chip.title)}">${esc(chip.text)}</span>`;
    }).join('');
    return totals + chips;
  }

  function estimateOn(storage = root.localStorage) {
    try { return storage?.getItem(ESTIMATE_STORE) === '1'; } catch { return false; }
  }
  function setEstimate(on, storage = root.localStorage) {
    try { on ? storage.setItem(ESTIMATE_STORE, '1') : storage.removeItem(ESTIMATE_STORE); } catch { /* private mode */ }
  }

  // Case-insensitive handle -> member usage entry, for matching channel participants.
  function byHandle(payload) {
    const map = new Map();
    for (const m of payload?.members || []) {
      const key = String(m.handle).toLowerCase();
      if (m.gone && map.has(key)) continue; // a current member wins over a gone namesake
      map.set(key, m);
    }
    return map;
  }

  root.AgentPttUsage = {
    STALE_MS, ESTIMATE_STORE, formatTokens, formatUsd, parseTime, clock, isStale,
    tickerText, tickerTitle, limitLabel, limitChip, stripHtml, estimateOn, setEstimate, byHandle,
  };
})(globalThis);
