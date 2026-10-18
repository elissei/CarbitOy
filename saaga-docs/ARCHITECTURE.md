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

## Documentation scope and implementation status — 2026-10-09

This corpus describes separate source snapshots: domain stage 1 in
[feature/autodata-labour, commit 6fb2e993](https://github.com/elissei/CarbitOy/tree/6fb2e9930e20235661aece6856c396d8db15a3a4)
and the existing local web queue in feature/web-app, commit 5be66f9.
The domain snapshot is documented from Git without merging its Python files
into visuals. Local source paths below refer to their named source snapshot;
the visuals checkout is not a combined implementation of both branches.

| Status | Capability |
| --- | --- |
| Implemented in 6fb2e993 | Side vocabulary; WorkItem.side defaulting to UNKNOWN; SHOCK_ABSORBER_REPLACEMENT; explicit front shock absorber pair configuration. |
| Already implemented in the domain snapshot | Single-work build_raw_proposal service with caller-supplied LabourTime, inclusive pricing, empty default candidates and an aware UTC creation time. |
| Planned, not implemented | WorkPlan/LabourPlan, Autodata pair/combined-time selection, multi-operation time aggregation and complete multi-work quotation. |
| Later, not implemented | LabourTimeAdjustment, authorized approval, reasons and timestamps, retention of original times and adjustment history. |

The recorded domain verification result is **158 passing tests** at 6fb2e993.
This is the source branch's test result, not a test run of the visuals checkout.

The pair configuration is descriptive and queried explicitly. It does not
change work scope or parts quantities, require a pair, call Autodata or
calculate time. Only front shock absorbers are configured; other part types
and axles remain unspecified. The ontology PAIR unit describes quantities
and packaging, not a work-time selection policy.

The unchanged raw-proposal text formatter still labels only brake replacement.
The new shock absorber operation is a domain capability, not a completed
intake, rendering or web flow. See [Work and Part Data](./concepts/work-and-part-data.md)
and [Proposal Decisions](./features/proposal-decisions.md).

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

### Workshop configuration — `domain/config/`

In domain commit 6fb2e993, pair_replacement.py contains a tuple of frozen
PairReplacementRule records and get_pair_replacement_rule(). Exact canonical
part type and axle lookup returns a rule or None. None means unconfirmed,
not forbidden. This module imports only the domain Axle vocabulary.
No existing application or web workflow automatically consumes the rule.

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
In domain commit 6fb2e993, `services/proposal_builder.py` also provides `build_raw_proposal()`,
which uses that pricing service and the unchanged RawProposal model. It is separate from the frozen
demo's older library labour rule. [Local Work Queue](./features/local-work-queue.md) owns the
web workflow; [Proposal Decisions](./features/proposal-decisions.md) describes the two proposal entry points.

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

## Planned labour architecture — not implemented

LabourPlan will select non-overlapping groups of original Autodata time records.
For a same-operation pair, an available pair time replaces individual side times;
a missing pair time leaves the plan awaiting human review. Distinct operations
are summed by default, with no automatic overlap deduction. An exact Autodata
combined-work time may replace the times of the operations it covers, retaining
its provenance. Each operation must be counted exactly once.

The proposed pricing policy keeps full Decimal hours and rounds one overall
VAT-inclusive labour charge half up to cents at 90.00 EUR/h, then uses the existing
inclusive VAT split at 25.5%. This aggregation is a plan, not behavior of the
current single-time builder. Later LabourTimeAdjustment records will replace
effective time without overwriting original Autodata records. Neither planned
structure exists in the documented implementation.

The separate web queue currently stores a single manual Autodata snapshot and
versioned events. That history is not a LabourTimeAdjustment approval mechanism;
the local web queue has no authentication. A later integration must add approval
authorization and structured provenance while preserving version checks and
atomic data/event writes. See [decision log](../docs/notes/paatokset.md)
and the [status-separated diagram](../docs/visualizations/domain-vaihe-1.md).
