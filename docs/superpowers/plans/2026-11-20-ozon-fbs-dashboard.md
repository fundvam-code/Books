# Ozon FBS Dashboard Implementation Plan

> **For agentic workers:** Use this plan task by task. Steps use checkbox syntax for tracking.

**Goal:** Build a Docker-hosted dashboard for new and packed Ozon FBS orders across two seller accounts, with order details and official shipping-label printing through a local agent.

**Architecture:** A FastAPI server stores order snapshots and queued jobs in PostgreSQL. A local Docker agent polls the server over outbound HTTPS, reads Ozon credentials only on the user's PC, calls Seller API, and returns order data or temporary label PDFs. The dashboard is server-rendered with Jinja2 and HTMX.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, PostgreSQL 16, Jinja2, HTMX, httpx, Docker Compose v2, Caddy.

**Spec:** `docs/superpowers/specs/2026-11-20-marketplace-dashboard-design.md`

## Global Constraints

- Ozon API credentials stay on the local PC and never enter Git or the Netherlands server.
- Support two local credential profiles, represented as Cabinet 1 and Cabinet 2.
- Use Ozon FBS statuses `awaiting_packaging` and `awaiting_deliver` for the two order columns.
- Use Ozon's current asynchronous FBS label generation flow: `POST /v3/posting/fbs/package-label/create`, then `POST /v2/posting/fbs/package-label/get` until the PDF is ready.
- The local agent makes only outbound HTTPS requests; do not require inbound ports on the PC.
- Caddy Basic Auth protects dashboard and PDF routes; the agent API uses a separate bearer secret; HTTPS is required for public deployment.
- No recipient details are shown in order cards by default; label PDFs are temporary and access-controlled.
- Keep the stack to one API/web service, PostgreSQL, Caddy, and one local agent; do not add Redis or a separate SPA.

## Review Focus

- **Agent disconnect during a job:** leave the job retryable, show a stale/offline indicator, and avoid losing a label request; verify by stopping the agent during a label job and confirming the job can be claimed again.
- **Ozon rate limits or transient API failures:** back off, preserve last successful order snapshot, and show when it was last updated.
- **One invalid account key while the other works:** report the failure against the correct cabinet without hiding the healthy cabinet's data.
- **Same posting number across two accounts:** scope order identity and updates by both account ID and posting number; verify identical posting numbers from two profiles remain separate rows.
- **Label not ready or invalid status:** keep the request pending/retryable and return a clear status; do not display a broken or empty PDF.

## File Structure

| Path | Responsibility |
|---|---|
| `backend/app/main.py` | FastAPI application and route registration |
| `backend/app/config.py` | Server configuration and secret loading |
| `backend/app/db.py` | PostgreSQL engine and session lifecycle |
| `backend/app/models.py` | Account, order, job, and label metadata tables |
| `backend/app/schemas.py` | Server/agent request and response types |
| `backend/app/routes/dashboard.py` | Authenticated order pages and PDF download |
| `backend/app/routes/agent.py` | Agent job claim and result submission |
| `backend/app/services/jobs.py` | Job creation, leasing, retry, and completion |
| `backend/app/services/orders.py` | Upsert and query order snapshots |
| `backend/app/templates/orders.html` | Two-account order list and status columns |
| `backend/app/templates/order_detail.html` | Order detail page |
| `agent/app/main.py` | Agent polling loop and job dispatch |
| `agent/app/config.py` | Local credentials and server connection settings |
| `agent/app/ozon.py` | Ozon Seller API calls and response mapping |
| `agent/app/server.py` | Authenticated outbound calls to the dashboard server |
| `compose.server.yaml` | Server, PostgreSQL, and Caddy services |
| `compose.agent.yaml` | Local agent service and external `.env` loading |
| `Caddyfile` | HTTPS reverse proxy and Basic Auth protection for dashboard/PDF routes |
| `.env.server.example` | Safe server configuration template |
| `.env.agent.example` | Safe local agent configuration template |
| `README.md` | Local setup, remote deployment, and print workflow |

## Tasks

### Task 1: Application skeleton and container configuration

**Files:**
- Create: `pyproject.toml`
- Create: `backend/Dockerfile`
- Create: `agent/Dockerfile`
- Create: `compose.server.yaml`
- Create: `compose.agent.yaml`
- Create: `Caddyfile`
- Create: `.env.server.example`
- Create: `.env.agent.example`
- Modify: `.gitignore`
- Modify: `README.md`

**Interfaces:**
- Server container listens on `8000` internally; Caddy exposes HTTPS on `443`.
- Agent configuration uses `DASHBOARD_URL`, `AGENT_SHARED_SECRET`, `OZON_ENV_FILE`.
- `compose.agent.yaml` loads the existing Ozon credentials through the configured external env-file path and never copies it into the image.
- Server configuration uses `DATABASE_URL`, `PUBLIC_DOMAIN`, `DASHBOARD_USERNAME`, `DASHBOARD_PASSWORD_HASH`, and `AGENT_SHARED_SECRET`.

