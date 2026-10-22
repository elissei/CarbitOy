"""Shared choice resolution and result construction for every adapter.

A choice resolves only by an exact identity match with a staff selection, or
when the source shows exactly one candidate. Several candidates without a
selection return the options; the adapter never picks one of them.
"""

from dataclasses import dataclass

from integrations.autodata.models import (
    REJECTED_TEXT_CAP, Choice, ChoiceIdentity, ChoiceLevel, LabourTimeLookupResult,
    LookupOutcome, LookupProvenance, ResolvedBy, ResolvedChoice,
)
from integrations.autodata.time_parser import parse_labour_time


AMBIGUOUS = {
    ChoiceLevel.VEHICLE: LookupOutcome.VEHICLE_AMBIGUOUS,
    ChoiceLevel.OPERATION: LookupOutcome.WORK_AMBIGUOUS,
    ChoiceLevel.VARIANT: LookupOutcome.VARIANT_AMBIGUOUS,
}
NOT_FOUND = {
    ChoiceLevel.VEHICLE: LookupOutcome.VEHICLE_NOT_FOUND,
    ChoiceLevel.OPERATION: LookupOutcome.WORK_NOT_FOUND,
    # A listed operation without any variant row has no displayed time.
    ChoiceLevel.VARIANT: LookupOutcome.TIME_MISSING,
}


@dataclass(frozen=True)
class Resolution:
    resolved: ResolvedChoice | None = None
    outcome: LookupOutcome | None = None
    candidates: tuple[Choice, ...] = ()


def resolve_choice(level: ChoiceLevel, candidates, selection: ChoiceIdentity | None) -> Resolution:
    candidates = tuple(candidates)
    identities = [candidate.identity for candidate in candidates]
    if len(identities) != len(set(identities)):
        return Resolution(outcome=LookupOutcome.SELECTION_NOT_UNIQUE, candidates=candidates)
    if selection is not None:
        for candidate in candidates:
            if candidate.identity == selection:
                return Resolution(resolved=ResolvedChoice(
                    choice=candidate, resolved_by=ResolvedBy.EXPLICIT_SELECTION,
                ))
        return Resolution(outcome=LookupOutcome.SELECTION_INVALID, candidates=candidates)
    if not candidates:
        return Resolution(outcome=NOT_FOUND[level])
    if len(candidates) == 1:
        return Resolution(resolved=ResolvedChoice(
            choice=candidates[0], resolved_by=ResolvedBy.ONLY_CANDIDATE,
        ))
    return Resolution(outcome=AMBIGUOUS[level], candidates=candidates)


class LookupBuilder:
    """Accumulates resolved choices so every result keeps its full context."""

    def __init__(self, provenance: LookupProvenance):
        self.provenance = provenance
        self.resolved: dict[str, ResolvedChoice] = {}

    def result(self, outcome: LookupOutcome, **fields) -> LabourTimeLookupResult:
        return LabourTimeLookupResult(
            outcome=outcome, provenance=self.provenance, **self.resolved, **fields,
        )

    def resolve(self, level: ChoiceLevel, candidates, selection) -> LabourTimeLookupResult | None:
        """Record the resolved choice, or return the result that stops the lookup."""
        resolution = resolve_choice(level, candidates, selection)
        if resolution.resolved is None:
            if resolution.outcome == LookupOutcome.TIME_MISSING:
                # No variant row exists; the resolved operation shows no time.
                return self.result(LookupOutcome.TIME_MISSING)
            return self.result(resolution.outcome, level=level, candidates=resolution.candidates)
        self.resolved[level.value.lower()] = resolution.resolved
        return None

    def time_result(self, displayed_time_text: str | None) -> LabourTimeLookupResult:
        """Interpret the resolved variant's displayed text; None means no time shown."""
        if displayed_time_text is None or not displayed_time_text.strip():
            return self.result(LookupOutcome.TIME_MISSING)
        parsed = parse_labour_time(displayed_time_text)
        if parsed.reading is None:
            return self.result(
                LookupOutcome.UNPARSEABLE,
                rejected_time_text=displayed_time_text[:REJECTED_TEXT_CAP],
                rejected_time_text_truncated=len(displayed_time_text) > REJECTED_TEXT_CAP,
                rejection_reason=parsed.rejection.value,
            )
        return self.result(LookupOutcome.FOUND, time=parsed.reading)
