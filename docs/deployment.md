# Production server service

Run Agent PTT as one continuously running container, backed by managed PostgreSQL,
behind an HTTPS reverse proxy or load balancer. Agents connect to that server's
public URL; they do not need to run their own server.

This is a shared multi-tenant deployment: each customer creates an organization
in the same application. Organizations share the server and database while
permissions and tenant-owned data are scoped by `org_id`. Do not deploy one copy
per organization. See [the tenancy model](workspaces.md).

The repository supplies a non-root, PostgreSQL-only Docker image (unused libSQL
drivers are excluded), an explicit production entrypoint,
health endpoints, and a Compose service for a Linux VM on GCP, AWS, or another
provider. The local `start.command` stays a development launcher.

## First deployment on a VM

Provision a Linux VM with Docker Engine and the Compose plugin, and enable Docker
at boot using the host's service manager. Provision a PostgreSQL database and an
application database user. Restrict database access to the application network.

From the repository root on the VM:

```bash
cp deploy/.env.example deploy/.env
chmod 600 deploy/.env
# Edit deploy/.env: DATABASE_URL and AGENT_PTT_PUBLIC_ORIGIN must be real values.
docker compose -f deploy/compose.yaml build
# Apply migrations once, before starting the service.
docker compose -f deploy/compose.yaml run --rm --no-deps server alembic upgrade head
docker compose -f deploy/compose.yaml up -d

docker compose -f deploy/compose.yaml ps
docker compose -f deploy/compose.yaml logs --tail=100 server
curl --fail http://127.0.0.1:8080/health/ready
```

Compose `command` overrides the image CMD, so the migration command runs Alembic
instead of the web server. Back up existing databases before upgrading and verify
restoration. Migrations are a release step, not a command run by every replica.

The VM service binds only to `127.0.0.1:8080`. Configure a host reverse proxy to
terminate TLS and forward HTTP and WebSocket upgrades to that address. Only
expose HTTPS externally. If using a cloud load balancer directly, change the
port binding deliberately and restrict ingress to its network/security group.
Set `AGENT_PTT_PUBLIC_ORIGIN` to the exact HTTPS browser origin. Cookie and Origin
checks use that configured origin; forwarded headers are not trusted by default.

Account signup is closed in the example. For initial setup, enable
`AGENT_PTT_SIGNUP=1`, recreate the container, create the initial owner over HTTPS,
then disable signup and recreate it again. Invited users can still create accounts using their one-use invitation;
public account creation stays closed.

## Lifecycle and storage

