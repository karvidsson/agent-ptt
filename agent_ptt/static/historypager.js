/* Paged channel transcripts for the web UI (index.html). No build, no deps.
   A classic script: it sets globalThis.AgentPttHistory, and node tests evaluate it directly.

   The UI loads the latest page of GET /channels/{id}/history?limit=N, polls that same
   latest page for new messages, and fetches older pages with ?before=<oldest message id>
   when the reader scrolls to the top. Every page is {messages (oldest first), has_more}. */
(function (root) {
  'use strict';
  const PAGE_SIZE = 100;
  // Gap fill stops after this many older pages; the reader can still scroll for the rest.
  const MAX_GAP_PAGES = 10;

  function historyUrl(base, channelId, {limit = PAGE_SIZE, before = null} = {}) {
    let url = `${base}/channels/${encodeURIComponent(channelId)}/history?limit=${limit}`;
    if (before) url += `&before=${encodeURIComponent(before)}`;
    return url;
  }

  // Pagination state for one open channel.
  //   oldestId: the cursor for the next older page (null until a message is loaded)
  //   hasMore:  older messages exist on the server
  //   loading:  an older page is in flight
  function createPager() {
    return {oldestId: null, hasMore: false, loading: false, loaded: false};
  }

  /* Take the latest page (initial load or a poll). Returns the unseen messages to append,
     oldest first, and gapBefore: when a poll's page shares nothing with what is shown and
     older messages exist, messages may have been skipped between them, and gapBefore is
     the cursor to page back from to fill the gap. */
  function acceptLatest(pager, page, seen) {
    const messages = page.messages || [];
    const fresh = messages.filter(m => !seen.has(m.message_id));
    if (!pager.loaded || pager.oldestId === null) {
      // First load, or the channel was empty until now: this page starts the view.
      pager.loaded = true;
      pager.hasMore = !!page.has_more;
      if (messages.length) pager.oldestId = messages[0].message_id;
      return {fresh, gapBefore: null};
    }
    const overlaps = fresh.length < messages.length;
    const gapBefore = !overlaps && page.has_more && fresh.length ? fresh[0].message_id : null;
    return {fresh, gapBefore};
  }

  // Take an older page. Returns its unseen messages to prepend, oldest first.
  function acceptOlder(pager, page, seen) {
    const messages = page.messages || [];
    if (messages.length) pager.oldestId = messages[0].message_id;
    pager.hasMore = !!page.has_more && messages.length > 0;
    return messages.filter(m => !seen.has(m.message_id));
  }

  /* Merge gap-fill pages (each oldest first, fetched newest page first) with the poll's
     fresh messages: every unseen message, oldest first, without duplicates. Stops at the
     first page that reaches something already shown. */
  function fillGap(pages, fresh, seen) {
    const out = [], ids = new Set();
    for (const page of [...pages].reverse()) {
      for (const m of page.messages || []) {
        if (seen.has(m.message_id) || ids.has(m.message_id)) continue;
        ids.add(m.message_id); out.push(m);
      }
    }
    for (const m of fresh) if (!ids.has(m.message_id)) { ids.add(m.message_id); out.push(m); }
    return out;
  }

  // Whether a gap-fill page reached messages already shown (or the start of the history).
  function gapClosed(page, seen) {
    const messages = page.messages || [];
    return !page.has_more || messages.some(m => seen.has(m.message_id));
  }

  // The marker text at the top of the transcript.
  function edgeText(pager) {
    if (pager.loading) return 'loading older…';
    if (pager.loaded && !pager.hasMore && pager.oldestId !== null) return 'start of the conversation';
    return '';
  }

  /* Insert rows before the first transcript row in one DOM write, keeping what the reader
     sees in place: the scroll offset grows by exactly the height added above it. */
  function prependRows(box, nodes, {after = null, doc = root.document} = {}) {
    if (!nodes.length) return;
    const previous = box.scrollHeight;
    const fragment = doc.createDocumentFragment();
    for (const node of nodes) fragment.appendChild(node);
    box.insertBefore(fragment, after ? after.nextSibling : box.firstChild);
    box.scrollTop += box.scrollHeight - previous;
  }

  root.AgentPttHistory = {
    PAGE_SIZE, MAX_GAP_PAGES, historyUrl, createPager, acceptLatest, acceptOlder,
    fillGap, gapClosed, edgeText, prependRows,
  };
})(globalThis);
