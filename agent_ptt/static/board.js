// Agent PTT — Board: a read-only live kanban of project work items.
//
// The pure logic (column mapping, filtering, URL filters, merging poll
// batches, relative ages, demo data) sits at the top and is exported as
// `globalThis.BoardLogic`, so `node --test tests/board.test.mjs` can load this
// file without a browser. The DOM code below only runs when the page has a
// #board element.
(function (root) {
  "use strict";

  // ---- Pure logic ----------------------------------------------------------

  const COLUMNS = [
    { key: "open", title: "Open", states: ["pending", "delivered", "acked"] },
    { key: "progress", title: "In progress", states: ["claimed"] },
    { key: "waiting", title: "Waiting", states: ["waiting"] },
    { key: "review", title: "Review", states: ["review"] },
    { key: "done", title: "Done", states: ["done", "declined"] },
  ];
  const CARD_KINDS = new Set(["instruction", "handoff", "question"]);
  const FINAL_STATES = new Set(["done", "declined"]);
  const DONE_DAYS = 7;
  const DAY_MS = 24 * 60 * 60 * 1000;

  function columnFor(state) {
    const col = COLUMNS.find((c) => c.states.includes(state));
    return col ? col.key : null;
  }

  // Cards are top-level work only: instructions, handoffs, and questions that
  // don't reply to another item. Answers, notes, replies and resource
  // claim/release items are not cards.
  function isCard(item) {
    if (!item || !CARD_KINDS.has(item.kind)) return false;
    if (item.kind === "question" && item.reply_to) return false;
    return true;
  }

  function toMs(iso) {
    const t = Date.parse(iso || "");
    return Number.isNaN(t) ? 0 : t;
  }

  // Done/declined cards drop off after 7 days (the server applies the same
  // cutoff to /work-items; this keeps cards loaded earlier from lingering).
  function withinDoneWindow(item, now, days = DONE_DAYS) {
    if (!FINAL_STATES.has(item.state)) return true;
    return now - toMs(item.updated_at) <= days * DAY_MS;
  }

  function matchesFilters(item, filters) {
    if (filters.project && item.project_id !== filters.project) return false;
    if (filters.agent) {
      const from = item.from && item.from.member_id;
      const assignee = item.assignee && item.assignee.member_id;
      if (from !== filters.agent && assignee !== filters.agent) return false;
    }
    return true;
  }

  // The open questions asked about a card, newest first.
  function openQuestionsFor(items, itemId) {
    return items
      .filter((q) => q.kind === "question" && q.reply_to === itemId && !FINAL_STATES.has(q.state))
      .sort((a, b) => toMs(b.created_at) - toMs(a.created_at));
  }

  // Cards grouped into the five columns. Within a column, newest first
  // (Done: most recently finished first); ties keep the input order.
  function buildColumns(items, filters, now) {
    const out = {};
    for (const c of COLUMNS) out[c.key] = [];
    items.forEach((item, index) => {
      if (!isCard(item) || !withinDoneWindow(item, now) || !matchesFilters(item, filters)) return;
      const key = columnFor(item.state);
      if (key) out[key].push({ item, index });
    });
    for (const c of COLUMNS) {
      const field = c.key === "done" ? "updated_at" : "created_at";
      out[c.key] = out[c.key]
        .sort((a, b) => toMs(b.item[field]) - toMs(a.item[field]) || a.index - b.index)
        .map((e) => e.item);
    }
    return out;
  }

  // Active advisory resource claims (kind "claim", still pending).
  function resourceClaims(items, filters) {
    return items.filter(
      (i) => i.kind === "claim" && i.state === "pending" && matchesFilters(i, filters),
    );
  }

  // Merge a poll batch into the item list by item_id: updated items keep
  // their position, new ones are appended. Returns a new array.
  function mergeItems(items, batch) {
    const out = items.slice();
    const pos = new Map(out.map((it, i) => [it.item_id, i]));
    for (const it of batch || []) {
      if (!it || !it.item_id) continue;
      if (pos.has(it.item_id)) out[pos.get(it.item_id)] = it;
      else {
        pos.set(it.item_id, out.length);
        out.push(it);
      }
    }
    return out;
  }

  function parseFilters(search) {
    const p = new URLSearchParams(search || "");
    return {
      project: p.get("project") || "",
      agent: p.get("agent") || "",
      demo: p.get("demo") === "1",
    };
  }

  function serializeFilters(filters) {
    const p = new URLSearchParams();
    if (filters.project) p.set("project", filters.project);
    if (filters.agent) p.set("agent", filters.agent);
    if (filters.demo) p.set("demo", "1");
    const s = p.toString();
    return s ? "?" + s : "";
  }

  // "just now", "42s", "3m", "5h", "2d", "6w". `shortAge` is the bare
  // duration (card age); `ago` appends " ago" except for "just now".
  function shortAge(iso, now) {
    const s = Math.max(0, Math.floor((now - toMs(iso)) / 1000));
    if (s < 10) return "just now";
    if (s < 60) return s + "s";
    const m = Math.floor(s / 60);
    if (m < 60) return m + "m";
    const h = Math.floor(m / 60);
    if (h < 24) return h + "h";
    const d = Math.floor(h / 24);
    if (d < 14) return d + "d";
    return Math.floor(d / 7) + "w";
  }

  function ago(iso, now) {
    const a = shortAge(iso, now);
    return a === "just now" ? a : a + " ago";
  }

  const ACTION_TEXT = {
    created: "created",
    delivered: "delivered",
    acked: "acked",
    claimed: "claimed",
    released: "released",
    waiting: "is waiting",
    review: "asked for review",
    resumed: "resumed",
    answered: "answered",
    done: "finished",
    declined: "declined",
    lease_expired: "lease expired",
  };

  // "Rex claimed · 3m ago"; server-made events (no `by`) read "lease expired · 3m ago".
  function lastUpdateText(item, now) {
    const ev = item.last_event;
    if (!ev) return "updated · " + ago(item.updated_at, now);
    const verb = ACTION_TEXT[ev.action] || String(ev.action || "updated").replace(/_/g, " ");
    const who = ev.by && ev.by.handle ? ev.by.handle + " " : "";
    return who + verb + " · " + ago(ev.at || item.updated_at, now);
  }

  // Distinct projects and agents seen in the items, for the filter menus.
  function filterOptions(items) {
    const projects = new Map();
    const agents = new Map();
    for (const it of items) {
      if (it.project_id) projects.set(it.project_id, it.project_name || it.project_id);
      for (const who of [it.from, it.assignee]) {
        if (who && who.member_id) agents.set(who.member_id, who.handle || who.member_id);
      }
    }
    const sort = (m) =>
      [...m].map(([id, label]) => ({ id, label })).sort((a, b) => a.label.localeCompare(b.label));
    return { projects: sort(projects), agents: sort(agents) };
  }

  // Thread as GET /projects/{p}/items/{id} builds it: reply_to ancestors
  // (oldest first), the item, then every descendant by created_at. Used by
  // demo mode, which has no server.
  function buildThread(items, itemId) {
    const byId = new Map(items.map((i) => [i.item_id, i]));
    const item = byId.get(itemId);
    if (!item) return [];
    const ancestors = [];
    const seen = new Set([itemId]);
    for (let p = byId.get(item.reply_to); p && !seen.has(p.item_id); p = byId.get(p.reply_to)) {
      seen.add(p.item_id);
      ancestors.unshift(p);
    }
    const descendants = [];
    const frontier = [itemId];
    while (frontier.length) {
      const id = frontier.shift();
      for (const c of items) {
        if (c.reply_to === id && !seen.has(c.item_id)) {
          seen.add(c.item_id);
          descendants.push(c);
          frontier.push(c.item_id);
        }
      }
    }
    descendants.sort((a, b) => toMs(a.created_at) - toMs(b.created_at));
    return [...ancestors, item, ...descendants];
  }

  // Built-in sample items for ?demo=1 (no server needed).
  function demoItems(now) {
    const t = (min) => new Date(now - min * 60000).toISOString();
    const P1 = { id: "demo-project-ptt", name: "agent-ptt" };
    const P2 = { id: "demo-project-site", name: "website" };
    const finn = { member_id: "m-finn", handle: "Finn", harness: "claude" };
    const rex = { member_id: "m-rex", handle: "Rex", harness: "codex" };
    const ada = { member_id: "m-ada", handle: "Ada", harness: "claude" };
    const kim = { member_id: "m-kim", handle: "Kim", harness: "script" };
    const item = (o) => {
      const p = o.project || P1;
      return {
        item_id: o.id,
        project_id: p.id,
        project_name: p.name,
        kind: o.kind,
        to: o.to,
        from: o.from,
        title: o.title,
        body: o.body || o.title,
        context: o.context || null,
        reply_to: o.reply_to || null,
        state: o.state,
        assignee: o.assignee || null,
        lease_expires_at: o.assignee && !FINAL_STATES.has(o.state) ? t(-25) : null,
        created_at: t(o.created),
        updated_at: t(o.updated),
        last_event: { action: o.action, by: o.by === undefined ? o.from : o.by, at: t(o.updated) },
        channel_id: "demo-channel",
        notice_message_id: null,
        open_questions: o.open_questions || 0,
      };
    };
    return [
      item({ id: "d1", kind: "instruction", to: "role:backend", from: ada, title: "Add rate limiting to POST /projects/join", state: "pending", created: 4, updated: 4, action: "created" }),
      item({ id: "d2", kind: "handoff", to: "role:reviewer", from: finn, title: "Review the inbox long-poll change", state: "delivered", created: 22, updated: 21, action: "delivered", by: null,
        context: { repo: "github.com/example/agent-ptt", branch: "feature-inbox-wait", commit: "4f2c9e1", files: ["agent_ptt/projects.py", "tests/test_projects.py"], summary: "Long-poll waits on a per-member asyncio.Event; no DB session is held while waiting.", links: [] } }),
      item({ id: "d3", kind: "question", to: "project", from: kim, title: "Which port does the tailnet test server use?", state: "acked", created: 50, updated: 48, action: "acked", by: rex, project: P2 }),
      item({ id: "d4", kind: "instruction", to: "agent:m-rex", from: ada, title: "Write the board docs section", state: "claimed", assignee: rex, created: 95, updated: 3, action: "claimed", by: rex }),
      item({ id: "d5", kind: "handoff", to: "role:frontend", from: finn, title: "Finish the landing page hero", state: "waiting", assignee: ada, created: 180, updated: 12, action: "waiting", by: ada, project: P2, open_questions: 1 }),
      item({ id: "d5q", kind: "question", to: "agent:m-finn", from: ada, reply_to: "d5", title: "Should the hero use the dark or the light logo?", state: "delivered", created: 12, updated: 11, action: "delivered", by: null, project: P2 }),
      item({ id: "d6", kind: "handoff", to: "role:reviewer", from: rex, title: "Normalize git remotes on project join", state: "review", assignee: rex, created: 300, updated: 30, action: "review", by: rex }),
      item({ id: "d7", kind: "instruction", to: "role:backend", from: ada, title: "Expire claim leases in the reaper", state: "done", assignee: finn, created: 1500, updated: 200, action: "done", by: finn }),
      item({ id: "d7a", kind: "note", to: "role:backend", from: finn, reply_to: "d7", title: "Lease expiry runs every 30 s next to the idle reaper.", state: "pending", created: 210, updated: 210, action: "created" }),
      item({ id: "d8", kind: "instruction", to: "project", from: kim, title: "Port the CLI to Windows", state: "declined", assignee: rex, created: 3000, updated: 2000, action: "declined", by: rex }),
      item({ id: "d9", kind: "claim", to: "project", from: finn, title: "branch:feature-inbox-wait", state: "pending", created: 60, updated: 60, action: "created", context: { resource: "branch:feature-inbox-wait" } }),
    ];
  }

  const BoardLogic = {
    COLUMNS, columnFor, isCard, withinDoneWindow, matchesFilters, openQuestionsFor,
    buildColumns, resourceClaims, mergeItems, parseFilters, serializeFilters,
    shortAge, ago, lastUpdateText, filterOptions, buildThread, demoItems,
  };
  root.BoardLogic = BoardLogic;

  if (typeof document === "undefined" || !document.getElementById("board")) return;

  // ---- DOM -----------------------------------------------------------------

  const $ = (id) => document.getElementById(id);
  const POLL_MS = 3000;
  const state = {
    items: [],
    serverTime: null,
    filters: parseFilters(location.search),
    selected: null,
    cards: new Map(), // item_id -> card element
    loaded: false,
    projects: [],
  };

  // Shared API key: same localStorage entry and Bearer header as index.html.
  const API_KEY_STORE = "agent_ptt_api_key";
  let apiKey = "";
  try { apiKey = localStorage.getItem(API_KEY_STORE) || ""; } catch {}
  let keyPrompted = false;
  function setApiKey(value) {
    apiKey = (value || "").trim();
    try { apiKey ? localStorage.setItem(API_KEY_STORE, apiKey) : localStorage.removeItem(API_KEY_STORE); } catch {}
  }
  function askForKey() {
    if (keyPrompted) return false;
    keyPrompted = true;
    const v = prompt("This server requires an API key (AGENT_PTT_API_KEY).\nIt is stored in this browser only.", apiKey);
    if (v === null) return false;
    setApiKey(v);
    return !!apiKey;
  }
  async function api(url) {
    const headers = new Headers();
    if (apiKey) headers.set("Authorization", "Bearer " + apiKey);
    const r = await fetch(url, { headers });
    if (r.status !== 401 || !askForKey()) return r;
    headers.set("Authorization", "Bearer " + apiKey);
    return fetch(url, { headers });
  }

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  function setStatus(text, ok) {
    const s = $("serverStatus");
    s.textContent = text;
    s.className = ok ? "ok" : "bad";
  }

  // ---- Filters ---------------------------------------------------------------

  function fillSelect(select, allLabel, options, value) {
    const current = [...select.options].map((o) => o.value + "\u0000" + o.textContent).join("|");
    const wanted = [{ id: "", label: allLabel }, ...options];
    if (value && !wanted.some((o) => o.id === value)) wanted.push({ id: value, label: value });
    const next = wanted.map((o) => o.id + "\u0000" + o.label).join("|");
    if (current !== next) {
      select.replaceChildren(...wanted.map((o) => new Option(o.label, o.id)));
    }
    select.value = value;
  }

  function renderFilters() {
    const opts = filterOptions(state.items);
    const projects = new Map(opts.projects.map((p) => [p.id, p.label]));
    for (const p of state.projects) if (!projects.has(p.project_id)) projects.set(p.project_id, p.name);
    const projectList = [...projects].map(([id, label]) => ({ id, label })).sort((a, b) => a.label.localeCompare(b.label));
    fillSelect($("projectFilter"), "All projects", projectList, state.filters.project);
    fillSelect($("agentFilter"), "All agents", opts.agents, state.filters.agent);
  }

  function applyFilters() {
    state.filters.project = $("projectFilter").value;
    state.filters.agent = $("agentFilter").value;
    history.replaceState(null, "", location.pathname + serializeFilters(state.filters));
    render();
  }

  // ---- Cards -----------------------------------------------------------------

  function cardBody(card, item, now) {
    const title = el("div", "card-title", item.title || "(untitled)");
    const meta = el("div", "card-meta");
    meta.append(el("span", "project", item.project_name || ""));
    meta.append(el("span", "kind", item.kind));
    meta.append(el("span", "age", shortAge(item.created_at, now)));
    meta.lastChild.title = "created " + new Date(item.created_at).toLocaleString();
    const who = el("div", "card-who");
    if (item.assignee) {
      who.append(el("span", "handle", item.assignee.handle));
      if (item.assignee.harness) who.append(el("span", "harness", item.assignee.harness));
    } else {
      who.append(el("span", "unassigned", "unassigned"));
    }
    if (item.state === "declined") who.append(el("span", "declined", "declined"));
    const parts = [title, meta, who];
    if (item.state === "waiting") {
      const q = openQuestionsFor(state.items, item.item_id)[0];
      const box = el("div", "card-question");
      box.append(el("span", "q-mark", "?"));
      box.append(el("span", "", q ? q.title : item.open_questions ? item.open_questions + " open question(s)" : "waiting"));
      parts.push(box);
    }
    parts.push(el("div", "card-update", lastUpdateText(item, now)));
    card.replaceChildren(...parts);
  }

  function cardFor(item, now) {
    let card = state.cards.get(item.item_id);
    if (!card) {
      card = el("button", "card");
      card.type = "button";
      card.dataset.id = item.item_id;
      card.onclick = () => openPanel(card.dataset.id);
      state.cards.set(item.item_id, card);
    }
    card.classList.toggle("selected", state.selected === item.item_id);
    card.dataset.state = item.state;
    cardBody(card, item, now);
    return card;
  }

  function render() {
    const now = Date.now();
    renderFilters();
    const cols = buildColumns(state.items, state.filters, now);

    // FLIP: remember where every card was, move nodes, then animate from there.
    const before = new Map();
    for (const [id, card] of state.cards) if (card.isConnected) before.set(id, card.getBoundingClientRect());

    const keep = new Set();
    let total = 0;
    for (const c of COLUMNS) {
      const list = $("col-" + c.key);
      const cards = cols[c.key].map((it) => {
        keep.add(it.item_id);
        return cardFor(it, now);
      });
      total += cards.length;
      $("count-" + c.key).textContent = cards.length;
      if (cards.length) list.replaceChildren(...cards);
      else list.replaceChildren(el("div", "col-empty", "—"));
    }
    for (const [id, card] of state.cards) if (!keep.has(id)) { card.remove(); state.cards.delete(id); }

    for (const [id, card] of state.cards) {
      const old = before.get(id);
      if (!old) {
        card.animate?.([{ opacity: 0, transform: "scale(0.97)" }, { opacity: 1, transform: "none" }], { duration: 220, easing: "ease-out" });
        continue;
      }
      const now2 = card.getBoundingClientRect();
      const dx = old.left - now2.left, dy = old.top - now2.top;
      if (dx || dy) card.animate?.([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: "none" }], { duration: 320, easing: "cubic-bezier(.2,.8,.2,1)" });
    }

    const empty = $("emptyState");
    const anyCard = state.items.some((i) => isCard(i) && withinDoneWindow(i, now));
    if (!state.loaded) empty.classList.add("hidden");
    else if (!anyCard) { empty.textContent = ""; empty.append("No work items yet — agents create them with ", el("code", "", "agent-ptt send")); empty.classList.remove("hidden"); }
    else if (!total) { empty.textContent = "No work items match these filters."; empty.classList.remove("hidden"); }
    else empty.classList.add("hidden");

    const claims = resourceClaims(state.items, state.filters);
    const strip = $("claims");
    strip.classList.toggle("hidden", !claims.length);
    strip.replaceChildren(el("span", "claims-label", "claimed resources"), ...claims.map((c) => {
      const chip = el("span", "claim-chip", (c.context && c.context.resource) || c.title);
      chip.title = (c.from ? c.from.handle : "") + " · " + (c.project_name || "") + " · " + ago(c.created_at, now);
      chip.append(el("span", "claim-who", c.from ? c.from.handle : ""));
      return chip;
    }));

    if (state.selected) refreshPanelHeader();
  }

  // ---- Side panel ------------------------------------------------------------

  function threadEntry(it, isFocus, now) {
    const box = el("div", "t-item" + (isFocus ? " focus" : ""));
    const head = el("div", "t-head");
    head.append(el("span", "kind", it.kind));
    head.append(el("span", "handle", it.from ? it.from.handle : "?"));
    if (it.from && it.from.harness) head.append(el("span", "harness", it.from.harness));
    head.append(el("span", "to", "→ " + (it.to || "")));
    head.append(el("span", "grow"));
    head.append(el("span", "state s-" + it.state, it.state));
    head.append(el("span", "age", ago(it.created_at, now)));
    box.append(head);
    if (it.title && it.title !== it.body) box.append(el("div", "t-title", it.title));
    box.append(el("div", "t-body", it.body || ""));
    const ctx = it.context;
    if (ctx && typeof ctx === "object") {
      const dl = el("dl", "t-context");
      for (const key of ["repo", "branch", "commit", "summary", "resource"]) {
        if (ctx[key]) { dl.append(el("dt", "", key), el("dd", "", String(ctx[key]))); }
      }
      if (Array.isArray(ctx.files) && ctx.files.length) dl.append(el("dt", "", "files"), el("dd", "", ctx.files.join("\n")));
      if (Array.isArray(ctx.links) && ctx.links.length) {
        const dd = el("dd", "");
        for (const link of ctx.links) {
          const a = el("a", "", String(link));
          if (/^https?:\/\//.test(String(link))) { a.href = String(link); a.target = "_blank"; a.rel = "noopener"; }
          dd.append(a, document.createElement("br"));
        }
        dl.append(el("dt", "", "links"), dd);
      }
      if (dl.children.length) box.append(dl);
    }
    return box;
  }

  function refreshPanelHeader() {
    const item = state.items.find((i) => i.item_id === state.selected);
    if (!item) return;
    $("panelTitle").textContent = item.title || "(untitled)";
    const sub = $("panelSub");
    sub.replaceChildren(
      el("span", "", item.project_name || ""), el("span", "sep", "·"),
      el("span", "state s-" + item.state, item.state), el("span", "sep", "·"),
      el("span", "", item.assignee ? item.assignee.handle : "unassigned"),
    );
    const link = $("panelChannel");
    if (item.channel_id) {
      link.href = "/ui/#" + encodeURIComponent(item.channel_id);
      link.classList.remove("hidden");
    } else link.classList.add("hidden");
  }

  let panelToken = 0;
  async function openPanel(id) {
    const item = state.items.find((i) => i.item_id === id);
    if (!item) return;
    state.selected = id;
    for (const [cid, card] of state.cards) card.classList.toggle("selected", cid === id);
    $("panel").classList.remove("hidden");
    refreshPanelHeader();
    const body = $("panelBody");
    body.replaceChildren(el("div", "panel-note", "loading thread…"));
    const token = ++panelToken;
    let data;
    try {
      if (state.filters.demo) {
        data = { item, thread: buildThread(state.items, id), events: demoEvents(item) };
      } else {
        const r = await api(`/projects/${encodeURIComponent(item.project_id)}/items/${encodeURIComponent(id)}`);
        if (!r.ok) throw new Error("HTTP " + r.status);
        data = await r.json();
      }
    } catch (e) {
      if (token === panelToken) body.replaceChildren(el("div", "panel-note", "Couldn't load the thread (" + e.message + ")."));
      return;
    }
    if (token !== panelToken) return;
    const now = Date.now();
    const thread = data.thread && data.thread.length ? data.thread : [data.item || item];
    const parts = [el("div", "panel-sec", "Thread"), ...thread.map((t) => threadEntry(t, t.item_id === id, now))];
    const events = data.events || [];
    if (events.length) {
      parts.push(el("div", "panel-sec", "Events"));
      const ul = el("ul", "events");
      for (const ev of events) {
        const li = el("li", "");
        li.append(el("span", "age", ago(ev.at, now)));
        li.append(el("span", "", (ev.by && ev.by.handle ? ev.by.handle + " " : "") + String(ev.action).replace(/_/g, " ")));
        if (ev.from_state || ev.to_state) li.append(el("span", "dim", (ev.from_state || "") + " → " + (ev.to_state || "")));
        if (ev.note) li.append(el("span", "note", ev.note));
        ul.append(li);
      }
      parts.push(ul);
    }
    body.replaceChildren(...parts);
  }

  function demoEvents(item) {
    const evs = [{ action: "created", by: item.from, at: item.created_at, from_state: null, to_state: "pending", note: null }];
    if (item.last_event && item.last_event.action !== "created") {
      evs.push({ action: item.last_event.action, by: item.last_event.by, at: item.last_event.at, from_state: null, to_state: item.state, note: null });
    }
    return evs;
  }

  function closePanel() {
    state.selected = null;
    panelToken++;
    $("panel").classList.add("hidden");
    for (const card of state.cards.values()) card.classList.remove("selected");
  }

  // ---- Polling ---------------------------------------------------------------

  let polling = false;
  async function poll() {
    if (polling) return;
    polling = true;
    try { await pollOnce(); } finally { polling = false; }
  }

  async function pollOnce() {
    const url = state.serverTime
      ? "/work-items?updated_after=" + encodeURIComponent(state.serverTime)
      : "/work-items";
    try {
      const r = await api(url);
      if (r.status === 404) {
        setStatus("no work items API on this server", false);
        state.loaded = true;
        render();
        return;
      }
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      state.items = mergeItems(state.items, data.items || []);
      if (data.server_time) state.serverTime = data.server_time;
      state.loaded = true;
      setStatus("live", true);
      render();
    } catch {
      setStatus("offline", false);
    }
  }

  async function loadProjects() {
    try {
      const r = await api("/projects");
      if (r.ok) state.projects = (await r.json()).projects || [];
    } catch {}
  }

  function boot() {
    $("host").textContent = location.host;
    $("projectFilter").onchange = applyFilters;
    $("agentFilter").onchange = applyFilters;
    $("panelClose").onclick = closePanel;
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePanel(); });
    $("apiKeyBtn").onclick = () => {
      const v = prompt("Server API key (leave empty to clear):", apiKey);
      if (v === null) return;
      setApiKey(v);
      location.reload();
    };
    if (state.filters.demo) {
      $("apiKeyBtn").classList.add("hidden");
      state.items = demoItems(Date.now());
      state.loaded = true;
      setStatus("demo data", true);
      render();
      setInterval(render, 30000);
      return;
    }
    loadProjects().then(render);
    poll();
    setInterval(poll, POLL_MS);
    setInterval(render, 30000); // keep relative ages fresh between changes
  }

  boot();
})(globalThis);
