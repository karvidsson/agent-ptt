/* Optional speech delivery. Never owns message persistence or text rendering. */
export class ServerSpeechProvider {
  constructor({fetchImpl = globalThis.fetch.bind(globalThis), audioFactory = () => new Audio(),
    urls = globalThis.URL} = {}) {
    this.fetch = fetchImpl; this.audioFactory = audioFactory; this.urls = urls;
  }
  async prepare(item, {signal}) {
    const path = `/api/workspace/organizations/${encodeURIComponent(item.orgId)}` +
      `/channels/${encodeURIComponent(item.channelId)}/messages/${encodeURIComponent(item.id)}/speech`;
    const response = await this.fetch(path, {signal, credentials: 'same-origin', cache: 'no-store'});
    if (!response.ok) throw new Error(`Speech unavailable (${response.status})`);
    const blob = await response.blob();
    if (signal.aborted) throw new DOMException('Cancelled', 'AbortError');
    const url = this.urls.createObjectURL(blob);
    let audio;
    try {audio = this.audioFactory(); audio.src = url;}
    catch (error) {this.urls.revokeObjectURL(url); throw error;}
    let disposed = false;
    return {
      play: ({signal: playbackSignal}) => new Promise((resolve, reject) => {
        let timer;
        const cleanup = () => {
          clearTimeout(timer);
          audio.removeEventListener('ended', ended);
          audio.removeEventListener('error', failed);
          playbackSignal.removeEventListener('abort', aborted);
        };
        const ended = () => {cleanup(); resolve();};
        const failed = () => {cleanup(); reject(new Error('Audio playback failed'));};
        const aborted = () => {
          audio.pause(); cleanup(); reject(new DOMException('Cancelled', 'AbortError'));
        };
        if (playbackSignal.aborted) {aborted(); return;}
        audio.addEventListener('ended', ended);
        audio.addEventListener('error', failed);
        playbackSignal.addEventListener('abort', aborted, {once: true});
        timer = setTimeout(() => {audio.pause(); failed();}, 120000);
        Promise.resolve().then(() => {
          if (!playbackSignal.aborted) return audio.play();
        }).catch(error => {cleanup(); reject(error);});
      }),
      dispose: () => {
        if (disposed) return;
        disposed = true; audio.pause(); audio.removeAttribute('src');
        this.urls.revokeObjectURL(url);
      },
    };
  }
}

export class SpeechQueue {
  constructor({provider, fallback = null, onError = () => {}, maxPending = 32} = {}) {
    this.provider = provider; this.fallback = fallback; this.onError = onError;
    this.maxPending = maxPending; this.muted = true; this.items = [];
    this.seen = new Set(); this.running = false; this.controller = null;
  }
  setMuted(value) {this.muted = Boolean(value); if (this.muted) this.cancel();}
  cancel() {
    this.items = []; this.seen.clear(); this.controller?.abort();
  }
  enqueue(item) {
    const key = JSON.stringify([item.orgId, item.channelId, item.id]);
    if (this.muted || this.seen.has(key)) return false;
    if (this.items.length >= this.maxPending) {
      this.report(new Error('Speech queue full'), item); return false;
    }
    this.seen.add(key);
    if (this.seen.size > 512) this.seen.delete(this.seen.values().next().value);
    this.items.push({...item}); void this.drain(); return true;
  }
  report(error, item) {try {this.onError(error, item);} catch { /* UI must not break chat. */ }}
  async drain() {
    if (this.running) return;
    this.running = true;
    try {
      while (!this.muted && this.items.length) {
        const item = this.items.shift();
        const controller = new AbortController(); this.controller = controller;
        const options = {signal: controller.signal};
        let prepared;
        try {
          try {prepared = await this.provider.prepare(item, options);}
          catch (error) {
            // Fallback is opt-in for safe pre-play failures only. Never replay a
            // partially spoken message, or fallback around authorization errors.
            if (!this.fallback || !error.fallbackAllowed || options.signal.aborted) throw error;
            prepared = await this.fallback.prepare(item, options);
          }
          if (!options.signal.aborted) await prepared.play(options);
        } catch (error) {
          if (!options.signal.aborted) this.report(error, item);
        } finally {
          try {prepared?.dispose();} catch { /* Best effort cleanup. */ }
          if (this.controller === controller) this.controller = null;
        }
      }
    } finally {this.running = false;}
  }
}
