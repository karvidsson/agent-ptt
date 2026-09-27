# Optional speech delivery

> Archived 2026-09-27: historical implementation/design record. Session assignments
> and deployment/test claims below describe that earlier work, not current instructions.
> Start with [current documentation](../../README.md) and the [next phase](../../next-phase.md).

Current implementation: server-side Pocket TTS with browser WAV playback.
Client-side model execution is deferred; see the [roadmap](../../roadmap/client-speech.md).

Status: server route and client adapter implemented; integration owned by Olive
(routes/assets) and Ada (Listen controls). No shared-server restart or deployment.

Text is the product's durable record. Saving, receiving, and rendering messages
never waits for speech. Listening is opt-in and local to each human listener;
playback is not synchronized between listeners.

## Server contract

Register `agent_ptt.workspace_speech.install(app)` once. No tables, migrations,
or changes to message persistence are needed. Allowlist `workspace-speech.js`
in `/workspace/assets/{name}`. Workspace CSP must allow `media-src 'self' blob:`
for the fetched WAV object URLs.

Paths begin `/api/workspace/organizations/{org}/channels/{room}/messages/{id}`:

- `GET /speech`: authenticated WAV for this saved message.
- `GET /speech/metadata`: `{message_id, text, truncated, voice, mime_type}`.
  Voice is `{engine: "pocket-tts", id: "alba" | ..., scope: "public-catalog",
  version: 1}`. Text is capped at 600 characters; metadata reports truncation.

Both routes require current organization access and match organization, channel,
and message ID together. The audio route checks access again after synthesis,
so credentials revoked during inference cannot fetch the result. Browser clients
use their session cookie; agents use the normal bearer credential. Audio and
metadata responses are `no-store`. There are no public audio URLs or global
voice-profile reads. Only the eight built-in Pocket catalog voices are used,
selected deterministically from organization and authenticated sender identity.
Voices can repeat; this is not a guarantee of unique voices for every agent.

The process allows at most two outstanding jobs, at most one per organization,
and no waiting job queue. Additional requests return 429 with Retry-After.
Limits are 20 requests/minute per identity, 40/minute per organization, a 30-second
response wait, and 8 MiB output. Timeouts return 504; synthesis errors return 503
without leaking backend details. A timed-out/cancelled HTTP request retains its
capacity slot until inference actually finishes, because cancelling an async
wait does not stop a CPU thread. A stuck backend therefore consumes bounded
capacity rather than accumulating abandoned jobs. These limits are per process;
multiple server processes need shared admission control before scaling.

## Client integration

```js
import {SpeechQueue, ServerSpeechProvider} from '/workspace/assets/workspace-speech.js';
const speech = new SpeechQueue({
  provider: new ServerSpeechProvider(),
  onError: (error, message) => showAudioStatus(error.message),
});
// In a human's Listen button handler:
speech.setMuted(false);
// After rendering a NEW message, independently of text delivery:
speech.enqueue({orgId, channelId, id: message.id, text: message.text});
// On organization/channel switch or logout:
speech.cancel();
// On mute:
speech.setMuted(true);
```

Do not enqueue initial history. Defaults: muted, FIFO order, up to 32 waiting
messages, bounded duplicate tracking for the most recent 512 IDs (scoped to org
and channel). Queue overflow is reported and drops that audio request. Cancel
clears pending work and aborts the current fetch/playback; mute also cancels.
Errors report through `onError` and allow subsequent messages to proceed.
Object URLs are released. A two-minute playback watchdog prevents a missing
media-ended event from blocking all following clips.

Browser autoplay rules still apply. Start listening from a user gesture; if
playback is rejected, show an audio status and leave chat usable. This adapter
does not claim to bypass autoplay restrictions. The UI owner handles controls.

Providers implement `prepare(item, {signal}) -> {play({signal}), dispose()}`.
Preparation must not play audio; providers must honor abort and release resources.
A browser synthesizer consumes the same text-plus-voice metadata, while the
server provider fetches the saved-message rendition. An optional fallback runs
only when preparation fails with `error.fallbackAllowed === true`. HTTP/auth
errors do not opt into fallback. Once playback starts, no automatic fallback
or replay is allowed, preventing duplicate partial speech.

## Browser Pocket TTS compatibility experiment — 2026-09-27

Browser synthesis remains a separate experiment, not a chat prerequisite or a
shipped production provider. No third-party inference code or model was added
to the application's dependencies.

The [Kyutai project](https://github.com/kyutai-labs/pocket-tts) identifies browser
WebAssembly/JavaScript implementations. The community
[ONNX demo documentation](https://huggingface.co/spaces/KevinAHM/pocket-tts-web/blob/main/README.md)
requires a modern browser, HTTPS/localhost, and cross-origin isolation for
threaded ONNX Runtime. It lists Chrome, Edge, Firefox, and Safari. This is an
upstream compatibility claim, not our tested browser matrix. The
[ONNX Runtime matrix](https://onnxruntime.ai/docs/get-started/with-javascript/web.html)
lists WebAssembly on those browser families; WebGPU support is narrower.

Observed in this machine's Chrome, using the remote demo's English April 2026
bundle and Alba voice:

| Check | Result |
|---|---|
| Input | “The build is finished. All tests passed.” |
| Completion | Demo UI reached Finished and enabled Download |
| Time to first byte | Demo displayed 148 ms |
| Generation speed | Demo displayed 3.45× real time |
| Cold model download duration | Not measured; model was Ready before generation |
| Peak memory / actual network transfer | Not measured |
| Edge, Firefox, Safari, mobile | Not tested locally |

These are single-sample, demo-reported timings, not an independent benchmark or
our adapter's end-to-end latency. Hardware/browser-version benchmarking remains.
The [English bundle directory](https://huggingface.co/spaces/KevinAHM/pocket-tts-web/tree/main/onnx/english_2026-04)
lists 199 MB of assets, including a 52.4 MB `voices.bin`; directory size is not
browser heap usage or a measured network download. Working memory includes
runtime, graph tensors, and decoded voice state and must be measured separately.

Voice portability is not established. The demo offers the same eight catalog
names, but its April 2026 ONNX checkpoint and `voices.bin` representation cannot
be assumed interchangeable with our installed Python model or `.safetensors`
clones. A real provider must pin model/runtime hashes, map supported voice IDs,
and reject unsupported versions before playback. Do not send global or another
tenant's cloned profiles to browsers.

Before shipping browser synthesis: measure cold/warm load, first audible sample,
peak memory, cancellation and long-message behavior on the named desktop/mobile
browsers; compare catalog voices against the server checkpoint; review model
licensing; test worker isolation headers and autoplay. Keep server speech as an
optional provider and text available throughout model downloads or failures.

## Verification

- `python -m pytest tests/test_workspace_speech.py`: fake TTS, scoped access,
  revocation before/during inference, text surviving TTS failure, capacity held
  across timeout and released on backend completion.
- `node --test tests/workspace-speech.test.mjs`: ordering/dedup, mute/cancel,
  context switch, queue bounds, fallback without duplicate playback, cookie
  fetch and object-URL cleanup.
- Browser experiment above used synthetic text only; no tenant data or voices.
