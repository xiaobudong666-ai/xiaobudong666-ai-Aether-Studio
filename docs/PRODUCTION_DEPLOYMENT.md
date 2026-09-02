# Production Deployment

## Current launch boundary

The repository provides a production-shaped Compose deployment, persistent
SQLite and media volumes, internal Sidecars, same-origin routing, health gates,
authenticated tenant isolation, a persistent Worker queue, and a full-stack
smoke test. No production host, SSH target, domain, or TLS endpoint has
been provided, so this document does not claim that a public machine was
changed.

Both upstream Sidecars are fetched at fixed full commit SHAs during their
image builds. Their Dockerfiles verify `HEAD` before removing Git metadata.
MoneyPrinterTurbo intentionally uses the default Debian and PyPI endpoints
instead of the upstream Dockerfile's location-specific mirror retry chain.

## Prepare configuration

Run these commands on the intended Docker host from a clean checkout of the
accepted `main` commit:

```bash
cp infra/docker/.env.example infra/docker/.env
```

Keep `COMPOSE_PROJECT_NAME` stable after the first launch; it names the
persistent volumes. Set `AETHER_HTTP_BIND=127.0.0.1` when Caddy, Nginx, or a
managed ingress terminates TLS on the host. Set it to `0.0.0.0` only when port
access is controlled elsewhere.

The first start requires all of the following:

- Set `AETHER_BOOTSTRAP_ADMIN_PASSWORD` to a unique password of at least 12
  characters. The initial email defaults to `admin@aether.local` and can be
  changed with `AETHER_BOOTSTRAP_ADMIN_EMAIL`.
- Generate `AETHER_WORKER_TOKEN` with at least 32 random bytes, for example
  `openssl rand -hex 32`. API and Worker must receive the same value.
- Keep `AETHER_COOKIE_SECURE=true` and terminate HTTPS before public traffic.
  Set it to `false` only for a local HTTP smoke environment.
- Review the initial tenant quotas. They are persisted at first bootstrap:
  50 projects, 50 GiB storage, 2 concurrent renders, and 36,000 render seconds
  per month by default.

The first authenticated startup also migrates a legacy SQLite database by
adding tenant and owner columns, then assigns pre-existing projects to the
bootstrap owner. Back up `sqlite-db` before that first upgraded launch. Later
restarts do not overwrite persisted users, roles, tenant IDs, quotas, sessions,
or render tasks.

`ELEVENLABS_API_KEY` is optional. Leaving it empty disables transcription
without fabricating a result. MoneyPrinterTurbo generation likewise requires
its own licensed provider configuration before generation can be accepted as
operational. `VITE_OPENREEL_URL` is also optional and is consumed only while
building the Web image; leave it empty to omit the fallback-editor link.

## Governed private Provider canary boundary

The base Compose stack remains fail-closed: its committed Provider mode is
`disabled`, it does not mount a target configuration, and the unauthenticated
MoneyPrinter Sidecar is not attached to `aether-net`. Only the Worker shares
the internal `provider-control` network with the Sidecar. The Sidecar alone is
attached to `provider-egress`; API, Web and video-use have no Provider route.

The optional `docker-compose.provider-canary.yml` override is not a deployment
approval. It only defines a future, separately authorized target-local bind:

- `MONEYPRINTER_CONFIG_FILE` must be an absolute, repository-external regular
  file owned by the operator and have permissions no broader than `0600`;
- the file is mounted only at `/MoneyPrinterTurbo/config.toml` on the Sidecar
  and is read-only;
- no secret value is copied into Compose environment, a command, a label, a
  healthcheck, a build argument, an image layer or an Aether evidence record;
- the Worker receives only `credentialState=PRESENT`,
  `networkIsolation=ENFORCED` and `canaryProfile=private-one-task-v1` after the
  structural preflight passes.

From a clean checkout of an owner-approved exact commit, the only action that
is safe without a separate real-execution approval is the default preflight:

