# TaskPilot

A task management backend with an AI planning step. Users create tasks through a REST API; an AI workflow can analyse a task and propose an execution plan. The plan is **reviewed and approved by the user** before anything is executed.

Built as a portfolio project to show backend fundamentals (API design, SQL, transactions, caching, idempotency, concurrency, testing, containers, CI) with one small, real agentic component (LangGraph).

## Overview

```
Task -> AI planning -> Plan review -> Approval -> Execution (step tracking) -> Completion
```

- The AI never changes a task by itself. It only produces a *proposal* (a plan with steps).
- Execution only moves task/plan/step **status**. It does not run code, shell commands or SQL.
- `AI_MODE=mock` needs no API key, so tests, CI and demos are free and deterministic.

## Features

- JWT auth with bcrypt password hashing, roles `USER` / `ADMIN`
- Task CRUD with pagination, filtering, sorting and a validated status state machine
- AI plan generation (LangGraph) running as a background task, returning `202 Accepted`
- Plan approval / rejection / execution with step-by-step progress
- Idempotent plan creation (`Idempotency-Key`) and a database-enforced "one active plan per task" rule
- Redis cache for task lists with graceful fallback when Redis is down
- Request IDs, structured log context, Prometheus metrics, health/readiness probes
- Docker, Docker Compose, Kubernetes manifests, GitHub Actions CI -> GHCR

## Architecture

A modular monolith: one deployable FastAPI app with clear internal layers.

```
              Client / Swagger
                     |
              NGINX Ingress (k8s)
                     |
                FastAPI app
   +-----------------+------------------+
   | routes  (HTTP only, no logic)       |
   | services (rules, transactions)      |---> ai/ (LangGraph planner + tools)
   | repositories (SQLAlchemy queries)   |            |
   +--------+----------------+-----------+            v
            |                |                  LLM (or mock)
            v                v
       PostgreSQL          Redis (cache only)
```

Request flow for the main use case:

```
POST /tasks/{id}/plans  (Idempotency-Key: abc)
  route -> PlanService.request_plan
            1. same key already used?  -> return that plan (no new work)
            2. fail GENERATING plans older than the timeout
            3. active plan exists?     -> 409
            4. INSERT plan(GENERATING) -> COMMIT
  route -> 202 + plan, then BackgroundTasks:
            analyze (tools) -> create_plan (LLM) -> validate -> save
            save = one transaction: steps + status PENDING_APPROVAL
```

## System Design

| Question | Answer |
|---|---|
| Why a modular monolith? | One developer, one deployable, one database transaction boundary. Layers (routes / services / repositories / ai) keep seams where a service could be split out later, without paying for network calls, distributed tracing and eventual consistency now. |
| Why PostgreSQL? | The data is relational (users -> tasks -> plans -> steps) and correctness matters: foreign keys, unique constraints, transactions, `SELECT ... FOR UPDATE` and partial unique indexes are all used here. |
| Why Redis? | A fast shared cache that works across API replicas (an in-process dict would not). It is optional: every cache call is wrapped so a Redis failure only costs speed. |
| Why BackgroundTasks, not Celery? | LLM calls take seconds, so the request should not wait. For one process and one job type, `BackgroundTasks` gives the async behaviour with zero extra infrastructure. The trade-off is durability (see Reliability). |
| Why human approval? | LLM output is untrusted and can be wrong. The plan is a proposal; a person decides. It also means execution always runs a reviewed, immutable plan. |
| Why idempotency? | Clients retry on timeouts. Without a key, a retry would create a second plan and another LLM bill. |

### Scaling story

- **~100 users:** what is here. One or two API replicas, one Postgres, optional Redis.
- **~10,000 users:** more API replicas (the app is stateless; JWT needs no session store), Postgres connection pooling (PgBouncer), indexes already cover the hot queries, Redis cache absorbs list reads. Move plan generation to a real queue (e.g. Celery/RQ/SQS + worker pool) so generation load is isolated from API latency and survives restarts.
- **~1,000,000 users:** Postgres read replicas for list endpoints, partition/archive old tasks and plans, cache per-user stats, rate-limit AI endpoints per user and budget LLM spend, split the AI worker into its own service. Only now would extracting services be worth the cost.

### Reliability

