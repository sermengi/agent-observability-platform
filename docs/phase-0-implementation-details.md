# Phase 0 (Walking Skeleton) — Implementation Decisions

Captured from planning discussion, 2026-08-25. These are decisions made ahead of implementation, refining Implementation Plan v1.0 / Phase 0. This is the first implementation phase, so there is no prior-phase codebase to reconcile against — every decision below is net-new. Decisions are locked task-by-task, following the Phase 0 task list from the implementation plan:

1. Create the repository/package foundation and environment-based configuration strategy. **(locked)**
2. Create an async FastAPI application with `/health` and one minimal placeholder run endpoint. **(locked)**
3. Add PostgreSQL to Docker Compose and verify application-to-database connectivity. **(locked)**
4. Create an initial migration/bootstrap path for the observability database. **(locked)**
5. Containerize the API service and make the stack start through documented Docker Compose commands. **(locked)**
6. Add a minimal pytest smoke suite. **(locked)**
7. Add GitHub Actions for dependency installation, configured checks/linting, tests, and Docker build. **(locked)**
8. Document local startup and one smoke API call. **(locked)**

All eight Phase 0 tasks are locked. None are implemented yet — this document is planning-only, ahead of writing any code. See Success Criteria and Status at the bottom.

---

## Task 1 — Repository/package foundation and environment-based configuration strategy

### Dependency/environment manager

- **`uv`, not Poetry.** Chosen for speed and reliability, and for consistency with the tooling already used on Project 1 (the Industrial Maintenance Agent) — same mental model, no context-switching between projects. Poetry's build/publish machinery would be unused overhead anyway, since Project 2 ships as a running service, not a published package.

### Settings structure

- **Grouped/nested Pydantic settings** — a top-level `Settings` composed of sub-models (e.g. `DatabaseSettings`, `APISettings`), namespaced via env-var prefixes (e.g. `DB__HOST`) — rather than one flat `Settings` class with every field at the top level.
- Adds a little ceremony now for a config surface that's still tiny (DB URL, API host/port, log level), but pays off starting Phase 6 onward, when Langfuse credentials, LLM judge provider credentials, and telemetry sink configuration all need to land in the same settings object without turning into a flat field soup.

### Layout & config loading (straightforward)

- `src/`-layout package (`src/obs_platform/`), keeping `tests/` cleanly external — avoids import-shadowing between the installed package and the working directory.
- `pydantic-settings` `BaseSettings`, reading from real environment variables with a `.env` file supported for local dev only.
- `.env.example` committed to the repo (documents every variable name, no real values); the actual `.env` is gitignored. Docker Compose reads the same `.env` for local orchestration.
- **Fail-fast validation**: the app refuses to start if required settings are missing or invalid — no lazy-loading, no silent defaults for required fields like the DB URL.
- **Python 3.12** as the pinned version.
- **Internal package name**: `obs_platform`.

### Test / Validation

- [ ] `uv sync` installs all dependencies from `pyproject.toml`/`uv.lock` on a clean checkout with no manual `pip` steps.
- [ ] `Settings()` raises a validation error when a required field (e.g. the database host) is missing from the environment — confirmed by a test that clears the relevant env vars before instantiating settings.
- [ ] `Settings` is composed of nested sub-models (e.g. `DatabaseSettings`, `APISettings`), not one flat class with every field at the top level — confirmed by code inspection.
- [ ] Every environment variable referenced anywhere in `Settings` has a corresponding documented entry in `.env.example`, with no real secret values committed.
- [ ] `.env` is listed in `.gitignore` and is not present in the repository's tracked files.
- [ ] The package imports correctly as `obs_platform` from an editable/installed environment under the `src/` layout, not by accident via the working directory being on `sys.path`.

---

## Task 2 — Async FastAPI application with `/health` and one placeholder run endpoint

### Placeholder endpoint response

