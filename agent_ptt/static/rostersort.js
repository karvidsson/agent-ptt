/* Roster order: who is around comes first. No build, no deps.
   A classic script: it sets globalThis.AgentPttRoster so index.html (classic) and
   workspace.js (module) can share it, and node tests can evaluate it directly.

   Groups, top to bottom: 0 active/working, 1 listening/online, 2 idle, 3 away,
   4 offline/gone. Within a group the most recent activity comes first. The order
   is sticky: while every person stays in the same group, a re-render keeps the
   previous order, so the list doesn't jump on every poll tick just because
   someone's last activity moved. */
(function (root) {
  'use strict';
  const GROUPS = ['active', 'listening', 'idle', 'away', 'offline'];
  const RANK = { active: 0, working: 0, thinking: 0, draining: 0, speaking: 0, listening: 1, online: 1, idle: 2, away: 3, offline: 4, gone: 4 };

  function rankOf(word) {
    const rank = RANK[String(word || '').toLowerCase()];
    return rank === undefined ? null : rank;
  }

  function toMs(value) {
    if (value === null || value === undefined || value === '') return 0;
    if (typeof value === 'number') return value;
    let text = String(value).trim();
    // SQLite timestamps without an offset are UTC.
    if (/^\d{4}-\d{2}-\d{2}[ T]\d/.test(text) && !/(?:Z|[+-]\d{2}:?\d{2})$/i.test(text)) text = text.replace(' ', 'T') + 'Z';
    const ms = Date.parse(text);
    return Number.isNaN(ms) ? 0 : ms;
  }

  // Channel participant (state: active | away | offline) plus, when known, its
  // project member (presence: active | listening | idle | away; gone_at).
  function channelGroup(participant, member, { speaking = false } = {}) {
    if (speaking) return 0;
    const seat = participant?.state || 'active';
    const presence = member?.gone ? null : rankOf(member?.presence);
    if (presence !== null) {
      // A restored seat nobody came back to, whose member is long quiet too.
      if (seat === 'offline' && presence >= 3) return 4;
      return presence;
    }
    return rankOf(seat) ?? 0;
  }

  function channelRecency(participant, member) {
    return Math.max(toMs(participant?.last_active), toMs(participant?.since), toMs(member?.last_seen), toMs(member?.doing_at));
  }

  // Hosted workspace: a person plus its presence sessions (effective_state:
  // working | thinking | draining | listening | idle | offline); agents also carry
  // online_status (online | offline | unknown) from their check-ins.
  function workspaceGroup(person, sessions = []) {
    const ranks = sessions.map((s) => rankOf(s.effective_state || s.state)).filter((r) => r !== null);
    const online = { online: 1, offline: 4, unknown: 3 }[person?.online_status];
    if (online !== undefined) ranks.push(online);
    return ranks.length ? Math.min(...ranks) : 3;
  }

  function workspaceRecency(person, sessions = []) {
    return Math.max(toMs(person?.last_seen), ...sessions.map((s) => toMs(s.last_refresh)));
  }

  // Sorts ``people`` into groups. ``keyOf`` names a person, ``groupOf`` gives its
  // group number, ``recencyOf`` its last activity (ms or a timestamp). ``memo``
  // (an object you keep between renders) makes the order sticky: when the same
  // people are in the same groups as last time, the last order is reused.
  function sortRoster(people, { keyOf = (p) => p.handle, groupOf, recencyOf = () => 0, memo = null } = {}) {
    const rows = people.map((person, index) => ({
      person,
      index,
      key: String(keyOf(person)),
      group: groupOf ? groupOf(person) : 0,
      recency: toMs(recencyOf(person)),
    }));
    const signature = rows.map((r) => `${r.key}\u0000${r.group}`).sort().join('\u0001');
    if (memo && memo.signature === signature && Array.isArray(memo.order)) {
      const at = new Map(memo.order.map((key, i) => [key, i]));
      rows.sort((a, b) => at.get(a.key) - at.get(b.key));
    } else {
      rows.sort((a, b) => a.group - b.group || b.recency - a.recency || a.key.localeCompare(b.key) || a.index - b.index);
    }
    if (memo) {
      memo.signature = signature;
      memo.order = rows.map((r) => r.key);
    }
    return rows.map((r) => r.person);
  }

  root.AgentPttRoster = { GROUPS, RANK, rankOf, toMs, channelGroup, channelRecency, workspaceGroup, workspaceRecency, sortRoster };
})(globalThis);