| Failure | What happens |
|---|---|
| PostgreSQL down | API returns `503` JSON (never a stack trace). `/health` stays 200 (process is alive), `/ready` returns 503 so Kubernetes stops routing traffic. Recovers by itself when the DB returns. |
| Redis down | Cache calls fail fast (300 ms timeouts), are logged as warnings and skipped; requests are served from PostgreSQL. `/ready` reports `redis: unavailable` but stays ready. |
| LLM API down / bad output | HTTP retries with backoff for transient errors (tenacity). Invalid output gets one retry with the validation error as feedback. After that the plan becomes `FAILED` with a reason and the task is free for a new attempt. |
| Background task fails | The exception is caught, logged, the failure metric increments and the plan is marked `FAILED`. |
| Process crashes mid-generation | The plan stays `GENERATING`. The next generate request fails plans older than `PLAN_GENERATION_TIMEOUT_SECONDS` before checking for conflicts, so the task is never stuck. A late result from the old run is discarded. |

## API

Swagger UI: `http://localhost:8000/docs`

| Method | Path | Notes |
|---|---|---|
| POST | `/auth/register` | 201. Always creates a `USER` |
| POST | `/auth/login` | OAuth2 form (`username` = email). Returns a JWT |
| GET | `/users/me` | Current user |
| POST | `/tasks` | 201 |
| GET | `/tasks` | `page`, `page_size` (max 100), `status`, `priority`, `sort` (`created_at`, `updated_at`, `title`; prefix `-` for descending) |
| GET / PATCH / DELETE | `/tasks/{id}` | PATCH is a partial update; DELETE returns 204 |
| POST | `/tasks/{id}/plans` | **202** + `Idempotency-Key` header; **409** if the task already has an active plan |
| GET | `/tasks/{id}/plans` | Newest first |
| GET | `/plans/{id}` | Plan with steps |
| POST | `/plans/{id}/approve` · `/reject` | Only from `PENDING_APPROVAL`, otherwise 409 |
| POST | `/plans/{id}/execute` | Only from `APPROVED`. Task -> `IN_PROGRESS`, first step starts |
| POST | `/plans/{id}/steps/{step_id}/complete` | Completes the current step and starts the next. The last one completes the plan and the task |
| GET | `/health` · `/ready` · `/metrics` | Liveness · readiness · Prometheus |

Status codes in use: 200, 201, 202, 204, 401 (missing/invalid token), 403 (not your resource), 404, 409 (state conflict), 422 (validation), 503 (database unavailable), 500 (generic message + `request_id`).

**Why PATCH, not PUT?** Clients usually change one field (like `status`). PATCH sends only that field; PUT would require the whole resource and risk overwriting concurrent edits.

**Why 202?** Generation is not finished when the response is sent; the plan resource exists and is being produced. **Why 409?** The request is valid but conflicts with current state (an active plan already exists).

### State machines

Task: `TODO -> IN_PROGRESS | BLOCKED | CANCELLED`, `IN_PROGRESS -> TODO | BLOCKED | COMPLETED | CANCELLED`, `BLOCKED -> TODO | IN_PROGRESS | CANCELLED`, `CANCELLED -> TODO`, `COMPLETED` is final.

