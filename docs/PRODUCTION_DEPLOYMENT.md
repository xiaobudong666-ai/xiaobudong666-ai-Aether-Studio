# Production Deployment

## Current launch boundary

The repository provides a production-shaped Compose deployment, persistent
SQLite and media volumes, internal Sidecars, same-origin routing, health gates,
and a smoke test. No production host, SSH target, domain, or TLS endpoint has
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
- Restrict direct access to the loopback API and Worker ports.
- Confirm the selected domain is allowed by the API CORS configuration if the
  Web and API origins are intentionally separated.
- Back up both named volumes before upgrades.
- Complete one real upload, timeline placement, video-use render, SSE progress,
  and MP4 download using non-sensitive test media.
- Configure and test paid provider credentials separately; never commit them.

The release is not considered publicly launched until these target-specific
checks pass on the actual host.
