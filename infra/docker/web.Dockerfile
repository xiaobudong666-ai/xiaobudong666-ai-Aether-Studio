# Build Stage
FROM node:24-slim AS builder
WORKDIR /app

ARG VITE_OPENREEL_URL=""
ENV VITE_OPENREEL_URL=${VITE_OPENREEL_URL}

# Enable pnpm
ENV PNPM_HOME="/pnpm"
ENV PATH="$PNPM_HOME:$PATH"
RUN corepack enable && corepack prepare pnpm@10.30.3 --activate

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml tsconfig.json .eslintrc.json .gitignore ./
COPY packages/contracts ./packages/contracts
COPY packages/editor ./packages/editor
COPY apps/web ./apps/web

RUN pnpm install --frozen-lockfile
RUN pnpm --filter "@aether/contracts" build
RUN pnpm --filter "@aether/editor" build
RUN pnpm --filter "@aether/web" build

# Serve Stage
FROM nginx:alpine
COPY --from=builder /app/apps/web/dist /usr/share/nginx/html
COPY infra/docker/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
