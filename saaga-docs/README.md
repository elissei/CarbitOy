---
title: carbitoy-docs Documentation
type: index
---

# carbitoy-docs Documentation

Generated navigation for this corpus. Saaga rewrites this file from the
INDEX files on every documentation run — edit the documents it links to, not
this page.

Read in order: the architecture, then the core concepts, then the workflows.

## Architecture

- [Architecture](./ARCHITECTURE.md)

## Core Concepts

The concepts the rest of the corpus links to most often. Everything else assumes them.

- [Work and Part Data](./concepts/work-and-part-data.md) — Legacy single-work, part, supplier, proposal, selection, quote, and presentation contracts, plus intake interpretation.
- [Work Plans](./concepts/work-plans.md) — Domain work and labour plan contracts, original time coverage, and the distinct application snapshot and revision identifiers.
- [Workshop Identity](./concepts/workshop-identity.md) — Employee identity, persistent authentication and recovery state, session and role contracts, workshop membership, trusted context, and event attribution.

## Workflows and Features

What the system does, end to end, in index order.

- [Proposal Decisions](./features/proposal-decisions.md) — Builds and orders supplier candidates, assembles a single-work proposal from supplied labour time, renders it, and records a human choice.
- [Customer Intake](./features/customer-intake.md) — Interprets messages, routes requests needing a person, and composes ready work items into demo proposals.
- [Final Quote and Presentation](./features/final-quote.md) — Calculates and validates a final quote, formats Finnish price text, and optionally produces a work description.
- [Local Work Queue](./features/local-work-queue.md) — Lists workshop-scoped inquiries, records session-attributed manual Autodata time, and shows inclusive labour prices and versioned history.
- [Labour Planning](./features/labour-planning.md) — Selects full-scope original times, reports coverage issues, and requires explicit current human review before approved hours are available.
- [Employee Authentication](./features/employee-authentication.md) — Signs employees in with password and MFA, enforces session and CSRF gates, supports logout, and describes trusted offline MFA recovery.
- [Workshop Access](./features/workshop-access.md) — Establishes and selects explicit employee workshop membership and scopes protected inquiry operations.
- [Inquiry Drafts](./features/inquiry-drafts.md) — Manually saves and reopens workshop inquiry drafts with revision checks, replay receipts, and bounded retention.
- [Approved Labour](./features/approved-labour.md) — Stores versioned original times and human decisions, gates approved hours, and prices current labour approval.

## Indexes

- [Concept Index](./concepts/INDEX.md)
- [Pattern Index](./patterns/INDEX.md)
- [Feature Index](./features/INDEX.md)
- [Glossary](./GLOSSARY.md) — every indexed term, with the definition its index gives it
