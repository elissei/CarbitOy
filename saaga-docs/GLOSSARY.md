---
title: Glossary
type: index
---

# Glossary

Every term the INDEX files name, with the one-line definition its index gives
it. Saaga regenerates this file on every documentation run and copies each
definition verbatim — to change one, change the INDEX row it comes from.

- [Approved Labour](./features/approved-labour.md) — Stores versioned original times and human decisions, gates approved hours, and prices current labour approval.
- [Customer Intake](./features/customer-intake.md) — Interprets messages, routes requests needing a person, and composes ready work items into demo proposals.
- [Employee Authentication](./features/employee-authentication.md) — Signs employees in with password and MFA, enforces session and CSRF gates, supports logout, and describes trusted recovery services and local consoles.
- [Final Quote and Presentation](./features/final-quote.md) — Calculates and validates a final quote, formats Finnish price text, and optionally produces a work description.
- [Inquiry Drafts](./features/inquiry-drafts.md) — Manually saves, reopens, and discards shared workshop inquiry drafts with revision checks, retry receipts, actor names, and bounded retention.
- [Labour Planning](./features/labour-planning.md) — Selects full-scope original times, reports coverage issues, and requires explicit current human review before approved hours are available.
- [LabourTimeEntry](./concepts/work-plans.md) — Domain work and labour plan contracts, original time coverage, and the distinct application snapshot and revision identifiers.
- [Local Work Queue](./features/local-work-queue.md) — Lists workshop-scoped inquiries, records session-attributed manual Autodata time, and shows inclusive labour prices and versioned history.
- [plan revision](./concepts/work-plans.md) — Domain work and labour plan contracts, original time coverage, and the distinct application snapshot and revision identifiers.
- [PlannedWork](./concepts/work-plans.md) — Domain work and labour plan contracts, original time coverage, and the distinct application snapshot and revision identifiers.
- [Proposal Decisions](./features/proposal-decisions.md) — Builds and orders supplier candidates, assembles a single-work proposal from supplied labour time, renders it, and records a human choice.
- [Supplier Adapters](./patterns/supplier-adapters.md) — Implement supplier searches and collect offers through the shared adapter contract.
- [Work and Part Data](./concepts/work-and-part-data.md) — Legacy single-work, part, supplier, proposal, selection, quote, and presentation contracts, plus intake interpretation.
- [Work Plans](./concepts/work-plans.md) — Domain work and labour plan contracts, original time coverage, and the distinct application snapshot and revision identifiers.
- [WorkPlanSnapshot](./concepts/work-plans.md) — Domain work and labour plan contracts, original time coverage, and the distinct application snapshot and revision identifiers.
- [Workshop Access](./features/workshop-access.md) — Establishes and selects explicit employee workshop membership and scopes protected inquiry operations.
- [Workshop Identity](./concepts/workshop-identity.md) — Employee identity, persistent authentication and recovery state, session and role contracts, workshop membership, trusted context, and event attribution.
