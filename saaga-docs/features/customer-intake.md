---
title: "Feature: Customer Intake"
type: feature
sources:
  - app/customer_intake_workflow.py
  - domain/models/customer_request.py
  - domain/models/work_item.py
  - domain/models/part_requirement.py
  - domain/rules/customer_request_work_item.py
  - integrations/ai/customer_request_interpreter.py
  - integrations/ai/fake_customer_request_interpreter.py
  - integrations/ai/openai_customer_request_client.py
  - integrations/ai/openai_customer_request_interpreter.py
---

# Feature: Customer Intake

## Overview

`process_customer_message()` turns an interpreter's structured reading of a customer message into an intake result. It creates a work item only when that reading is ready for automatic handling; requests requiring a person retain the interpretation without a work item.

## Key Concepts

Before working with this feature, understand these concepts:

- [Work and Part Data](../concepts/work-and-part-data.md) defines the interpretation vocabulary and work-item data contracts.

## Functional Specification

### Mechanism

1. A caller supplies the message and a `CustomerRequestInterpreter` to `process_customer_message()`; the workflow calls `interpreter.interpret(message)`.
2. If the interpretation has `HUMAN_REQUIRED` status, the workflow returns `CustomerIntakeResult` with that interpretation and `work_item=None`.
3. For `READY`, `build_work_item_from_customer_request()` converts each supported part type into a requirement and returns a `WorkItem`; the result carries both the interpretation and work item.
4. With the OpenAI implementation, `OpenAICustomerRequestClient.interpret_customer_request()` sends the message to `OpenAI().responses.create()`, parses `response.output_text` as JSON, and requires a JSON object. `OpenAICustomerRequestInterpreter.interpret()` then validates that object as `CustomerRequestInterpretation`.

The fake interpreter returns its configured interpretation for any message; the workflow therefore exercises the same status branch without an external call.

### Validation Rules

- `CustomerRequestInterpretation` forbids extra fields. `READY` requires an operation and at least one part type; `diagnosis_required=True` requires `HUMAN_REQUIRED` status.
- The interpretation model does not check part-type membership. The work-item rule accepts only `brake_pad` and creates one `SET` requirement for each occurrence, using the interpretation's axle and operation.
- The work-item rule rejects any status other than `READY`, a missing operation, and every unsupported part type with `ValueError`.
- The OpenAI prompt instructs the model to avoid diagnosing symptoms, inferring repairs from symptoms, and inventing registration, work, axle, or parts. It directs uncertain or diagnostic requests to `HUMAN_REQUIRED`; these instructions are not all enforced by model validators.

### Edge Cases

| Scenario | Behavior |
|----------|----------|
| `HUMAN_REQUIRED` with no operation or parts | Returns the interpretation with no work item; the conversion rule is not called. |
| `READY` with missing operation or empty part list | Pydantic rejects the interpretation before the workflow can convert it. |
| `READY` with an unsupported part type | Conversion raises `ValueError`; the workflow does not return a partial result. |
| OpenAI response is malformed JSON or a JSON non-object | JSON parsing fails or the client raises `ValueError`; no intake result is returned. |
| OpenAI response is an object with invalid fields or status combination | Pydantic validation fails; the workflow does not turn it into a work item. |
| Repeated `brake_pad` entries | The rule appends a separate one-`SET` requirement for each entry; it does not deduplicate them. |
| Unknown axle on a supported part | The rule copies the interpretation's axle into both the work item and requirement. |
| Interpreter or OpenAI call raises | The exception propagates; the workflow has no retry or fallback branch. |

## Technical Implementation

### Data Model

| Model/Type | Key Fields | Purpose |
|--------|------------|---------|
| `CustomerRequestInterpretation` | `status`, `diagnosis_required`, `operation`, `part_types` | Validated interpreter output; see [Work and Part Data](../concepts/work-and-part-data.md) for its full contract. |
| `CustomerIntakeResult` | `interpretation`, `work_item` | Workflow result; `work_item` is optional. |
| `WorkItem` | `operation`, `axle`, `part_requirements` | Ready request converted into work the later proposal flow can use. |

### Services/Functions

| Module | Function/Method | Purpose |
|---------|--------|---------|
| `app.customer_intake_workflow` | `process_customer_message()` | Calls the interpreter and branches on status. |
| `domain.rules.customer_request_work_item` | `build_work_item_from_customer_request()` | Converts a ready interpretation to supported requirements. |
| `CustomerRequestInterpreter` | `interpret()` | Abstract boundary for turning message text into a validated interpretation. |
| `FakeCustomerRequestInterpreter` | `interpret()` | Returns its configured interpretation for a deterministic caller or test. |
| `OpenAICustomerRequestInterpreter` | `interpret()` | Validates the dictionary supplied by its injected client. |
| `OpenAICustomerRequestClient` | `interpret_customer_request()` | Requests structured output from OpenAI and parses JSON. |
| `CustomerRequestClient` | `interpret_customer_request()` | Protocol used to inject the dictionary-producing client into the OpenAI interpreter. |

## Integration Points

- **Depends on**: [Work and Part Data](../concepts/work-and-part-data.md) contracts and the interpreter supplied by the caller.
- **Used by**: Callers needing a `WorkItem` for subsequent proposal decisions; see [Architecture](../ARCHITECTURE.md) for module placement.
- **External systems**: The OpenAI client calls the Responses API through the `openai` SDK. The fake interpreter needs no external service.

The client chooses its model through the constructor's `model` argument, which defaults to `gpt-6-luna`.

## Extension Guide

Implement `CustomerRequestInterpreter.interpret()` to return a `CustomerRequestInterpretation`; inject it into `process_customer_message()`. For another automatically supported part type, extend `PART_REQUIREMENT_UNITS` in the work-item rule and verify the resulting requirement unit. Keep requests needing diagnosis on the `HUMAN_REQUIRED` path, where no work item is built.

An OpenAI-compatible implementation can supply any `CustomerRequestClient` with `interpret_customer_request(message) -> dict`; the interpreter remains responsible for Pydantic validation.

Callers handling intake failures should catch client, JSON, validation, or conversion exceptions around `process_customer_message()`, since that function does not convert them to `HUMAN_REQUIRED`.
