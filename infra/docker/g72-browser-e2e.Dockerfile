# Disposable FAKE-only browser runner.
# Built in CI; at runtime it joins only the isolated internal Compose network.
# It must never be attached to the default bridge or any provider-egress network.
FROM node:24-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

RUN corepack enable && corepack prepare pnpm@10.30.3 --activate
COPY . .
RUN pnpm install --frozen-lockfile
RUN pnpm exec playwright install --with-deps chromium

CMD ["pnpm", "exec", "playwright", "test", "-c", "playwright.g72.config.ts"]
