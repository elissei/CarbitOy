---
title: Architecture
type: architecture
last_verified: 2026-10-09
sources:
  - app/
  - domain/models/
  - domain/ontology/
  - domain/rules/
  - integrations/ai/
  - integrations/suppliers/
  - presentation/
  - services/
  - webapp/
  - requirements-web.txt
---

# Architecture

## Overall Architecture

CarbitOy is a system under development for automating an automotive workshop's workflow from work order intake through supplier comparison and human-approved parts selection to a customer quotation.

The Python implementation contains reusable domain contracts and rules, adapter boundaries, application workflows,
and text presentation functions. A separate FastAPI application serves a browser work queue and stores its inquiries
in a local SQLite database. Its entry point does not call the existing library intake, proposal, or quote workflows.

The `app/` layer connects customer-message intake to proposal creation and a separate quote finalization call.
Callers supply interpreters, proposal inputs, and the selected candidate. [Customer Intake](./features/customer-intake.md)
owns intake, [Proposal Decisions](./features/proposal-decisions.md) owns candidate and labour rules, and
[Final Quote and Presentation](./features/final-quote.md) owns quote assembly and output.

### Dependency direction

```text
webapp/ ─────────────> services/ ─────────────> domain/models/
   │
   ├──────────────────────────────────────────> domain/models/
   └──> webapp/database.py ───────────────────> SQLite file

app/ ────────────────> domain/models/, domain/rules/
  │                            │             │
  └──> integrations/ai/        │             └──> domain/ontology/
               │               │
               └──────────────>│

presentation/ ───────> domain/models/, domain/rules/
       │
       └──────────────> integrations/ai/

integrations/suppliers/ ──────> domain/models/
domain/ontology/ ─────────────> domain/models/
```

The Python arrows describe imports in the current modules; the database arrow is a storage boundary. The web app
imports its repository, shared labour models, and application-facing pricing functions. Its static browser assets
call its JSON routes through the same server. `services/` imports the shared labour models, not the library labour rule.

Domain rules use models and ontology lookup; they do not import either
integration package. Supplier adapters use supplier-offer models. AI adapters use the intake or quote-presentation models and
the OpenAI SDK where an OpenAI client is selected. Presentation uses domain data and rules; its quote workflow also depends
on the presenter interface in `integrations/ai/`.

## Modules

### Domain models — `domain/models/`

Pydantic models carry vehicle and work data, supplier facts, labour, proposals, selections, final quotes, and presentation data.
They validate relationships including proposal labour hours and final quote identity, currency, purchase price, and total.
[Work and Part Data](./concepts/work-and-part-data.md) owns their fields and validation contracts.

### Domain ontology — `domain/ontology/`

YAML vocabulary and a parser map part and axle terms to canonical work data. The parser uses PyYAML and domain models;
[Work and Part Data](./concepts/work-and-part-data.md) covers its access functions and part compatibility vocabulary.

### Domain rules — `domain/rules/`

Rules convert intake to work data, evaluate products and offers, calculate prices, record a selection, assemble a quote, and
project facts for presentation. They depend on domain models; matching and coverage rules consult the ontology.
[Customer Intake](./features/customer-intake.md) owns intake conversion; [Proposal Decisions](./features/proposal-decisions.md) owns candidate and selection decisions;
[Final Quote and Presentation](./features/final-quote.md) owns quote calculations and projection.

### Supplier integrations — `integrations/suppliers/`

An abstract supplier adapter defines offer search; a collector and fake adapter combine or return offers. These modules
depend on the supplier-offer model. [Supplier Adapters](./patterns/supplier-adapters.md) owns the interface and collection pattern.

### AI integrations — `integrations/ai/`

Interpreter and presenter interfaces separate application calls from implementations. Fakes return supplied values;
OpenAI clients call the Responses API for structured intake data or a Finnish work description. They depend on domain
models, and live clients use the `openai` SDK. See [Customer Intake](./features/customer-intake.md) for interpretation and
[Final Quote and Presentation](./features/final-quote.md) for description generation and the data sent to that client.

### Application workflows — `app/`

The application exposes intake, demo proposal, and final quote helpers. `app/demo_intake_workflow.py` calls intake and, when
it yields a work item, the proposal helper; its separate finalization function delegates to the quote helper. Callers supply
the interpreter, proposal inputs, and human selection. These modules import domain models and rules; intake uses the
interpreter interface. See [Customer Intake](./features/customer-intake.md) and
[Final Quote and Presentation](./features/final-quote.md) for their workflow contracts.

### Presentation — `presentation/`

Formatters render raw proposal and Finnish final-quote price text. A quote workflow builds presentation input through a
domain rule and delegates the work description to an injected AI presenter. Presentation imports models and rules; its
workflow also imports the presenter interface. [Proposal Decisions](./features/proposal-decisions.md) owns raw proposal output;
[Final Quote and Presentation](./features/final-quote.md) owns final quote text and description delegation.

### Application services — `services/`

This layer prices `LabourTime` into a shared `LabourCost` and splits an inclusive total into net and VAT for the web workflow.
It imports shared models; the library proposal rule is separate. [Local Work Queue](./features/local-work-queue.md) owns the
web pricing behavior, while [Proposal Decisions](./features/proposal-decisions.md) owns the library calculation.

### Web application — `webapp/`

FastAPI serves the browser assets and JSON routes and validates requests. Its repository initializes and migrates SQLite
storage for inquiries, events, and creation retries. The browser calls those routes to create, inspect, and update inquiries.
The web app imports `services/`, shared labour models, and its own database module, but no `app/` workflow. See
[Local Work Queue](./features/local-work-queue.md) for the HTTP surface, stored data, and user flow.

## Entry-Point Scope

The `app/` functions remain library entry points. Callers provide the message and interpreter, proposal inputs,
then a selected candidate and selection metadata for the separate quote finalization call. Supplier search, intake
interpretation, and quote description generation depend on caller-chosen adapters.

`webapp.main:app` is an ASGI entry point for the local browser and HTTP work queue. Its `create_app()` factory accepts
a database path; otherwise it reads `CARBIT_DATABASE` or uses the repository's `var/carbit.db`. The server initializes
storage at startup. Web dependencies, including FastAPI and Uvicorn, are listed in `requirements-web.txt`. The web
routes and storage behavior belong to [Local Work Queue](./features/local-work-queue.md). No command-line entry point is present.
