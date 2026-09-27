# Current Proxmox test deployment

Deployed on 2026-09-27 to Proxmox VM **108**, `agent-ptt-test`:

- Ubuntu 24.04, 4 vCPUs, 8 GiB RAM, 60 GiB disk.
- Guest DHCP address: `192.168.30.103`.
- Proxmox access through Tailscale: `100.81.137.103`.
- Application source: `/opt/agent-ptt` in the guest.
- PostgreSQL 16 and the application run as Docker services, with reboot recovery verified.
- Speech is generated on the server with Pocket TTS; the browser plays WAV responses.

## Open the app from this Mac

Double-click `deploy/connect-test.command`, then open
<http://localhost:8780/workspace/>. Keep the Terminal window open while using the
app. Closing the tunnel does not stop the server. Tailscale must be connected;
an expired SSH identity check may require signing in again.

Signup is enabled for this private test. Create your account and organization in
the app. No permanent user account was created during deployment. Pocket model files are cached in the server model volume.

The shortcut uses the machine-specific SSH configuration at
`~/.ssh/agent-ptt-test.conf` and a dedicated verified host-key file. These files
are not in the repository. Another workstation needs its own authorized SSH
identity and configuration. The service binds to guest loopback port 8080, and
PostgreSQL has no published port. Public HTTPS access is not configured.

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
`AGENT_PTT_PUBLIC_ORIGIN=http://localhost:8780`. Preserve it across updates. The
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
