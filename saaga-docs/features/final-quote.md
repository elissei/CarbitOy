---
title: "Feature: Final Quote and Presentation"
type: feature
sources:
  - app/demo_workflow.py
  - domain/models/final_quote.py
  - domain/models/human_selection.py
  - domain/models/part_sale_price.py
  - domain/models/quote_presentation.py
  - domain/models/quote_presentation_output.py
  - domain/models/raw_proposal.py
  - domain/rules/final_quote.py
  - domain/rules/part_sale_price.py
  - domain/rules/quote_presentation.py
  - integrations/ai/quote_presenter.py
  - integrations/ai/fake_quote_presenter.py
  - integrations/ai/openai_quote_presenter.py
  - integrations/ai/openai_client.py
  - presentation/final_quote_text.py
  - presentation/quote_workflow.py
---

# Feature: Final Quote and Presentation

## Overview

Given a proposal and a person's selected part candidate, callers can create a priced final quote. They can render fixed Finnish price text and, separately, ask a presenter for a short work description.

## Key Concepts

Before working with this feature, understand these concepts:
- [Work and Part Data](../concepts/work-and-part-data.md) defines the proposal, selection, quote, and presentation contracts.
- [Proposal Decisions](./proposal-decisions.md) covers how candidates are built and a person selects one.

## Functional Specification

### Mechanism

1. Obtain a `RawProposal` and `HumanSelection`. The [Proposal Decisions](./proposal-decisions.md) feature owns candidate preparation and selection.
2. Call `create_final_quote(quote_id, proposal, selection, created_at)`. It calculates a part sale price from the selection's **total** purchase price, then adds that sale price to the proposal's labour total.
3. The `FinalQuote` model validates the resulting proposal identity, purchase price, currency agreement, and sum. It retains the proposal's vehicle, work item, labour cost, and the given selection and timestamp.
4. Call `format_final_quote(quote)` to produce deterministic Finnish text with the vehicle, parts price, labour hours and price, and VAT-inclusive total.
5. Optionally call `present_final_quote(quote, presenter)`. It builds presentation input from the quote, delegates to `presenter.present()`, and returns `QuotePresentationOutput` containing only `work_description`.

`present_final_quote()` does not call `format_final_quote()` or combine the work description with the price text. A caller that needs both outputs must request and compose them separately.

The demo workflow shows the concrete composition:

1. `create_demo_proposal()` takes a vehicle, work item, labour time, products, and offers. It prices labour, builds candidates for the work item's sole part requirement, and creates the proposal.
2. The caller chooses one proposal candidate and supplies `selected_by` and `selected_at` to `create_demo_final_quote()`.
3. That helper converts the candidate into a selection, then calls `create_final_quote()`. It does not format or present the resulting quote.

For example, a selected purchase total of `44.90` becomes a part sale price of `58.37`; with `72.00` labour, the quote total is `130.37`.

These numbers use the selected `total_price`, even when the selected quantity exceeds one.
The sale-price rule never multiplies the selected `unit_price` directly.

### Validation Rules

- `calculate_part_sale_price()` rejects a selection whose `total_price` is zero or negative. It multiplies that total by `Decimal("1.30")` and rounds to `Decimal("0.01")` with `ROUND_HALF_UP`.
- `PartSalePrice` requires positive purchase and sale prices. Its currency is copied from the selection.
- `FinalQuote` requires `part_selection.proposal_id` to equal `proposal_id` and `part_sale_price.purchase_price` to equal `part_selection.total_price`.
- Labour cost, selection, and part sale price currencies must all agree. `total_price` must equal `labour_cost.total_price + part_sale_price.sale_price`.
- The quote model verifies arithmetic but does not recalculate the sale price from the 1.30 multiplier. That calculation belongs to `create_final_quote()`.

`build_quote_presentation_input()` copies the vehicle, work, labour, and price facts from the quote and sets `prices_include_vat` to `True`. Part types come from each work-item requirement, not from the selected product.

### Edge Cases

| Scenario | Behavior |
|----------|----------|
| Demo work item has zero or multiple part requirements | `create_demo_proposal()` raises `ValueError`; it accepts exactly one. |
| Selection has no positive total purchase price | `calculate_part_sale_price()` raises `ValueError`; no quote is created. |
| Quote identity, purchase price, currency, or sum is inconsistent | Pydantic validation rejects `FinalQuote`. |
| Presenter returns an empty string | `FakeQuotePresenter` and `OpenAIQuotePresenter` can package it; `QuotePresentationOutput` does not prohibit empty text. |
| OpenAI response text is blank after trimming | `OpenAIQuotePresentationClient` raises `ValueError`. |
| OpenAI request fails | The exception propagates; this code defines no retry or fallback. |

