# Aether Studio — M0-0 Engineering Baseline

Aether Studio is an AI-powered anime/cartoon short video editor. This repository houses the completed **M0-0 Engineering Baseline**, establishing a robust, modular, testable, and containerized workspace foundation for subsequent M1/M2/M3 phase developments.

This project is completely independent. It does **not** read, reference, or modify `Ai-Eos` as per engineering constraints.

---

## 1. Architectural Blueprint & Design Decision Record (DDR)

### Monorepo Strategy (pnpm Workspace)
- **Monorepo Root**: Managed via `pnpm` workspaces for maximum dependency deduplication, speed, and clean code boundaries.
- **`packages/contracts`**: Standardizes domain models, shared Data Transfer Objects (DTOs), Zod validators, standard SSE event schemas, and the **Canonical Timeline v1.1**.
- **`packages/editor`**: Contains visual-editor-agnostic abstract adapters (`IMaterialLoader`, `ICanvasAdapter`, `ITimelineController`). This strictly prevents tight coupling of core business capabilities with any single third-party timeline or rendering engine.
- **`apps/web`**: Responsive three-column React + TypeScript + Vite workbench.
- **`apps/api`**: FastAPI-based Web API facilitating project metadata, SQLite WAL storage, and Server-Sent Events (SSE) streaming.
- **`apps/worker`**: Independent Python daemon handling processing tasks (FFmpeg proxies, AI-driven stylization, crash recoveries).

### Accurate Frame Timing (`RationalTime`)
Unlike floating-point decimals or flat millisecond integers, frame-accurate systems cannot afford cumulative precision drift. We enforce `RationalTime` math representing time as a rational fraction:
$$\text{Time} = \frac{\text{value}}{\text{timescale}}$$
For instance, a standard anime timeline uses a timescale of `24000` (allowing perfect fraction increments for 24fps film rates, i.e., 1000 value ticks per frame). All duration and offset computations are resolved using exact common denominators, completely eliminating precision drifting over long sequences.

### High-Performance DB (`SQLite WAL`)
- **WAL (Write-Ahead Logging)**: Configured via SQL pragmas (`PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;`). This enables simultaneous high-speed reads and writes, protecting database consistency during continuous auto-saving without blocking visual rendering tasks.

### Auto-Save & Optimistic Concurrency Control
- **Automatic Revision Logs**: Every visual change (adding clips, updating materials) increments the project's `revision` count.
- **Optimistic Locking**: When updating a project (`PUT /projects/{id}`), the front-end includes an `expectedRevision`. If the DB revision has progressed beyond `expectedRevision` (due to multiple tabs or parallel processing), a `409 Concurrency Conflict` is returned. The UI gracefully alerts the user, refetches, and merges states, preventing silent visual data loss.

---

## 2. Directory Structure & File Inventory

```
aether-studio-monorepo/
├── .github/
│   └── workflows/
│       └── ci.yml             # Github Actions: setup, compile checks, linting, tests
├── apps/
│   ├── api/                   # Python FastAPI backend
│   │   ├── app/
│   │   │   ├── main.py        # Routes, optimistic locks, SSE events streaming
│   │   │   ├── database.py    # DB WAL & connection configurations
│   │   │   ├── models.py      # SQLAlchemy DB project layout
│   │   │   └── schemas.py     # Pydantic schemas mapping contracts
│   │   ├── requirements.txt
│   │   └── test_main.py       # Pytest API integration tests
│   ├── web/                   # React + TS + Vite Workbench
│   │   ├── src/
│   │   │   ├── components/    # AssetLibrary, CanvasPreview, PropertyInspector, Timeline
│   │   │   ├── test/          # Test setups
│   │   │   ├── App.tsx        # Workbench connector
│   │   │   ├── main.tsx
│   │   │   └── index.css      # Dark-themed dashboard stylesheets
│   │   ├── package.json
│   │   └── vite.config.ts
│   └── worker/                # Python processing daemon
│       ├── app/
│       │   ├── main.py        # Event loop & health server
│       │   ├── ffmpeg_adapter.py # Video transcoding skeleton (480p proxy)
│       │   ├── ai_provider.py # AI cartoon stylizer & STT subtitle skeleton
│       │   └── recovery.py    # Interrupted task recovery adapter
│       ├── requirements.txt
│       └── test_worker.py     # Pytest unit tests
├── infra/
│   └── docker/
│       ├── docker-compose.yml # Dev orchestration (API + Worker + Web)
│       ├── api.Dockerfile
│       ├── worker.Dockerfile
│       └── web.Dockerfile     # Multi-stage built React static app in Nginx
├── packages/
│   ├── contracts/             # Shared Timing Math and schemas
│   │   ├── src/
│   │   │   ├── index.ts       # RationalTime calculations, 480p constants
│   │   │   └── schemas.ts     # Zod timelines and project schemas
│   │   └── __tests__/         # Vitest Timing unit tests
│   └── editor/                # Abstract editor adapters (loaders, controllers)
├── package.json
├── pnpm-workspace.yaml
└── README.md                  # This documentation
```

