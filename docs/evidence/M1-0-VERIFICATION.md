# M1-0 MoneyPrinterTurbo Sidecar Adapter Integration Verification

This document provides the canonical verification evidence and integration documentation for the **M1-0** milestone.

---

## 1. Pinned Upstream Specifications
- **Upstream Repository**: [harry0703/MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo)
- **Pinned Version Tag**: `v1.2.7`
- **Pinned Commit Hash**: `b09b0b6bc7fa05e60d3d5f3dfd68377e68e4de80`
- **License**: MIT
- **Upgrade Strategy**: Upgrading must be done by explicitly updating the `build.context` URL or image tag in `infra/docker/docker-compose.yml`, re-running the unit tests, and verifying same-origin endpoint responses.

---

## 2. Decoupled Same-Origin API Interfaces
All communication to the MoneyPrinterTurbo sidecar is strictly isolated within the internal bridge network. The public API exposes standard proxy-like endpoints preserving Aether Studio's same-origin topology:

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/moneyprinter/health` | Probes sidecar connectivity by pinging `GET /` |
| `GET` | `/api/moneyprinter/capabilities` | Returns verified/available features or unknown/unavailable flags |
| `POST` | `/api/moneyprinter/generate` | Submits automated video generation task (validated via schemas) |
| `GET` | `/api/moneyprinter/status/{task_id}` | Queries active task execution state and handles exceptions |

---

## 3. Real vs Mock Boundaries
- **Adapter/Contract Ready**: The API client interfaces, HTTP retries, exponential backoffs, timeouts, and error mappings are fully integrated and verified via strict unit/integration tests.
- **Credential & Config Boundaries**: No production keys or personal credentials (e.g., Pexels API key, LLM key, TTS key) are committed. They are managed through standard externalized environment configurations (`.env.example`).
- **Media Pipeline Status**: Actual video rendering, subtitles, and style transfer remain mock capabilities (M0-0 behavior is preserved), and `video-use` is strictly deferred to **M2**. No fake success status or fallback video is forged when the sidecar is disconnected.

---

## 4. Verification & Validation Commands

### 4.1 Dependency Installation & Hygiene
```bash
# Install Node packages
pnpm install --frozen-lockfile

# Run ESLint validation
pnpm lint

# Compile and Build Web static bundle
pnpm build
```

### 4.2 Python Test Verification (Pytest)
Run the 32-test backend python test suites (both standard and moneyprinter adapter coverage):
```bash
# Run FastAPI Backend test suite
PYTHONPATH=apps/api .venv/bin/python -m pytest apps/api/test_main.py apps/api/test_moneyprinter.py -v

# Run Worker Backend test suite
PYTHONPATH=apps/worker .venv/bin/python -m pytest apps/worker/test_worker.py apps/worker/test_moneyprinter.py -v
```

### 4.3 Frontend End-to-End Flow (Playwright)
```bash
PATH=/app/.venv/bin:$PATH pnpm e2e
```

### 4.4 Docker Compose Validation
Validate syntax and configuration of the multi-service topology:
```bash
docker compose -f infra/docker/docker-compose.yml config
```

---

## 5. Docker Integration Topology & Health Checks
The environment is orchestrated using `infra/docker/docker-compose.yml`:
- **Service Name**: `moneyprinter-sidecar`
- **Isolation**: Runs inside a dedicated bridge network named `aether-net`.
- **Health Checks**: Uses lightweight Python-based health probing requesting `http://127.0.0.1:8080/` inside the sidecar.
- **Dependency Pipeline**: API and Worker depend on `moneyprinter-sidecar` being fully healthy before starting.

---

## 6. Risks & Rollback Procedure
- **Connection Failures**: If the sidecar goes down, the API adapter triggers a graceful degradation status response (`status: "failed", degraded: True`), reporting zero progress and avoiding forging fake files/URLs.
- **Rollback Procedure**: In case of regression or sidecar failure, revert the commit changes on the branch, restart the containers with `docker compose down -v && docker compose up -d`, and verify core M0-0 operations remain unharmed.
