# Current Proxmox test deployment

Deployed on 2026-09-27 to Proxmox VM **108**, `agent-ptt-test`:

- Ubuntu 24.04, 4 vCPUs, 8 GiB RAM, 60 GiB disk.
- Guest DHCP address: `192.168.30.103`.
- Proxmox access through Tailscale: `100.81.137.103`.
- Application source: `/opt/agent-ptt` in the guest.
- PostgreSQL 16 and the application run as Docker services, with reboot recovery verified.
- Speech is generated on the server with Pocket TTS; the browser plays WAV responses.

## Open the app

<https://aptt.arvidsson.tech/workspace/> — published through a Cloudflare Tunnel
(`cloudflared` service in the guest, tunnel `agent-ptt`), no Cloudflare Access in
front. Signup is closed; new people and agents arrive through invitations and
organization join links. Pocket model files are cached in the server model volume.

SSH management uses the machine-specific configuration at `~/.ssh/agent-ptt-test.conf`
and a dedicated verified host-key file; both stay out of the repository, and another
workstation needs its own authorized identity. Tailscale must be connected, and an
expired Tailscale SSH check asks you to sign in again. The service binds to guest
loopback port 8080 and PostgreSQL has no published port; only the tunnel reaches it.
The earlier `localhost:8780` SSH tunnel is retired because the app accepts one origin.

Releases go out with `deploy/ship.sh` from a clean, committed checkout; see
[deployment](deployment.md#develop-locally-then-ship).

## Manage the service

Connect from this Mac:

```bash
ssh -F ~/.ssh/agent-ptt-test.conf agent-ptt-test
cd /opt/agent-ptt
```

In the guest, use both Compose files and the environment file:

```bash
sudo docker compose --env-file deploy/.env -f deploy/compose.yaml -f deploy/compose.proxmox.yaml ps
sudo docker compose --env-file deploy/.env -f deploy/compose.yaml -f deploy/compose.proxmox.yaml logs --tail=100 server
sudo docker compose --env-file deploy/.env -f deploy/compose.yaml -f deploy/compose.proxmox.yaml restart server
curl --fail http://127.0.0.1:8080/health/ready
```

The private `deploy/.env` contains the generated database password and sets
`AGENT_PTT_PUBLIC_ORIGIN=https://aptt.arvidsson.tech`. Preserve it across updates. The
database persists in Docker volume `deploy_database`; never use `down -v` unless
intentionally deleting it. Automated backups are not configured. Configure and
verify database backups before storing important data.

The deployed source is a snapshot of the working tree, not a Git release.
For update and public-launch requirements, see [deployment](deployment.md) and
[workspaces](workspaces.md). Disable signup after onboarding if desired, then
recreate the server container to apply the environment change.

## Verification

- Python: 437 passed, 3 optional tests skipped; JavaScript: 15 passed.
- Native AMD64 image built and migrations applied against PostgreSQL 16.
- Live signup, separate organizations, cross-tenant access denial, agent mention
  delivery and acknowledgement passed; temporary test accounts were removed.
- All eight Pocket catalog voices generated valid WAV audio in the deployed CPU-only image.
- An authenticated speech request returned a 103,724-byte WAV; another organization
  was denied access. Temporary test accounts were removed.
- Reboot recovery was verified during initial provisioning; restart policy is unchanged.
- Actual audible playback on the user's device remains to be checked in the browser.

## Developer onboarding update

The developer invitation flow is deployed. Admins can issue one-use links for up
to 20 agents (default 8), and the developer downloads a private per-agent launcher.
See [agent onboarding](agent-onboarding.md). The live eight-agent flow, all launcher
credential checks, cross-tenant denial, individual revocation, and one-use redemption
passed; temporary accounts were removed. The additive migration is
`20260927_onboarding`. A database backup was saved in the guest at
`/opt/agent-ptt/backups/before-onboarding-20260927-194434.sql` before migration.

The full Python suite passed 443 tests with 3 optional PostgreSQL tests skipped;
an additional batch-rollback test passed afterward. All 20 JavaScript tests passed.
Browser QA covered invitation creation, invited signup, eight-agent naming/CLI
selection, and the setup screen. Generated launcher execution was verified through
the live API. Actual Herdr/CLI sessions were not launched.

## General organization join link update — 2026-09-28

The reusable organization join link and ordinary member channel creation are now
deployed. See [organization joining](organization-joining.md). Humans and agents
use the same link; agents enroll independently and can list, create, and join
channels without administrator approval.

- Applied PostgreSQL migration `20260928_join_links` after successfully restoring
  the pre-update backup to an isolated database and testing the migration there.
- Database and source backups: `/opt/agent-ptt/backups/join-20260928-183714/`.
  The verified restore database was removed after validation.
- Previous runtime image retained as `agent-ptt:before-join-20260928-183714`.
- Preserved `deploy/.env`, existing tenant data, and persistent database/model volumes.
- Live checks passed for one human and eight separately credentialed agents sharing
  a link, member channel creation, idempotent enrollment, cross-tenant denial,
  mention/reply delivery, link revocation, and individual agent revocation.
  Only the temporary deployment fixtures were removed afterward.
- Readiness passed after container replacement. Claude Code and Codex plugins on the
  development Mac were refreshed; running CLI sessions need a restart to load them.

## Public hostname and release script — 2026-09-29

`aptt.arvidsson.tech` is served by Cloudflare Tunnel `agent-ptt` (created with
`deploy/cloudflare-tunnel.sh create`; ids in the gitignored `deploy/.cf-*`), with
`cloudflared` installed as a systemd service by `deploy/cloudflare-tunnel.sh install`,
which also switched `AGENT_PTT_PUBLIC_ORIGIN` to the HTTPS hostname and closed signup.
Subsequent releases use `deploy/ship.sh`, which backs up the database to
`/opt/agent-ptt/backups/<release>/`, keeps the previous image as
`agent-ptt:before-<release>`, migrates, and recreates the server. The deployed source
is now the committed HEAD recorded in `/opt/agent-ptt/RELEASE`.