## Technical Implementation

### Data Model

The field contracts live in [Work and Part Data](../concepts/work-and-part-data.md); these models play distinct roles in this flow:

| Model/Type | Key Fields | Purpose |
|--------|------------|---------|
| `RawProposal` | `labour_cost`, `work_item` | Supplies labour and work to quote assembly. |
| `HumanSelection` | `proposal_id`, `total_price`, `currency` | Supplies the chosen part's purchase total and identity. |
| `PartSalePrice` | `purchase_price`, `sale_price`, `currency` | Holds the calculated part price for the customer. |
| `FinalQuote` | `part_selection`, `part_sale_price`, `total_price` | Holds and validates the final amount. |
| `QuotePresentationInput` | `operation`, `axle`, `part_types` | Carries quote facts to a presenter; it also contains vehicle and price fields. |
| `QuotePresentationOutput` | `work_description` | Returns only the presenter-produced description. |

### Services/Functions

| Module | Function/Method | Purpose |
|---------|--------|---------|
| `domain.rules.part_sale_price` | `calculate_part_sale_price()` | Marks up the selected purchase total and rounds it. |
| `domain.rules.final_quote` | `create_final_quote()` | Assembles and validates the quote. |
| `domain.rules.quote_presentation` | `build_quote_presentation_input()` | Projects quote facts into presenter input and sets `prices_include_vat=True`. |
| `presentation.final_quote_text` | `format_final_quote()` | Produces fixed Finnish quote text. |
| `presentation.quote_workflow` | `present_final_quote()` | Delegates description creation to a `QuotePresenter`. |
| `app.demo_workflow` | `create_demo_proposal()` | Composes labour and candidate rules for one requirement. |
| `app.demo_workflow` | `create_demo_final_quote()` | Selects a candidate from that proposal and creates a quote. |
| `integrations.ai.quote_presenter.QuotePresenter` | `present()` | Defines the presentation interface. |
| `integrations.ai.fake_quote_presenter.FakeQuotePresenter` | `present()` | Returns a supplied description for local or test use. |
| `integrations.ai.openai_quote_presenter.OpenAIQuotePresenter` | `present()` | Wraps a client's description in output data. |
| `integrations.ai.openai_client.OpenAIQuotePresentationClient` | `create_work_description()` | Requests Finnish description text from OpenAI. |

### API Calls (if applicable)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| OpenAI Responses API | `responses.create()` | Generate a Finnish work description from the selected work facts. |

The OpenAI client creates `OpenAI()` and defaults to model `gpt-6-luna` unless its constructor receives another model. Its request JSON includes only `operation`, `axle`, and `part_types`: no vehicle identity, labour, or prices are sent. Instructions ask for a short Finnish description using only supplied facts; the response is stripped and checked for emptiness. The code does not otherwise verify that generated wording follows those instructions.

The deterministic formatter uses the quote's part **sale** price, labour hours and total, and overall total. It emits `€` and `sis. ALV` literally and formats monetary amounts to two decimal places; it does not read the quote's currency field for display.

Its output separates the vehicle line, parts and labour lines, and total line with blank lines. Presentation input carries the price fields, but the OpenAI client deliberately selects only work facts for its request.

## Integration Points

- **Depends on**: [Proposal Decisions](./proposal-decisions.md) for a chosen candidate, [Work and Part Data](../concepts/work-and-part-data.md) for contracts, and the [Architecture](../ARCHITECTURE.md) module map.
- **Used by**: Callers composing quote creation and presentation; `app.demo_workflow` is the in-repository composition example.
- **External systems**: OpenAI is used only when `OpenAIQuotePresentationClient` is supplied through `OpenAIQuotePresenter`; the fake presenter needs no external service.

## Extension Guide

For another quote source, construct a proposal and a valid selection, then call `create_final_quote()`. The demo helper is intentionally limited to one part requirement; broader proposal composition needs its own handling before calling the quote rule. Keep monetary calculation in the domain rules, and use `format_final_quote()` for the fixed price wording. Implement `QuotePresenter.present()` for another description provider, and pass it to `present_final_quote()`; compose the returned description with the formatted quote in caller code when both are needed.