Plan: `GENERATING -> PENDING_APPROVAL | FAILED`, `PENDING_APPROVAL -> APPROVED | REJECTED`, `APPROVED -> EXECUTING -> COMPLETED`.
(The spec's `DRAFT` is replaced by `GENERATING`: the row exists while the AI works.)

## Database Design

```
users 1 --- * tasks 1 --- * plans 1 --- * plan_steps
```

| Table | Key columns |
|---|---|
| users | id (UUID), email (unique), password_hash, role, created_at |
| tasks | id, user_id -> users, title, description, status, priority, created_at, updated_at |
| plans | id, task_id -> tasks, status, summary, failure_reason, idempotency_key, created_at, approved_at |
| plan_steps | id, plan_id -> plans, title, description, position, status, created_at |

Indexes and why:

- `users.email` unique: login lookup, duplicate prevention
- `tasks(user_id)`, `tasks(user_id, status)`: every list query filters by owner, usually also by status
- `plans(task_id)`: list plans of a task, FK lookups
- `plans(task_id, idempotency_key)` unique: retry-safe creation
- `plans(task_id) WHERE status IN ('GENERATING','PENDING_APPROVAL','APPROVED','EXECUTING')` unique, partial: at most one active plan per task, enforced by the database
- `plan_steps(plan_id, position)` unique: ordered, no duplicate positions

Statuses are stored as `VARCHAR`, not native PG enums, so adding a state is a code change instead of an `ALTER TYPE` migration. IDs are UUIDs so they are not guessable or enumerable. `ON DELETE CASCADE` removes plans and steps with their task.

### Transactions

Every service method is one transaction: do the work, `commit()`, and on failure the session is rolled back (the session is closed per request, which rolls back anything uncommitted).

- **Create plan:** insert plan row, commit. A unique-constraint failure is caught, rolled back and turned into 409 (or the earlier plan, for the same key).
- **Save generated plan:** lock the plan row, check it is still `GENERATING`, insert all steps and flip the status in one commit. If saving fails, no half-written plan exists; the plan is marked `FAILED`.
- **Approve / reject / execute / complete step:** `SELECT ... FOR UPDATE` on the plan, check the state, change the plan (and task/step) together, commit. Two concurrent approvals serialise on the lock: one wins, the other gets 409.

## AI Planning Flow

```
START -> analyze -> create_plan -> validate -> save -> END
                        ^             |
                        +-- invalid --+   (one retry, with the validation error as feedback)
```

- **analyze** calls three read-only tools backed by the real database: `get_task_context()`, `get_related_tasks()` (the user's other open tasks), `get_user_preferences()` (preferred plan size, inferred from the user's previously accepted plans). Their output becomes the `constraints` given to the model.
- **create_plan** calls the LLM through a small `LLMClient` interface. `MockLLM` is deterministic for tests and local development, while `GeminiLLM` uses Google's Gemini API with structured JSON output. The generated plan is validated with Pydantic before it can be saved.

- **validate** parses the raw text with Pydantic (`PlanOutput`): non-empty summary, 1-15 steps, titles present, positions exactly `1..n`.
- **save** persists steps and moves the plan to `PENDING_APPROVAL` in one transaction.

Tools are invoked by the `analyze` node (deterministic, backend-driven), not chosen by the model via function-calling. That keeps behaviour testable and the model's power limited to producing text. The model has no way to execute code, shell commands or SQL.

Using a real model:

```bash
AI_MODE=llm LLM_API_KEY=... LLM_MODEL=gpt-4o-mini   # LLM_BASE_URL for other providers
```

## Caching

- **What:** pages of `GET /tasks` for a regular user, keyed by user + filters + sort + page, TTL 30 s. Admin queries span all users and are not cached.
- **Invalidation:** each user has a version number in Redis. Cache keys include it. Any task create/update/delete or plan execution step bumps the version (`INCR`, O(1)), making all of that user's cached pages unreachable; the old keys expire by TTL. No key scanning.
- **Redis unavailable:** every call is wrapped; errors are logged and treated as cache misses/no-ops. Redis is never required for correctness.

## Background Processing

`POST /tasks/{id}/plans` commits the `GENERATING` plan, returns 202, and FastAPI runs the generation after the response using its own DB session. Limits: it lives in the API process, so a crash loses in-flight jobs (handled by the timeout rule above) and generation competes with request handling for the worker. At higher scale this becomes a queue plus worker pool; the service method that does the work (`PlanService.generate`) would not change.

## Authentication

- `POST /auth/login` verifies the bcrypt hash and returns a signed JWT (`sub` = user id, `exp`). Clients send `Authorization: Bearer <token>`.
- Each request decodes the token, then loads the user from the database, so role changes and deleted users take effect immediately.
- **Authentication** = who you are (token). **Authorization** = what you may do: users can access their own tasks and plans, admins can access all. Accessing someone else's resource is 403.
- Registration always creates a `USER`. Create admins with `scripts/seed.py`.

## Idempotency

`POST /tasks/{id}/plans` accepts `Idempotency-Key`. The key is stored on the plan, unique per task. A retry with the same key returns the original plan (202) and starts no new generation. If two identical requests race, the unique constraint decides the winner and the loser returns the winner's plan.

## Concurrency

"One active plan per task" (states `GENERATING`, `PENDING_APPROVAL`, `APPROVED`, `EXECUTING`) is checked in the service for a friendly 409 and **enforced by a partial unique index** in the database for the race where two requests pass the check together. No distributed locks. Plan state changes use row locks (`FOR UPDATE`).

## Testing

```bash
pip install -r requirements-dev.txt
pytest
ruff check .
```

73 tests, no network or LLM required. They use SQLite and a fake Redis; the background job runs inside the test client, so a GET after the POST sees the finished plan. Covered: auth, task CRUD/pagination/filtering/sorting/authorization, plan lifecycle, idempotency, duplicate generation, state transitions, Redis fallback, mock planner, output validation, retry-once behaviour, tools against the database, the real LLM client (HTTP mocked), request IDs, metrics, error handling.

Not covered by the SQLite suite: `FOR UPDATE` row locking and real concurrent requests (SQLite ignores both). Run the stack on PostgreSQL to exercise them.

## Docker

```bash
docker compose up --build
```

Starts `api`, `postgres` and `redis`. The API container runs `alembic upgrade head` and then uvicorn as a non-root user (UID 10001). Swagger: http://localhost:8000/docs

```bash
docker compose exec api python -m scripts.seed   # admin@example.com / demo@example.com
```

Compose uses development-only credentials. Set `JWT_SECRET` in your shell or a `.env` file for anything else.

## Kubernetes

Layout: `Namespace`, API `Deployment` (2 replicas, rolling update, startup/readiness/liveness probes, requests and limits, non-root, no privilege escalation), `Service`, PostgreSQL `StatefulSet` + headless `Service` with a PVC, Redis `Deployment` + `Service`, `ConfigMap`, `Ingress` (NGINX).

```bash
docker build -t taskpilot:local .

cp k8s/secret.example.yaml k8s/secret.yaml        # edit values; the file is git-ignored
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/secret.yaml
kubectl apply -k k8s/

kubectl -n taskpilot get pods
kubectl -n taskpilot port-forward svc/taskpilot-api 8000:80    # then open /docs
```

Docker Desktop needs the NGINX ingress controller for the Ingress (`kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/cloud/deploy.yaml`); then use `http://taskpilot.localhost/docs`.

To use the image from GHCR, change `newName`/`newTag` under `images:` in `k8s/kustomization.yaml`.

Migrations run on container start. Several replicas starting together are safe: `alembic/env.py` takes a PostgreSQL advisory lock, so the others wait and then find the schema current.

## CI/CD

`.github/workflows/ci.yml`

- Pull requests and pushes: checkout, Python 3.12, install, `ruff check .`, `pytest`
- Push to `main`, after tests pass: build the Docker image and push `ghcr.io/<owner>/taskpilot:latest` and `:<git sha>` (owner lowercased)
- No automatic deployment to Kubernetes

## Project Structure

```
app/
  main.py            app factory, middleware, routers
  config.py          environment-based settings
  api/               dependencies.py + routes/ (HTTP only)
  services/          business rules, transactions, state machines
  repositories/      SQLAlchemy queries
  db/                engine/session, models
  schemas/           Pydantic request/response models
  ai/                graph.py (LangGraph), planner.py (LLM + validation), tools.py, state.py
  cache/redis.py     task list cache
  core/              security, exceptions, logging + request ID middleware
  observability/     Prometheus metrics + middleware
alembic/             migrations
scripts/seed.py      admin + demo data
tests/               pytest suite
k8s/                 manifests (kustomize)
```

## Setup

Local development (API on the host, dependencies in Docker):

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

docker compose up -d postgres redis
cp .env.example .env            # set JWT_SECRET (32+ chars)

alembic upgrade head
python -m scripts.seed          # optional
uvicorn app.main:app --reload
```

Demo flow with curl:

```bash
B=http://localhost:8000
curl -X POST $B/auth/register -H 'content-type: application/json' \
     -d '{"email":"me@example.com","password":"password123"}'
TOKEN=$(curl -s -X POST $B/auth/login -d 'username=me@example.com&password=password123' | jq -r .access_token)
H="Authorization: Bearer $TOKEN"

TASK=$(curl -s -X POST $B/tasks -H "$H" -H 'content-type: application/json' \
       -d '{"title":"Prepare a data engineering interview project","priority":"HIGH"}' | jq -r .id)

PLAN=$(curl -s -X POST $B/tasks/$TASK/plans -H "$H" -H 'Idempotency-Key: demo-1' | jq -r .id)   # 202
curl -s $B/plans/$PLAN -H "$H"                          # PENDING_APPROVAL with steps
curl -s -X POST $B/plans/$PLAN/approve -H "$H"
curl -s -X POST $B/plans/$PLAN/execute -H "$H"          # task -> IN_PROGRESS
# repeat for each step id returned in the plan:
curl -s -X POST $B/plans/$PLAN/steps/<step_id>/complete -H "$H"
```

## Design Decisions

- **Sync SQLAlchemy and sync routes.** FastAPI runs them in a thread pool. It is simpler to read and test than async SQLAlchemy, and the workload is I/O-light.
- **Service + repository layers.** Services own rules and transactions; repositories own queries. Routes stay thin. Repositories exist only for the three aggregates that need them.
- **Tools run in the `analyze` node.** Predictable and testable; the model only produces text.
- **Plans are immutable after approval.** Execution runs exactly what was reviewed.
- **403 for someone else's resource** (and 404 for unknown ids). Ids are UUIDs, so existence leaks little; returning 404 for both is the stricter alternative.
- **JWT secret is required** (no default) and must be 32+ characters, so a forgotten secret fails at startup instead of running insecurely.
- **No Celery, Kafka, microservices or vector DB.** None solves a problem this project has.

## Future Improvements

- Durable job queue + worker for plan generation
- Refresh tokens and token revocation; rate limiting on login and AI endpoints
- Per-user LLM usage budgets
- Task statistics endpoint (cached)
- Model-driven tool calling with an allow-list of read-only tools
- Run the test suite against PostgreSQL in CI (service container)
- HPA and a PodDisruptionBudget for the API