`restart: unless-stopped` restarts an exited container and resumes it when Docker
starts after a host reboot, unless it was deliberately stopped. This requires
Docker itself to start at boot. A health check alone does not restart an unhealthy
Compose container; alert on unhealthy status and investigate or restart it.
[Docker service configuration](https://docs.docker.com/reference/compose-file/services/)
describes restart and health-check behavior.

```bash
# Restart the current image/configuration:
docker compose -f deploy/compose.yaml restart server
# Apply changed environment or a newly built image:
docker compose -f deploy/compose.yaml up -d --force-recreate server
# Intentionally stop the service:
docker compose -f deploy/compose.yaml stop server
```

PostgreSQL holds accounts, organizations, credentials, channels, messages, and
mention receipts. Never store the production database in the container's writable
layer. The runtime requires a `postgresql+psycopg` URL and hosted mode; it refuses
the local SQLite fallback. Pass database credentials through your provider's
secret manager in managed deployments; `.env` is the VM example only and is
excluded from Git and the Docker build context.

Speech is generated on the server using Pocket TTS. The hosted image installs
CPU PyTorch on Linux and excludes local speaker drivers. Model downloads are cached
in the `model-cache` volume at `/home/app/.cache/huggingface`; allow outbound access
for initial downloads. Warm the model before accepting speech traffic to avoid
cold-start request timeouts. The runtime runs as UID 10001 and logs to stdout/stderr.
Set `AGENT_PTT_SERVER_SPEECH=0` to disable speech while keeping text chat available.

## Provider choices

- **GCP Compute Engine or AWS EC2:** use the Compose setup above for the first
  release. This is the most direct match for the current long-running process.
- **AWS ECS:** publish the same image to your registry and run an ECS service with
  desired count 1, managed PostgreSQL, secret-injected environment, an HTTPS load
  balancer, and container health checks. ECS maintains the desired running count.
  See [ECS services](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs_services.html).
- **GCP Cloud Run:** the image listens on `0.0.0.0:$PORT`, but Cloud Run needs its
  own startup/liveness probes and resource configuration. Its local filesystem
  is ephemeral; use external PostgreSQL. Evaluate this separately before adopting serverless
  scaling; see the [container contract](https://docs.cloud.google.com/run/docs/container-contract).

Build for the target CPU architecture before publishing. For example, use
`docker buildx build --platform linux/amd64 -t REGISTRY/agent-ptt:RELEASE --push .`
for an x86 deployment when building on an ARM laptop. Native local image testing
does not validate a different target architecture.

Keep one container and one Uvicorn worker initially. Rate limits are in-process; adding replicas requires shared coordination and
capacity testing. Avoid overlapping replicas during releases until that work is
done, accepting a brief maintenance window. This setup does not promise high
availability or zero-downtime deployments.

`GET /health/live` confirms the HTTP process responds. `GET /health/ready` also
checks the database and returns 503 if unavailable. Both endpoints are public,
return only status, and work in hosted mode; no tenant content or credentials
are exposed. Configure database connection timeouts, such as the example's
`connect_timeout=5`. Readiness does not validate every schema migration. Model availability is independent of database readiness; test actual synthesis separately.

## Release boundary

The deployment files prepare service operation; they do not provision cloud
resources or deploy anything automatically. Choose the provider, domain, database,
and secret store before connecting a CI/CD release pipeline. Finish the account
verification/recovery, shared quotas, and operational checks in the
[workspace launch checklist](workspaces.md) before a public launch.

## Validation

The current Pocket-only code passed 437 Python tests (3 optional PostgreSQL tests
skipped), 15 JavaScript tests, Ruff, and diff checks. The native AMD64 image was
built on the Proxmox VM with CPU-only PyTorch; all eight catalog voices generated
valid WAV audio before activation. See [the deployment handoff](proxmox-testing.md).

## Proxmox test VM

Use a Linux VM with Docker Engine and Compose. A starting test allocation is
4 vCPUs, 8 GB RAM, and a 60 GB disk; this leaves headroom for testing.
This includes headroom for server-side Pocket TTS model memory; benchmark concurrent speech before sizing production.
Enable VM startup in Proxmox and Docker startup in the guest for reboot recovery.

The additional `deploy/compose.proxmox.yaml` supplies PostgreSQL on a private
Compose network, a persistent database volume, and a one-shot migration service.
It publishes no database port. For a new test installation:

```bash
cp deploy/proxmox.env.example deploy/.env
chmod 600 deploy/.env
# Replace POSTGRES_PASSWORD with a generated hex secret before proceeding.
# Temporarily set AGENT_PTT_SIGNUP=1 to create your initial owner.
docker compose --env-file deploy/.env -f deploy/compose.yaml -f deploy/compose.proxmox.yaml build
docker compose --env-file deploy/.env -f deploy/compose.yaml -f deploy/compose.proxmox.yaml up -d
```

The `--env-file` argument is required for the password interpolation in the
Compose override. Keep the generated password stable after database initialization;
changing the environment does not rotate the existing PostgreSQL password.

For the example's localhost origin, open an SSH tunnel from your workstation:
`ssh -L 8080:127.0.0.1:8080 YOUR_VM`, then visit `http://localhost:8080/workspace/`.
For other users on the LAN, configure an HTTPS reverse proxy and change the
public origin accordingly. The server stays bound to the VM's loopback interface
until that routing is deliberately configured. The initial test uses the app's
PostgreSQL user as the database owner; production deployments should separate
migration and runtime privileges.

After onboarding, disable signup and recreate the server. Back up PostgreSQL
before updates; do not use `docker compose down -v` unless intentionally deleting
the test database. Take database-consistent backups in addition
to VM snapshots. These commands prepare the stack; they do not create a VM or
configure Proxmox networking.