- [ ] Create the Python project metadata with runtime floor Python `>=3.12` and runtime dependencies FastAPI, SQLAlchemy, psycopg, Jinja2, httpx, and uvicorn.
- [ ] Create server and agent Dockerfiles that run as non-root and install only their declared project dependencies.
- [ ] Define server Compose services for API, PostgreSQL 16, and Caddy with a persistent database volume and health checks; Caddy applies Basic Auth to all proxied dashboard and PDF routes.
- [ ] Define local-agent Compose configuration that receives server URL/shared secret from `.env.agent` and loads the external Ozon `.env` using `OZON_ENV_FILE`.
- [ ] Add ignore rules for real `.env*`, local database/PDF volumes, Python caches, and logs; keep only `*.example` templates tracked.
- [ ] Document required environment values and startup commands for local agent and remote server.
- [ ] Check `docker compose -f compose.server.yaml config` and `docker compose --env-file .env.agent -f compose.agent.yaml config` with placeholder environment files; confirm rendered configs contain no API key values.

### Task 2: Persistent models and server job queue

**Files:**
- Create: `backend/app/config.py`
- Create: `backend/app/db.py`
- Create: `backend/app/models.py`
- Create: `backend/app/schemas.py`
- Create: `backend/app/services/jobs.py`
- Create: `backend/app/main.py`

**Interfaces:**
- `AgentJob`: `{id: UUID, kind: "sync_orders" | "label_pdf", account_key: "1" | "2", payload: dict}`.
- `POST /api/agent/jobs/claim` returns one leased `AgentJob` or `204` when none is available; requires agent bearer authentication.
- `POST /api/agent/jobs/{job_id}/complete` accepts `{status: "succeeded" | "retryable_error" | "permanent_error", result: dict | null, error: str | null}`.
- `PUT /api/agent/jobs/{job_id}/label` accepts a PDF body with `Content-Type: application/pdf` and stores it in the private label volume.
- Job lease timeout is 90 seconds; active jobs renew the lease every 30 seconds; retryable jobs use exponential delay capped at 5 minutes.

- [ ] Add SQLAlchemy models for `Order`, `AgentJob`, and `AgentHeartbeat`; make order uniqueness `(account_key, posting_number)`.
- [ ] Add database session lifecycle, startup migration mechanism, and settings validation for missing secrets.
- [ ] Implement atomic job claim with a 90-second lease so concurrent agent requests cannot claim the same job.
- [ ] Implement lease renewal for an active job; reject renewal when the job is completed or belongs to a different agent.
- [ ] Implement job completion and retry state transitions using the schema above.
- [ ] Add the authenticated agent routes and return `204` when the queue is empty.
- [ ] Verify two concurrent claim requests cannot return the same job and an expired lease becomes claimable again using a focused local verification script or database session.
- [ ] Verify an active label job remains leased after renewal and cannot be claimed by another agent.

### Task 3: Local agent connection and health status

**Files:**
- Create: `agent/app/config.py`
- Create: `agent/app/server.py`
- Create: `agent/app/main.py`
- Modify: `backend/app/routes/agent.py`
- Modify: `backend/app/models.py`

**Interfaces:**
- Agent polls `POST /api/agent/jobs/claim` over HTTPS with `Authorization: Bearer <AGENT_SHARED_SECRET>`.
- Agent renews an active job with `POST /api/agent/jobs/{job_id}/renew` every 30 seconds.
- `POST /api/agent/heartbeat` accepts `{agent_version: str, account_keys: ["1", "2"], observed_at: datetime}`.
- Dashboard agent is online when heartbeat age is at most 120 seconds.

- [ ] Implement agent configuration validation; fail closed if server URL, shared secret, or both Ozon account credentials are missing.
- [ ] Implement outbound polling with bounded connection/read timeouts and exponential reconnect delay up to 60 seconds.
- [ ] Implement heartbeat updates and a dashboard-readable online/offline state.
- [ ] Restrict the server agent routes to the configured shared secret and redact authorization headers from logs.
- [ ] Verify with the agent process stopped that the server marks it offline within 120 seconds, then returns online after restart and heartbeat.

### Task 4: Ozon FBS order synchronization

**Files:**
- Create: `agent/app/ozon.py`
- Create: `backend/app/services/orders.py`
- Create: `backend/app/routes/dashboard.py`
- Modify: `backend/app/services/jobs.py`
- Modify: `backend/app/schemas.py`
- Modify: `backend/app/models.py`

**Interfaces:**
- Agent uses `POST /v4/posting/fbs/list` to fetch FBS postings with statuses `awaiting_packaging` and `awaiting_deliver` for each local account profile.
- Ozon request headers are `Client-Id` and `Api-Key`; they are constructed only in the agent process.
- Agent submits normalized snapshots to `POST /api/agent/jobs/{job_id}/complete`.
- Order snapshot fields: `account_key`, `posting_number`, `status`, `created_at`, `shipment_date`, `products[]` with `name`, `offer_id`, `sku`, `quantity`, `price`, plus nullable `warehouse` and `delivery_method`.
- Server enqueues account sync jobs once per minute; dashboard reports last successful sync timestamp per account.

