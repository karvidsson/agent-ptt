# Horizontal scaling for hosted mode

Status: deferred until the hosted user base outgrows one container. Not blocking launch.
[Deployment](../deployment.md) still pins production to one container and one Uvicorn worker.

## Goal

Run N replicas of the hosted server behind a load balancer without changing tenant
behavior, message ordering, or global limits. Do not shard channels across processes;
replicate the whole hosted process instead.

## Why replication, not sharding

Hosted mode already has the shape of a stateless gateway. The legacy in-memory channel
machinery (`channel.py` dicts, `_tts_worker`, `AudioMixer`) never starts under
`AGENT_PTT_HOSTED=1`; messages are database rows, WebSocket streams poll the database,
duplicates are rejected by `client_id`, and speech is a per-message WAV endpoint the
browser fetches and plays. Two replicas against one database therefore produce correct
results today. Nothing owns a channel, and nothing needs to while audio playback stays in
the browser. The only per-process state that leaks is admission control.

Channel ownership (one worker owning a channel and its audio loop) is only required if a
physical room speaker or server-side mixer returns to hosted mode. That design exists in
the archived [distributed channels plan](../archive/2026-09/distributed-channels.md) and
should be revived on that requirement, not on load.

## Blockers to remove

1. Login rate limiter in `workspace_auth.py` keeps attempts in a process-local deque.
   With N replicas the per-address limit becomes N times larger. Move it to the database
   or a shared store keyed by address and window.
2. Speech admission in `workspace_speech.py` holds the per-organization and server-wide
   synthesis caps in process memory. The server-wide cap of 2 becomes 2N. Move the counters
   to a shared store with expiry so a crashed replica releases its slots.
3. Presence and the 1s stream poll are already database-backed but were sized for one
   process; measure database load at N replicas before raising the poll rate or replica count.

## Implementation stages

1. Add a shared-store abstraction with an in-memory backend (current behavior, tests) and
   a database or Redis backend selected by configuration.
2. Port the login rate limiter, then the synthesis caps, behind it. Keep the existing unit
   tests passing against the in-memory backend and add tests that two limiter instances
   sharing a backend enforce one global limit.
3. Lift the single-replica constraint in [deployment](../deployment.md) and the launch
   checklist in [workspaces](../workspaces.md); document the required store.
4. Load-test two replicas against one database with the workspace client tests, watching
   poll query volume and speech admission.

## Release checks

Verify one global login limit and one global synthesis cap across replicas, no duplicate
messages under concurrent sends, stream continuity when a replica restarts, and that
[client-side speech](client-speech.md), if adopted first, removes the synthesis cap entirely.