---

## 3. Quick Start Guide & Orchestration

### Prerequisites
- Node.js >= 22 & `pnpm` >= 10
- Python >= 3.12
- Docker & Docker Compose (optional for local container testing)

### Local Dev Setup (Bare Metal)

1. **Install Workspace Dependencies**:
   ```bash
   pnpm install
   ```

2. **Set up Python Virtualenvs**:
   - **API**:
     ```bash
     python3 -m venv apps/api/.venv
     ./apps/api/.venv/bin/pip install -r apps/api/requirements.txt
     ```
   - **Worker**:
     ```bash
     python3 -m venv apps/worker/.venv
     ./apps/worker/.venv/bin/pip install -r apps/worker/requirements.txt
     ```

3. **Start Back-end Services**:
   - **API (Port 8000)**:
     ```bash
     cd apps/api
     ./.venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
     ```
   - **Worker (Port 8001)**:
     ```bash
     cd apps/worker
     PYTHONPATH=. .venv/bin/python3 -m app.main
     ```

4. **Start Front-end Web (Port 5173)**:
   ```bash
   cd apps/web
   pnpm dev
   ```

### Running with Docker Compose

Spin up the entire unified mesh (FastAPI + Worker + Nginx Web) instantly:
```bash
docker compose -f infra/docker/docker-compose.yml up --build
```
- Navigate to http://localhost to access the workbench.
- API is exposed on http://localhost:8000.
- Worker is exposed on http://localhost:8001.

---

## 4. Test Suite Execution

To run all front-end, contracts, and timing tests using Vitest:
```bash
pnpm test
```

To run Pytest tests for API and Worker:
```bash
# API Tests
cd apps/api && PYTHONPATH=. .venv/bin/pytest

# Worker Tests
cd apps/worker && PYTHONPATH=. .venv/bin/pytest
```

---

## 5. M0-0 Acceptance Record

| Feature Requirement | Status | Verification Command / Proof |
| :--- | :--- | :--- |
| **Monorepo Workspace** | Passed | Workspace mapping with pnpm links compiled successfully. |
| **RationalTime Accuracy** | Passed | Tested via `packages/contracts/__tests__/rationalTime.test.ts`. 100% precision. |
| **FastAPI + WAL** | Passed | Verified with health-checks indicating journal_mode=WAL. |
| **Optimistic Lock** | Passed | Tested via `apps/api/test_main.py`. Returns 409 Conflict as specified. |
| **SSE Tasks Updates** | Passed | Verified with live EventSource listener inside React components. |
| **FFmpeg/AI Adapters** | Passed | Deployed in `apps/worker` with standalone pytest verification. |
| **Web Editor layout** | Passed | Verified via Playwright screenshots illustrating complete responsive panels. |

---

## 6. Known Limitations & Next-Phase (M1) Recommendations

### Known Limitations (M0-0 Baseline)
- **Mock Transcoding**: FFmpeg and AI interfaces in the worker utilize mocked processing loops. In M1, these must be integrated with real `subprocess` bindings and actual vendor endpoints.
- **Client Cache Resiliency**: If the back-end drops connection, the frontend automatically falls back to an interactive client-side mockup mode. Real-time offline persistence (e.g. IndexedDB syncing) is deferred to M1.

### Recommendations for M1
1. **Dynamic Track Management**: Implement UI controls for creating, reordering, and deleting arbitrary tracks directly.
2. **Real FFmpeg Decoding**: Bind standard webcodecs or FFmpeg WASM inside the canvas monitor to support real visual rendering of the 480p proxy segments.
3. **Webhooks for Tasks**: Integrate FastAPI background queues with a Redis Broker (e.g. Celery / RQ) to distribute workloads securely to several worker nodes.
