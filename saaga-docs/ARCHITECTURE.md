---
title: Architecture
type: architecture
last_verified: 2026-10-06
sources:
  - app/
  - domain/models/
  - domain/ontology/
  - domain/rules/
  - integrations/ai/
  - integrations/suppliers/
  - presentation/
---

# Architecture

## Overall Architecture

CarbitOy is a system under development for automating an automotive workshop's workflow from work order intake through supplier comparison and human-approved parts selection to a customer quotation.

The current Python implementation contains reusable domain contracts and rules, adapter boundaries, application workflows, and text presentation functions.

The `app/` layer connects customer-message intake to single-requirement proposal creation through
`app/demo_intake_workflow.py`; callers supply the interpreter, vehicle, labour time, products, offers, and proposal details.
It also exposes a separate finalization call after a person selects a candidate. [Customer Intake](./features/customer-intake.md)
owns the intake branch and composition, [Proposal Decisions](./features/proposal-decisions.md) owns candidate and labour rules,
and [Final Quote and Presentation](./features/final-quote.md) owns quote assembly and output. These are library functions.

### Dependency direction

```text
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

These arrows describe imports in the current modules. Domain rules use models and ontology lookup; they do not import either
integration package. Supplier adapters use supplier-offer models. AI adapters use the intake or quote-presentation models and
the OpenAI SDK where an OpenAI client is selected. Presentation uses domain data and rules; its quote workflow also depends
on the presenter interface in `integrations/ai/`.

Callers choose implementations at the boundaries. The intake workflow receives a `CustomerRequestInterpreter`, while quote
presentation receives a `QuotePresenter`. Their fake implementations can exercise those calls without a network request;
OpenAI implementations are available for external generation. The [feature index](./features/INDEX.md) links to the resulting
behavior and failure paths.

## Modules

### Domain models — `domain/models/`

Pydantic models carry vehicle and work data, catalog and supplier facts, labour, proposals, human choices, final quotes, and
presentation data. They also validate relationships such as proposal labour hours and final quote identity, currency,
purchase price, and total. The models use Python standard-library values and Pydantic; they do not perform supplier searches
or external generation. [Work and Part Data](./concepts/work-and-part-data.md) owns the fields and validation contracts.

### Domain ontology — `domain/ontology/`

YAML vocabulary and a parser map part and axle terms to canonical work data and answer whether a part type requires an axle.
The parser uses PyYAML and domain models. Its vocabulary and access functions are covered by
[Work and Part Data](./concepts/work-and-part-data.md); proposal rules use the ontology when evaluating part compatibility.

### Domain rules — `domain/rules/`

Rules convert a supported intake interpretation to a work item, evaluate products and offers, calculate labour and part sale
price, record a human selection, assemble a final quote, and project quote facts for presentation. They depend on domain
models, and matching and coverage rules consult the ontology. [Customer Intake](./features/customer-intake.md) owns intake
conversion; [Proposal Decisions](./features/proposal-decisions.md) owns candidate and selection decisions;
[Final Quote and Presentation](./features/final-quote.md) owns quote calculations and projection.

### Supplier integrations — `integrations/suppliers/`

An abstract supplier adapter defines offer search, a collector combines offers from supplied adapters, and a fake adapter
returns stored offers. These modules depend on the domain supplier-offer model. The repository does not provide a live
supplier client here; [Supplier Adapters](./patterns/supplier-adapters.md) owns the interface and collection pattern.

### AI integrations — `integrations/ai/`

Interpreter and presenter interfaces separate application calls from their implementations. Fake implementations return
supplied values; OpenAI clients call the Responses API for structured intake data or a Finnish work description. The adapters
depend on the relevant domain models, and the live clients depend on the `openai` SDK. See
[Customer Intake](./features/customer-intake.md) for interpretation and
[Final Quote and Presentation](./features/final-quote.md) for description generation and the data sent to that client.

### Application workflows — `app/`

The application exposes intake, demo proposal, and final quote helpers. `app/demo_intake_workflow.py` calls intake and, when
it yields a work item, the proposal helper; its separate finalization function delegates to the quote helper. Callers supply
the interpreter, proposal inputs, and human selection. These modules import domain models and rules; intake uses the
interpreter interface. See [Customer Intake](./features/customer-intake.md) and
[Final Quote and Presentation](./features/final-quote.md) for their workflow contracts.

### Presentation — `presentation/`

Formatters render raw proposal text and deterministic Finnish final-quote price text from domain data. A separate quote
workflow builds presentation input through a domain rule and delegates the work description to an injected AI presenter. Thus
presentation imports domain models and rules, and that workflow imports the presenter interface.
[Proposal Decisions](./features/proposal-decisions.md) owns raw proposal output;
[Final Quote and Presentation](./features/final-quote.md) owns final quote text and description delegation.

## Entry-Point Scope

The public application functions are library entry points. Callers provide the message and interpreter, proposal inputs,
then a selected candidate and selection metadata for the separate quote finalization call. No command-line entry point is
present. Supplier search, intake interpretation, and quote description generation depend on caller-chosen adapters.