- [ ] Map the two local `OZON_*_1` / `OZON_*_2` groups to stable account keys `1` and `2` without returning secret fields to the server.
- [ ] Implement paginated FBS order retrieval for the two selected statuses using the current Seller API schema.
- [ ] Normalize optional Ozon fields without failing the whole account sync when a nonessential field is absent.
- [ ] Upsert snapshots by `(account_key, posting_number)` and retain last successful data if Ozon returns an error.
- [ ] Add a once-per-minute scheduler that creates at most one pending sync job per account.
- [ ] Verify both accounts populate independently, the two status groups are separated, and an invalid key for one account leaves the other account visible.

### Task 5: Dashboard order list and details

**Files:**
- Modify: `backend/app/routes/dashboard.py`
- Create: `backend/app/templates/base.html`
- Create: `backend/app/templates/orders.html`
- Create: `backend/app/templates/order_detail.html`
- Modify: `backend/app/main.py`

**Interfaces:**
- `GET /orders?account=all|1|2` renders account tabs and two status columns.
- `GET /orders/{account_key}/{posting_number}` renders order details.
- Display only orders whose status is `awaiting_packaging` or `awaiting_deliver`.
- Authentication is required for every page.

- [ ] Configure Caddy Basic Auth using `DASHBOARD_USERNAME` and `DASHBOARD_PASSWORD_HASH`; confirm Caddy protects order pages and label PDFs.
- [ ] Render Cabinet 1/Cabinet 2/All tabs and the two Russian status columns.
- [ ] Render order number, order time, shipment deadline, product name, offer ID/SKU, quantity, price, warehouse, and delivery method where present.
- [ ] Show per-account last-sync time and agent online/offline state.
- [ ] Verify the page remains usable when optional Ozon fields are missing, an account has no orders, or the agent is offline.

### Task 6: Ozon label generation and PDF print flow

**Files:**
- Create: `backend/app/services/labels.py`
- Modify: `backend/app/routes/dashboard.py`
- Modify: `backend/app/services/jobs.py`
- Modify: `agent/app/ozon.py`
- Modify: `agent/app/main.py`
- Modify: `backend/app/config.py`
- Modify: `compose.server.yaml`

**Interfaces:**
- `POST /orders/{account_key}/{posting_number}/label` creates one `label_pdf` agent job and returns a pending status page.
- `GET /labels/{job_id}` returns label state (`pending`, `ready`, or `failed`) for the authenticated dashboard.
- Agent calls `POST /v3/posting/fbs/package-label/create`, then polls `POST /v2/posting/fbs/package-label/get` until ready or failed.
- Agent uploads the completed PDF to `PUT /api/agent/jobs/{job_id}/label`; the server saves it in the private label volume at `{job_id}.pdf`; `GET /labels/{job_id}.pdf` requires dashboard authentication.
- Label files expire after 24 hours.

- [ ] Add a print action only for an order in `awaiting_deliver` status.
- [ ] Implement asynchronous Ozon label creation/retrieval with bounded retry count and clear permanent/retryable error mapping.
- [ ] Return the completed PDF as `application/pdf` through an authenticated route; do not log or commit PDF bytes.
- [ ] Add cleanup of PDF files older than 24 hours and persist only label-job status and expiry metadata.
- [ ] Verify an eligible order produces a readable PDF, an ineligible status shows a useful message, and the PDF route rejects unauthenticated access.

### Task 7: Remote deployment and operator guide

**Files:**
- Modify: `README.md`
- Modify: `compose.server.yaml`
- Modify: `compose.agent.yaml`
- Modify: `Caddyfile`
- Create: `docs/deploy.md`
- Create: `docs/operations.md`

**Interfaces:**
- Remote startup: `docker compose --env-file .env.server -f compose.server.yaml up -d`.
- Local agent startup: `docker compose --env-file .env.agent -f compose.agent.yaml up -d`.
- Public deployment requires a domain, DNS pointing to the NL server, HTTPS, dashboard login, and a locally allowed egress IP for Ozon.

- [ ] Document first-time setup, secret creation, environment file paths, update/restart commands, and backup of PostgreSQL.
- [ ] Document how to verify agent connection, order freshness, per-account API failures, and label PDF expiry.
- [ ] Confirm the production Compose configuration does not mount the local Ozon `.env` on the server and the agent Compose file has no inbound port mapping.
- [ ] Confirm the user can start the server and local agent from a clean clone by following only the docs.

## Execution Order

Complete Tasks 1–3 first to establish infrastructure and the outbound agent channel; Task 4 supplies order data; Task 5 displays it; Task 6 adds labels; Task 7 finishes deployment and operator guidance. The dashboard is not ready for use until Task 6 works end to end, while Tasks 1–5 provide a working read-only order view.
