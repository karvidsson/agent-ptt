/* Day dividers for chat transcripts (channel view and workspace). No build, no deps.
   A classic script: it sets globalThis.AgentPttDates so index.html (classic) and
   workspace.js (module) can share it, and node tests can evaluate it directly. */
(function (root) {
  'use strict';
  const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const pad = n => String(n).padStart(2, '0');

  // SQLite timestamps without an offset are stored in UTC, so read them as UTC.
  function parseTimestamp(value) {
    if (value === null || value === undefined || value === '') return null;
    if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
    if (typeof value === 'number') return new Date(value);
    let text = String(value).trim();
    if (/^\d{4}-\d{2}-\d{2}[ T]\d/.test(text)) {
      text = text.replace(' ', 'T');
      if (!/(?:Z|[+-]\d{2}:?\d{2})$/i.test(text)) text += 'Z';
    }
    const date = new Date(text);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  // The viewer's local calendar day, e.g. "2026-11-12"; null for an unreadable timestamp.
  function dayKey(value) {
    const d = parseTimestamp(value);
    return d ? `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` : null;
  }

  // "Today", "Yesterday", "Wed 12 Nov", or "Mon 29 Dec 2025" outside the current year.
  // relative:false skips Today/Yesterday; year:true always adds the year (exports).
  function dayLabel(value, now = new Date(), {relative = true, year = false} = {}) {
    const d = parseTimestamp(value);
    if (!d) return '';
    const n = parseTimestamp(now) || new Date();
    if (relative) {
      const key = dayKey(d);
      if (key === dayKey(n)) return 'Today';
      const y = new Date(n.getFullYear(), n.getMonth(), n.getDate() - 1, 12);
      if (key === dayKey(y)) return 'Yesterday';
    }
    const base = `${DAYS[d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()]}`;
    return year || d.getFullYear() !== n.getFullYear() ? `${base} ${d.getFullYear()}` : base;
  }

  // A divider goes before a message when it starts the log or starts a new local day.
  function needsDivider(previous, current) {
    const key = dayKey(current);
    if (!key) return false;
    return previous === null || previous === undefined || dayKey(previous) !== key;
  }

  /* Bring a transcript container's dividers in line with its rows. Idempotent and safe
     to call after every render or append: it inserts missing dividers, relabels kept
     ones (so "Today" becomes "Yesterday" after midnight) and removes stale ones.
     timeOf(node) returns a row's timestamp, or null for nodes that aren't rows. */
  function syncDividers(box, {timeOf, now = new Date(), doc = root.document, className = 'day-divider'} = {}) {
    const existing = [...box.children].filter(node => node.dataset?.day !== undefined);
    const kept = new Set();
    let prevKey = null, before = null;
    for (const node of [...box.children]) {
      if (node.dataset?.day !== undefined) { before = node; continue; }
      const time = timeOf(node), key = time === null || time === undefined ? null : dayKey(time);
      if (!key) continue;
      if (key !== prevKey) {
        let divider = before && before.dataset.day === key && !kept.has(before) ? before : null;
        if (!divider) {
          divider = doc.createElement('div');
          divider.className = className;
          divider.dataset.day = key;
          divider.setAttribute('role', 'separator');
          box.insertBefore(divider, node);
        }
        const label = dayLabel(time, now);
        if (divider.textContent !== label) {
          divider.textContent = label;
          divider.setAttribute('aria-label', label);
        }
        kept.add(divider);
      }
      prevKey = key; before = null;
    }
    for (const divider of existing) if (!kept.has(divider)) divider.remove();
  }

  // Run fn just after each local midnight, so Today/Yesterday labels never go stale.
  function onMidnight(fn, {setTimer = (cb, ms) => root.setTimeout(cb, ms), now = () => new Date()} = {}) {
    const schedule = () => {
      const n = now();
      const next = new Date(n.getFullYear(), n.getMonth(), n.getDate() + 1, 0, 0, 1);
      setTimer(() => { try { fn(); } finally { schedule(); } }, Math.max(1000, next - n));
    };
    schedule();
  }

  root.AgentPttDates = {parseTimestamp, dayKey, dayLabel, needsDivider, syncDividers, onMidnight};
})(globalThis);
