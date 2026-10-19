---
title: Architecture
type: architecture
sources:
  - app/*.py
  - domain/config/*.py
  - domain/models/*.py
  - domain/ontology/*.py
  - domain/ontology/*.yaml
  - domain/rules/*.py
  - integrations/ai/*.py
  - integrations/suppliers/*.py
  - presentation/*.py
  - services/*.py
  - webapp/*.py
  - webapp/admin_offline_recovery.py
  - webapp/admin_recovery_cli.py
  - webapp/user_mfa_recovery.py
  - webapp/user_recovery_cli.py
  - webapp/static/labour-review-demo.html
  - webapp/static/labour-review-demo.js
  - requirements-web.txt
last_verified: 2026-10-20
---

# Architecture

## Overall Architecture

CarbitOy has a Python library for automotive work intake, parts and labour decisions, and quote presentation. A FastAPI application provides a separate authenticated workshop inquiry queue. The library workflows and web queue share domain models and pricing services, but the web routes do not invoke the library's intake, proposal, or final-quote workflows.

The domain includes multi-work `WorkPlan` and `LabourPlan` contracts. The application layer also has an editable `WorkPlanSnapshot` and projection adapters. [Work Plans](./concepts/work-plans.md) owns those contracts; [Labour Planning](./features/labour-planning.md) owns original-time selection and review. Neither the snapshot adapter nor the domain planner is wired into the current web inquiry routes.

### Dependency direction

```text
app/ --------------------------> domain/models/, domain/rules/
  |                                      ^             |
  +--> integrations/ai/ -----------------+             +--> domain/ontology/

services/ ---------------------> domain/models/ ------> domain/config/
presentation/ -----------------> domain/models/, domain/rules/, integrations/ai/
integrations/suppliers/ -------> domain/models/
domain/ontology/ -------------> domain/models/

webapp/ -----------------------> services/, domain/models/
  +--> webapp/auth_repository.py -------> auth SQLite
  +--> webapp/database.py --------------> business SQLite
```
These arrows summarize imports and storage access, not a single call chain. `app/` uses interpreter interfaces supplied by callers; `presentation/` can use a supplied AI presenter. The web layer uses its own repositories and authorization boundary. Business operations verify workshop scope against the business store while retaining the auth session binding from the separate auth store; see [Workshop Identity](./concepts/workshop-identity.md).

## Modules

### Domain models — `domain/models/`

Pydantic records define vehicles, work, part requirements, supplier offers and candidates, labour time and plans, proposals, selections, quotes, and presentation data. [Work and Part Data](./concepts/work-and-part-data.md) owns the single-work and part vocabulary; [Work Plans](./concepts/work-plans.md) owns the multi-work and review records.

### Workshop configuration — `domain/config/`

Pair-replacement configuration records confirmed practices that planning may consult. It supplies descriptive scope rules rather than changing requested work or quantities; [Labour Planning](./features/labour-planning.md) explains when a matching full-group time can be selected.

### Domain ontology — `domain/ontology/`

YAML vocabulary and lookup code normalize part and axle terms for intake and matching. Its canonical terms are described in [Work and Part Data](./concepts/work-and-part-data.md).

### Domain rules — `domain/rules/`

Rules convert interpreted intake, build and rank supplier candidates, price labour, record a human selection, construct final quotes, and project presentation data. [Customer Intake](./features/customer-intake.md), [Proposal Decisions](./features/proposal-decisions.md), and [Final Quote and Presentation](./features/final-quote.md) own those flows.

### Supplier integrations — `integrations/suppliers/`

Supplier adapters search for offers and a collector combines adapter results through shared offer models. [Supplier Adapters](./patterns/supplier-adapters.md) owns this interface and collection pattern.

### AI integrations — `integrations/ai/`

Interpreter and presenter contracts separate application calls from fake and OpenAI implementations. Their respective uses are covered by [Customer Intake](./features/customer-intake.md) and [Final Quote and Presentation](./features/final-quote.md).

### Application workflows — `app/`

Library helpers orchestrate intake, a single-work demo proposal, and a separate final-quote call. `work_plan_contract.py` holds editor revision data, and `work_plan_adapters.py` projects complete editor data into domain plans without granting approval. The demo proposal remains a single-work path; see [Proposal Decisions](./features/proposal-decisions.md) and [Work Plans](./concepts/work-plans.md).

### Presentation — `presentation/`

Formatters render raw-proposal and quote text. A quote presentation workflow also delegates a work description to an injected presenter; [Final Quote and Presentation](./features/final-quote.md) owns that output path.

### Application services — `services/`

Services calculate labour cost and inclusive VAT splits, assemble a single-work raw proposal from caller-supplied inputs, and build and review domain labour plans. [Proposal Decisions](./features/proposal-decisions.md) owns proposal assembly; [Labour Planning](./features/labour-planning.md) owns the plan's selection and human review sequence.

### Web application — `webapp/`

`main.py` creates the FastAPI app, registers auth and draft routes, serves the browser, and exposes workshop inquiry routes. `http_access.py` checks the explicit route inventory and protects business requests. The auth modules manage passwords, MFA, sessions, and CSRF; `admin_offline_recovery.py` and `user_mfa_recovery.py` provide trusted Python recovery procedures. `admin_recovery_cli.py` and `user_recovery_cli.py` are executable local consoles for IT-admin and business-user MFA recovery, respectively. [Employee Authentication](./features/employee-authentication.md) owns the login, recovery commands, and failure behavior.

The workshop modules establish organizations, workshops, memberships, roles, and active context. Business operations receive a server-resolved employee and workshop context, while SQLite operations recheck authorization and inquiry scope. [Workshop Access](./features/workshop-access.md) owns establishment and access behavior; [Local Work Queue](./features/local-work-queue.md) owns inquiry creation, list, detail, and manual labour entry.

The draft modules persist incomplete inquiry and labour editor state with revisions and replay receipts. Their HTTP handlers are registered but use a disabled-by-default service setting; [Inquiry Drafts](./features/inquiry-drafts.md) owns their browser, API, and retention behavior.

The stored labour modules keep plan revisions, original times, review decisions, and a current approval pointer. An internal pricing service requires approved hours before making a labour-only snapshot. These services have no registered save, approve, or pricing HTTP route; the separate preview page uses invented data. [Approved Labour](./features/approved-labour.md) owns persistence and pricing behavior.

## Storage and Entry Points

`webapp.main:app` is the ASGI entry point; `create_app()` accepts a business database path and optional server settings. Its business database defaults from `CARBIT_DATABASE` or the repository default. Startup initializes separate auth and business SQLite databases: `AuthRepository.initialize()` applies any missing auth schema versions 1–4 (versions 3 and 4 add the recovery structures), while the business repository applies inquiry, labour, workshop, and draft migrations even when draft HTTP access is disabled. The auth recovery outbox rows record notification intent, not delivery; [Workshop Identity](./concepts/workshop-identity.md) owns the stored records.

Startup requires explicit auth configuration, including an auth database path, exact hosts and external origins, TOTP encryption keys, and CSRF signing keys. It rejects an auth database path that resolves to the business database. The route gate requires the configured HTTPS origin and guards session and write requests. Offline recovery also enters through `python -m webapp.admin_recovery_cli` for IT-admin recovery and `python -m webapp.user_recovery_cli` for business-user recovery. These consoles use the existing, current-schema auth SQLite store and its recovery services outside the HTTP route inventory; they do not initialize or migrate the store. [Employee Authentication](./features/employee-authentication.md) has the commands and operational behavior.

Draft HTTP access needs explicit opt-in and signing keys. Without opt-in, the handlers remain registered but cannot serve the draft service. [Inquiry Drafts](./features/inquiry-drafts.md) has the setting and route contract. Stored labour approval and approved pricing are internal service entry points; the queue's `/api/works/{work_id}/labour` route still records one manual labour snapshot. [Approved Labour](./features/approved-labour.md) and [Local Work Queue](./features/local-work-queue.md) describe those separate operations.

The `app/` functions are library entry points: callers supply interpreters, work and proposal inputs, and a human selection for quote finalization. `requirements-web.txt` is a compatibility entry point for a Windows AMD64, CPython 3.13 development lock; it does not define a universal deployment environment.

## Integration Boundary

Supplier search and AI interpretation or presentation depend on caller-selected adapters in the library workflows. Original labour records may carry Autodata provenance, but the current planner and web services do not query Autodata. A complete multi-work quote or send workflow is not exposed by these modules. A future quote write would need to recheck the current labour approval and inquiry version as part of its own transaction; [Approved Labour](./features/approved-labour.md) identifies the current read-time pricing boundary.