- **`POST /v1/runs` returns `501 Not Implemented`** with a short JSON detail message, rather than a `200` with a fixed stub payload. Chosen for honesty — it doesn't pretend to succeed — and specifically to keep this deliberately-unfinished route visible so later phases don't forget to build it out, rather than letting a silently-accepted stub blend into "working" behavior.

### App structure (straightforward)

- **App factory pattern**: `create_app()` returns a configured `FastAPI` instance; a module-level `app = create_app()` for uvicorn to import. Keeps app construction test-friendly rather than relying on a single import-time global.
- **One `APIRouter` per concern** from the start (`routes/health.py`, `routes/runs.py`), included via `app.include_router(...)`.
- **`/health` is pure liveness only at this stage** — `200 {"status": "ok"}`, no DB dependency, since the app has no DB engine wired in yet at this point in the task sequence. Extended with a real DB check in Task 3.
- **`POST /v1/runs` deliberately matches the real future ingestion path** — Phase 1 defines the `ExtendedRunEvent` contract that lands here, and Phase 9/10 point Project 1's HTTP telemetry sink at this exact route — so the path/method never has to change later, only the body contract. It accepts a generic/untyped JSON body for now; no Pydantic request model, since defining that schema is explicitly Phase 1's job.
- **`/v1` prefix** applied to the run endpoint (matching later endpoint names like `/v1/analytics/failures`); `/health` stays unversioned as an infra-level check, per common convention.
- No DB/session dependency wired into the app yet.

### Test / Validation

- [ ] `GET /health` returns `200 {"status": "ok"}` with no Postgres dependency at this stage — confirmed by a test that does not require a database connection (the DB-aware version is added and re-tested in Task 3).
- [ ] `POST /v1/runs` with an arbitrary JSON body returns exactly `501`, with a JSON body containing a `detail` message — never a `200`, a `422`, or an unhandled exception.
- [ ] `create_app()` returns a distinct, independently usable `FastAPI` instance on each call — confirmed by asserting no shared mutable global state leaks between two calls (needed for clean test isolation in Task 6).
- [ ] `/health` and `/v1/runs` are registered via separate `APIRouter` instances rather than defined inline on the app object — confirmed by code inspection.
- [ ] No route or app-level dependency references a database engine or session at this point — confirmed by code inspection; Task 3 introduces the first one.
- [ ] `POST /v1/runs`'s route is mounted under `/v1`, and `/health` is not — confirmed by inspecting the registered route table.

---

## Task 3 — PostgreSQL in Docker Compose + verified app-to-DB connectivity

### Database access layer

- **SQLAlchemy 2.0 (async) + `asyncpg` driver**, not raw `asyncpg` with hand-written SQL. Chosen for consistency with the pattern already proven on Project 1 (`AsyncSession`, ORM models feeding into distinct Pydantic "Record" types via a repository layer), to avoid re-deriving repository conventions from scratch, and because it pairs naturally with Alembic for Task 4's migrations. Familiarity from Project 1 was explicitly weighed as reducing friction for schema changes as the platform's entity list grows across phases.

### Health check scope (straightforward — settled by the design doc)

