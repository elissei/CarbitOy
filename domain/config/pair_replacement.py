"""Workshop pair replacement practices, independent of labour time selection.

Only front shock absorbers have a confirmed practice in the current project
discussion. Other part types and axles require workshop approval before adding
entries. No entry means an unspecified practice, not a prohibition on pairs.

These rules describe left and right replacement on the same axle. They do not
change WorkItem, infer part quantities, enforce replacement, or select Autodata
times. PAIR as a supplier packaging unit is a separate concept.
"""

from dataclasses import dataclass

from domain.models.enums import Axle


@dataclass(frozen=True)
class PairReplacementRule:
    """A confirmed same-axle pair practice for a canonical part type."""

    canonical_product_type: str
    axle: Axle


PAIR_REPLACEMENT_RULES: tuple[PairReplacementRule, ...] = (
    PairReplacementRule(
        canonical_product_type="shock_absorber",
        axle=Axle.FRONT,
    ),
)


def get_pair_replacement_rule(
    canonical_product_type: str,
    axle: Axle,
) -> PairReplacementRule | None:
    """Return an explicitly configured rule, or None when not confirmed."""
    for rule in PAIR_REPLACEMENT_RULES:
        if rule.canonical_product_type == canonical_product_type and rule.axle == axle:
            return rule
    return None
