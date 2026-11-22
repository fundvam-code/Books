# Ozon Assembly and Print Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Native implementation in this session. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Ozon FBS assembly-only and assembly-plus-print actions to order cards.

**Architecture:** The server records an idempotent action and exposes it to an authorized local Ozon agent. The agent calls Ozon, verifies the assembled status, and optionally gets the label. The existing local print queue reports success and sets the printed marker.

**Tech Stack:** FastAPI, SQLAlchemy/PostgreSQL, Jinja, async httpx, macOS CUPS agent.

**Spec:** `docs/superpowers/specs/2026-11-22-ozon-assembly-print-design.md`

## Global Constraints

- Ozon credentials and all Ozon API calls stay on the local agent.
- Never invoke live Ozon actions during implementation.
- Mark a label printed only after the local print worker reports `submitted`.
- Do not interfere with existing WB assembly jobs or print jobs.

## Review Focus

- Duplicate clicks: at most one active assembly per order.
- Ozon rejects or times out after request: do not claim assembly or printed success.
- Async shipment status: fetch/verify `awaiting_deliver` before queuing a label.
- Label generation fails: order remains assembled, printed flag remains false.
- Printer fails or is absent: do not mark printed.

### Task 1: Agent assembly workflow

**Files:** `agent/app/ozon.py`, `agent/app/main.py`

- [ ] Add Ozon `ship` using SKU as `product_id`, and a bounded status check for `awaiting_deliver`.
- [ ] Process `ozon_assembly` jobs; for print mode, continue through existing label create/download/upload flow.
- [ ] Advertise Ozon write-capable cabinet keys in heartbeat and pass them when claiming jobs.

### Task 2: Server queue and print handoff

**Files:** `backend/app/db.py`, `backend/app/jobs.py`, `backend/app/schemas.py`, `backend/app/main.py`, `backend/app/printing.py`

- [ ] Store Ozon assembly intent and its optional label job.
- [ ] Claim only for an agent advertising the matching Ozon cabinet; expose assembly jobs and handle verified completion.
- [ ] On print mode, enqueue the standard label job after verified assembly and queue its PDF for the local printer when uploaded.
- [ ] Allow the successful print task to mark its linked Ozon order printed, including while cached sync is pending.

### Task 3: Dashboard actions and progress

**Files:** `backend/app/main.py`, `backend/app/templates/orders.html`, new `backend/app/templates/ozon_assembly_status.html`, `backend/app/static/dashboard.css`

- [ ] Add the exact two requested buttons to Ozon orders in `awaiting_packaging`.
- [ ] Add CSRF/Origin-protected form handling and an idempotent progress page for assembly, label, and print state.
- [ ] Show errors without marking printed; refresh the board when the workflow completes.