- **`/health` now also checks DB connectivity** (a lightweight `SELECT 1` through the app's existing connection pool), returning `503` with a body indicating which check failed if the DB is unreachable. The design doc is explicit that the health check "depends on its own application and PostgreSQL availability, not... Langfuse or the external judge provider" — settling the question left open in Task 2 without needing a separate liveness/readiness split.

### Compose/Postgres details (straightforward)

- **Postgres 16**, a named volume for data persistence, credentials sourced from `.env` (`POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB`), and a `pg_isready`-based healthcheck.
- `api` service uses `depends_on: postgres: condition: service_healthy` so it never starts against a still-initializing Postgres.
- **Port `5432` published to the host** for local `psql`/debug access — this is a debug-scale project optimizing for inspectability, not a hardened deployment.
- **Small bounded connection retry with backoff** (~5 attempts) when the app's engine first connects — a safety net for running the API outside Compose against a dockerized Postgres that isn't fully warm yet, not a substitute for the Compose healthcheck gating.
- **DB URL assembled inside the `DatabaseSettings` group** from Task 1 (individual host/port/user/password/db-name fields plus a computed `url` property), rather than a single raw connection-string env var — keeps individual fields inspectable/overridable in tests.

### Test / Validation

- [ ] `GET /health` returns `200` when the app and Postgres are both reachable, and `503` with a body identifying the failed check when Postgres is unreachable — confirmed by a test that points the app at an invalid host/port.
- [ ] `docker compose up -d postgres` brings up a healthy Postgres container (per its `pg_isready` healthcheck) from a clean volume with no manual intervention.
- [ ] The `api` service does not start until `postgres`'s healthcheck reports healthy — confirmed by inspecting `docker-compose.yml`'s `depends_on` condition and/or observed startup ordering in `docker compose up` logs.
- [ ] `DatabaseSettings.url` is correctly assembled from its discrete host/port/user/password/db-name fields — confirmed by a unit test constructing known field values and asserting the resulting URL string.
- [ ] The bounded connection retry is exercised by a test that delays Postgres's availability and confirms the app still connects successfully rather than failing on the first attempt.
- [ ] Postgres's `5432` port is reachable from outside the Compose network (e.g. `psql` from the host) — confirmed manually or via `docker compose port postgres 5432`.

---

## Task 4 — Initial migration/bootstrap path for the observability database

### Migration tool and shape (straightforward, follows from Task 3)

- **Alembic**, using its async template (`alembic init -t async migrations`) so migrations run through the same `asyncpg`-based async engine the app already uses — no second, sync-only driver added just for migrations.
- **`env.py` imports the SQLAlchemy declarative `Base.metadata`** now, even with zero domain models defined yet, so `alembic revision --autogenerate` is ready to use the moment Phase 2 defines real ORM models. The migration DB URL reuses the Task 3 `DatabaseSettings` group rather than a separately hardcoded config.
- **Initial migration is genuinely empty.** The first `alembic upgrade head` creates only Alembic's own `alembic_version` tracking table — no placeholder domain schema. Rationale: the plan explicitly says not to introduce later-phase dependencies early, and Phase 2 is where "canonical PostgreSQL entities" get designed; a stand-in table now risks a shape Phase 2 has to unwind. Task 3's `/health` DB check already uses a driver-level `SELECT 1`, so no table is needed there either.
- **Migrations are explicit, checked-in artifacts** under `migrations/` at repo root (standard Alembic timestamp+slug naming) — per the design doc's own framing that database migrations "remain explicit engineering artifacts."
- **One-command dev experience without migrate-on-boot**: a `Makefile` target (`make up`) runs `alembic upgrade head` against the Compose-managed Postgres, then `docker compose up` — satisfying the design doc's explicit allowance for a wrapper command to combine migration and startup, without baking auto-migration into the running container's own startup code.
- **"Clean volume" test**: tear down the named Postgres volume entirely, bring Postgres up fresh, and confirm `alembic upgrade head` succeeds and lands on the single expected revision — proving the bootstrap path works from nothing, not just idempotently against an already-migrated DB.

### Test / Validation

- [ ] `alembic upgrade head` run against a freshly created, empty Postgres database succeeds and creates exactly the `alembic_version` table — no domain tables exist yet, confirmed by inspecting `information_schema.tables` afterward.
- [ ] Alembic's `env.py` uses the async template (`run_sync`/`AsyncEngine`), not a synchronous driver — confirmed by code inspection.
- [ ] `alembic revision --autogenerate` runs without error against the current (empty) `Base.metadata`, producing an empty upgrade/downgrade pair — proving the autogenerate wiring itself works before Phase 2 adds real models.
- [ ] `make up` succeeds end-to-end (migrate then Compose startup) against a completely torn-down Postgres volume (`docker compose down -v` beforehand) — the "clean volume" test.
- [ ] Alembic's migration DB URL is read from the same `DatabaseSettings` group used elsewhere in the app, not a separately hardcoded connection string — confirmed by code inspection of `env.py`.
- [ ] The single migration file under `migrations/versions/` contains no placeholder or speculative domain table — confirmed by reviewing its contents.

---

## Task 5 — Containerize the API service + documented Compose startup

### Local development workflow

- **Host-native dev, Compose reserved for integration/parity checks.** Day-to-day: `uv run uvicorn obs_platform.main:app --reload` directly on the host, against `docker compose up postgres` (DB only). The `api` service in `docker-compose.yml` stays exactly "production-shaped" — built image, no source bind-mount, no `--reload` — which is also the exact path Task 6's pytest suite, the CI Docker build, and `make up` all exercise. No `docker-compose.override.yml` / hot-reload-in-container setup was added, keeping Compose itself simple and stable across phases while the fast iteration loop happens on the host.

### Container/Compose details (straightforward)

- **Multi-stage Dockerfile**: a builder stage installs dependencies via `uv` (using uv's cache-mount pattern); a runtime stage copies only the resulting virtual environment + app source onto a slim base — keeps the shipped image free of build tooling and the uv cache.
- **Base image**: `python:3.12-slim` (Debian-based), not Alpine — avoids musl-libc/wheel-compatibility friction with `asyncpg`'s compiled extension.
- **Single `uvicorn` process** as the container's entrypoint command; no `gunicorn`/multi-worker process manager, consistent with the plan's "optimize for inspectability and completion, not realism or scale" framing.
- **`.dockerignore`** excludes `.venv`, `.git`, `__pycache__`, `.env`, test caches, etc.
- `api` service builds from the repo-root Dockerfile, reads `env_file: .env`, and depends on `postgres`'s healthcheck (Task 3).
- **DB host resolution**: `DatabaseSettings.host` defaults to the Compose service name `postgres` for containerized runs, overridden to `localhost` via `.env` for host-native `uv run` sessions — one settings field, two override points, no code branching.
- **Documented commands** extend Task 4's `make up` target rather than inventing a separate command.

### Test / Validation

- [ ] `docker compose build api` succeeds, and the final image contains the app's virtual environment but not `uv`'s build cache or the builder stage's intermediate layers — confirmed by inspecting image contents/size (e.g. `docker history`).
- [ ] The running `api` container hosts a single `uvicorn` process — no `gunicorn`, no multi-worker supervisor — confirmed via `docker top` or by inspecting the Dockerfile's `CMD`.
- [ ] `docker compose up` starts both `api` and `postgres`, and `GET /health` against the container's published port returns `200`.
- [ ] `DatabaseSettings.host` resolves to `postgres` when the app runs inside Compose and to `localhost` when run via `uv run uvicorn` on the host against `docker compose up postgres` — confirmed by checking the effective resolved host in each documented run mode.
- [ ] No `docker-compose.override.yml` and no bind-mount/`--reload` configuration exists for the `api` service — confirmed by code inspection, consistent with the host-native dev workflow decision.

---

## Task 6 — Minimal pytest smoke suite

### Test client

- **Async client** (`httpx.AsyncClient` + `ASGITransport`) driven by `pytest-asyncio` `async def` tests, not Starlette/FastAPI's sync `TestClient`. Matches the async-first style of the rest of the stack (async FastAPI, `AsyncSession`) from day one, and avoids a later migration once Phase 2+ needs async DB fixtures (seeding, querying) alongside HTTP calls in the same test function. Explicitly accepted paying more setup cost now (a `conftest.py` client fixture, `pytest-asyncio` config) for fewer changes later — the same reasoning pattern used for the Task 1 settings-structure decision.

### Scope and target (straightforward)

- **Tests run against the real Compose-managed Postgres** (`docker compose up -d postgres` before `pytest`), not an ephemeral testcontainer — directly following the plan's own Phase 0 test bullet ("API connects to Compose-managed PostgreSQL") and consistent with how Task 7's CI job is structured.
- **Layout**: `tests/` at repo root mirroring the `src/` package structure, with `conftest.py` holding the shared app/client fixtures.
- **Tests written for this phase**:
  - `test_health_ok` — with Postgres up, `GET /health` returns `200` with the expected body.
  - `test_placeholder_run_returns_501` — `POST /v1/runs` returns `501`.
  - `test_create_app` — `create_app()` returns a `FastAPI` instance with the expected routes registered.
- **Deliberately not tested**: simulating Postgres being down to exercise `/health`'s `503` path. That requires actually stopping/blocking the DB mid-test — failure-injection territory the design doc explicitly places out of v1 scope beyond targeted unit/integration tests.
- **No DB isolation/cleanup infrastructure yet** — there's no domain schema to pollute until Phase 2, so this is deferred until real tables exist.

### Test / Validation

- [ ] Every test in `tests/` is an `async def` function using `httpx.AsyncClient`/`ASGITransport` — no test relies on the synchronous `TestClient` — confirmed by code inspection.
- [ ] `test_health_ok` passes only when run with Postgres reachable via Compose, and fails clearly (not silently skips) if Postgres isn't up — confirming the documented prerequisite is actually required, not incidental.
- [ ] `test_placeholder_run_returns_501` asserts the exact status code `501` for `POST /v1/runs` given an arbitrary JSON body.
- [ ] `test_create_app` asserts `create_app()` returns a `FastAPI` instance exposing both the `/health` and `/v1/runs` routes.
- [ ] No test in this suite simulates Postgres being unreachable — confirmed by code inspection, consistent with the decision to defer failure-injection-style testing.
- [ ] The full suite passes against a freshly migrated (clean-volume) database, not only a database left over from prior manual testing.

---

## Task 7 — GitHub Actions CI (install, checks/lint, tests, Docker build)

### Job structure

- **Single sequential job** (install → lint → typecheck → test → docker build), not multiple parallel jobs. Matches the "keep it simple, don't over-engineer" posture appropriate for a solo, debug-scale build where CI wall-clock time and per-check UI granularity aren't yet a real constraint; can be split into parallel jobs later if that ever changes.

### Tooling and step design (straightforward)

- **`ruff`** for both lint and format — fast, single-tool, pairs naturally with the `uv`-based tooling theme already established.
- **`mypy`** for type checking — mature ecosystem/CI-plugin support for a Pydantic-heavy codebase; `pyright` remains an easy swap later if preferred for editor-parity reasons.
- **`astral-sh/setup-uv`** action with its built-in dependency caching for installs.
- **Tests step uses a native GitHub Actions Postgres service container** (`services: postgres: image: postgres:16`), not a full `docker compose up`. The plan lists "tests" and "Docker build" as separate concerns — Phase 0's own test bullet reads "Smoke tests **and** Docker build pass locally and in CI" — so they're implemented as two distinct steps rather than one combined integration run.
- **Docker-build step does more than `docker build .`**: it also runs the built image (linked to a Postgres service) and curls `/health`, since Phase 0's test list specifically requires "/health succeeds from the containerized API" — a successful image build alone doesn't prove that.
- **Triggers**: `push` and `pull_request` against `main`.

### Test / Validation

- [ ] The workflow file defines a single job with sequential steps in the order install → lint → typecheck → test → docker build — confirmed by inspecting `.github/workflows/*.yml`.
- [ ] `ruff check` and `ruff format --check` both run in the lint step and fail the job on any violation.
- [ ] `mypy` runs as a distinct step and fails the job on any type error.
- [ ] The test step uses a `services: postgres:` block rather than invoking `docker compose up`, and `pytest` runs directly via `uv run pytest` on the runner.
- [ ] The Docker-build step builds the `api` image, runs it linked to a Postgres service, and asserts `GET /health` returns `200` from the running container — not merely that `docker build` exits zero.
- [ ] The workflow's `on:` block triggers on both `push` and `pull_request` targeting `main`.
- [ ] A deliberately introduced failure in each stage (a lint violation, a type error, a failing test, and a `/health` non-`200` from the built container), tested one at a time and then reverted, each independently fails the corresponding CI step — confirming the checks actually gate rather than merely run.

---

## Task 8 — Document local startup and one smoke API call

### Documentation scope and shape (straightforward)

- **Single root `README.md`** — no `docs/` folder yet. Phase 11 is explicitly where the README gets built out (architecture, evaluation philosophy, failure taxonomy, Project 1 relationship, etc.); Phase 0 only adds a minimal "quick start" section for later phases to extend, not restructure.
- **The documented quick-start path is the full Compose path** (`git clone` → `cp .env.example .env` → `make up`), not the host-native dev loop — matching the success criterion that a fresh clone can start a healthy stack using documented commands, with no assumption the reader already has `uv` set up.
- **A short secondary "Local development" note** documents the host-native loop from Task 5 (`docker compose up postgres` + `uv run uvicorn ... --reload`) for active coding.
- **The one documented smoke API call is `GET /health`**, not `POST /v1/runs`. `/health` is the only endpoint that proves something real (app and DB both reachable); documenting the placeholder's deliberate `501` as "the" smoke call would send the wrong signal. The doc shows the exact `curl` command and the expected `200 {"status": "ok", ...}` body.
- **Deliberately not included yet**: lint/test run instructions, a CI badge, or a contributor guide — outside this task's literal scope, easy to add later without restructuring anything written now.

### Test / Validation

- [ ] Following `README.md`'s quick-start steps exactly (`git clone` → `cp .env.example .env` → `make up`) on a machine with only Docker and `make` installed (no pre-existing `uv` environment) results in a running, healthy stack — confirmed by a fresh-clone walkthrough.
- [ ] The exact `curl` command shown in the README against `/health` returns the exact response shape documented there.
- [ ] The commands in the README's "Local development" note (`docker compose up postgres` + `uv run uvicorn ... --reload`) work as written, resulting in a live-reloading local server connected to the Compose-managed Postgres.
- [ ] The README contains no Phase 11-scope content (architecture diagrams, evaluation philosophy, failure taxonomy, Project 1 relationship narrative) — confirmed by review, keeping this task's documentation scoped to what Phase 0 actually asked for.

---

## Success Criteria

- [ ] A fresh clone can start a healthy FastAPI + PostgreSQL stack using one documented command (`make up`), with no undocumented manual steps (Tasks 1, 3, 4, 5, 8).
- [ ] `/health` reflects both application liveness and real PostgreSQL connectivity, returning `503` (not a false `200`) when the DB is unreachable (Task 3).
- [ ] The public service shell — routes, versioning prefix, Compose service boundary — is already shaped the way later phases expect (`POST /v1/runs` at its final path, `DatabaseSettings` ready for real credentials, `Base.metadata` wired for autogeneration), so later phases add internals rather than reshaping the deployment boundary (Tasks 1, 2, 4).
- [ ] CI is green — lint, type-check, an async pytest suite against a service-container Postgres, and a Docker build-and-run-time `/health` check — before any platform intelligence is added (Task 7).
- [ ] The async test-client and nested-settings patterns adopted now are the same patterns later phases keep building on, rather than one-off Phase 0 choices that get revisited (Tasks 1, 6).

## Status

All eight Phase 0 tasks are locked. Phase 0 planning is complete. Next: proceed to implementation, or move on to Phase 1 (Telemetry Contract & Debug Fixtures) planning discussion.