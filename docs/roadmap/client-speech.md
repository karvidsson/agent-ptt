# Client-side Pocket TTS generation

Status: deferred. Current speech generation remains on the server with Pocket TTS.
The browser downloads complete WAV responses; it does not run a model or use OS voices.

## Goal

Move inference to the listener's device while preserving Pocket voice identities,
tenant authorization, and reliable text chat. Do not reintroduce another speech engine.

## Implementation stages

1. Evaluate a pinned Pocket TTS ONNX/WebAssembly browser implementation. Verify its
   license, model provenance, voice-state format, and compatibility with our model version.
2. Benchmark cold download, warm start, memory, generation speed, and voice fidelity
   on typical desktop and mobile browsers before selecting a runtime.
3. Add a worker-based provider behind the existing speech queue. Load model files
   only after explicit opt-in, show progress, and cache versioned assets on the device.
4. Use public cacheable assets for the base model/catalog. Scope custom voice downloads
   to organization membership; clear private application caches on logout and tenant changes.
   A downloaded voice asset cannot be remotely recalled from a user's device.
5. Keep stable voice IDs and versioned reference/voice-state assets. Test custom
   reference-audio cloning separately; natural-language voice design is not provided.
6. Preserve mute/cancellation, bounded queues, deduplication, and text-only operation.
   Decide explicitly whether failed client generation may request server speech;
   never silently replay partially spoken messages or bypass authorization failures.

## Release checks

Verify voice similarity, browser support, device performance, cache upgrades,
organization isolation, cancellation, and distribution bandwidth costs. Roll out
behind a feature flag; leave server speech as the default until these checks pass.
