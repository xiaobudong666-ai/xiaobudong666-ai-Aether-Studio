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
