---
title: Architecture
type: architecture
last_verified: 2026-10-05
sources:
  - domain/models/
  - domain/ontology/
  - domain/rules/
  - integrations/suppliers/
  - presentation/
---

# Architecture

## Overall Architecture

CarbitOy is a Python component library for turning vehicle work and part requirements into supplier candidates and proposal data. The repository has no application entry point or CLI. Callers compose the components; [tests](../tests/) show the currently exercised compositions.

`domain/models` defines the data exchanged across the system. `domain/ontology` interprets part terminology, and `domain/rules` makes matching, coverage, pricing, and selection decisions. `integrations/suppliers` gathers supplier offers through adapters. `presentation` renders a raw proposal as text. The dependency direction is toward the domain: integrations and presentation use domain models, while domain rules also use ontology data. The [visualizations](../docs/visualizations/README.md) distinguish a target architecture from dated source snapshots.

## Modules

### Domain models — `domain/models/`

Defines [validated data contracts](./concepts/work-and-part-data.md) for vehicles, work, parts, supplier products and offers, labour, proposals, human selection, and final quotes. Other source modules depend on these models; this module depends on Pydantic and Python standard library types.

### Domain ontology — `domain/ontology/`

Maps free text to canonical part requirements and supplies [part type and axle terminology](./concepts/work-and-part-data.md) from YAML data. It depends on domain models and PyYAML.

### Domain rules — `domain/rules/`

Provides [decisions](./features/proposal-decisions.md) for product compatibility, package coverage, supplier candidate construction and ordering, labour cost, unit conversion, and human selection. It depends on domain models and the ontology; the coverage result also uses Pydantic.

### Supplier integrations — `integrations/suppliers/`

Defines the supplier search adapter boundary, gathers offers from adapters, and includes an in-memory fake adapter. It depends on domain supplier-offer models.

### Presentation — `presentation/`

Formats a raw proposal as [human-readable text](./features/proposal-decisions.md). It depends on domain proposal and related models.
