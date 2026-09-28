from domain.models.supplier_candidate import SupplierCandidate


def sort_by_total_price(
    candidates: list[SupplierCandidate],
) -> list[SupplierCandidate]:
    return sorted(
        candidates,
        key=lambda candidate: candidate.total_price,
    )