```bash
MONEYPRINTER_CONFIG_FILE=/absolute/private/config.toml \
AETHER_CANARY_ENV_FILE=/absolute/private/aether-provider-canary.env \
AETHER_CANARY_LLM_PROVIDER=<approved-provider> \
AETHER_CANARY_MODEL=<approved-model> \
AETHER_CANARY_MATERIAL_SOURCE=<approved-material-source> \
AETHER_CANARY_VOICE_PATH=edge \
AETHER_GENERATION_TENANT_ID=<approved-tenant-id> \
AETHER_GENERATION_CONFIG_VERSION_ID=<published-config-version-id> \
AETHER_GENERATION_POLICY_HASH=<published-policy-hash> \
AETHER_CANARY_PROVIDER_BUDGET_EVIDENCE=PRESENT \
AETHER_CANARY_MATERIAL_LICENSE_EVIDENCE=PRESENT \
infra/docker/provider-canary.sh preflight --approved-sha <approved-main-sha>
```

Preflight emits only a sanitized status. It rejects a dirty or mismatched
checkout, unsafe target file, invalid TOML, DEBUG logging, automatic upload,
Redis, proxy/base URL, broad artifact prefix, multiple Provider/material
sources, or a policy other than one concurrent task, one monthly request, one
output and at most ten generated seconds.

The `arm`, `run` and `disarm` subcommands exist for a later owner-approved
private execution. They remain locked unless the separate execution approval,
owner UID, private-target assertion, authenticated owner session, exact SHA,
one synthetic request and external state-file controls are all present.
`run` persists the one-POST boundary before transmission, never automatically
replays an ambiguous submission, and always invokes fail-closed disarm. Disarm
sets the owner kill switch, removes the Sidecar container/read-only mount and
recreates the Worker with Provider mode and all proof fields disabled. Do not
invoke these subcommands during coding, CI, review or deployment approval.

## Build and start

```bash
docker compose \
  --env-file infra/docker/.env \
  -f infra/docker/docker-compose.yml \
  config --quiet

docker compose \
  --env-file infra/docker/.env \
  -f infra/docker/docker-compose.yml \
  up -d --build --wait --wait-timeout 600
```

Run the release smoke gate against the configured listener:

```bash
AETHER_SMOKE_BASE_URL=http://127.0.0.1:8088 \
  infra/docker/production-smoke.sh
```

Repository CI additionally executes `authenticated-smoke.py` through the
public Nginx route. It signs in, uploads a file larger than the former 1 MiB
limit, creates a four-second Canonical Timeline with a two-second gap, waits for
the Worker-owned persistent task, downloads the MP4, and verifies its duration.

Do not add `--volumes` when stopping or upgrading the production stack. A
normal stop preserves both `sqlite-db` and `video-use-media`:

```bash
docker compose \
  --env-file infra/docker/.env \
  -f infra/docker/docker-compose.yml \
  down
```

## Required operator checks before public traffic

- Terminate HTTPS at a host reverse proxy or managed ingress.
- Verify login, logout, owner/editor/viewer permissions, and tenant isolation
  with non-production test accounts before inviting users.
- Confirm `AETHER_WORKER_TOKEN` is non-empty and is not exposed to the Web
  container or browser bundle.
- Restrict direct access to the loopback API and Worker ports.
- Confirm the selected domain is allowed by the API CORS configuration if the
  Web and API origins are intentionally separated.
- Back up both named volumes before upgrades.
- Verify project, storage, concurrent-render, and monthly-render quotas match
  the intended commercial plan. Environment values seed only the first tenant;
  persisted values remain authoritative.
- Complete one real upload, timeline placement, video-use render, SSE progress,
  and MP4 download using non-sensitive test media.
- Configure and test paid provider credentials separately; never commit them.

The release is not considered publicly launched until these target-specific
checks pass on the actual host.
