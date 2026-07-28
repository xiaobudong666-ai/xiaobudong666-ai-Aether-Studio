# Build Stage
FROM node:24-slim AS builder
WORKDIR /app

# Enable pnpm
ENV PNPM_HOME="/pnpm"
ENV PATH="$PNPM_HOME:$PATH"
RUN corepack enable

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
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
