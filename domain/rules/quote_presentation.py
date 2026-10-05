from domain.models.final_quote import FinalQuote
from domain.models.quote_presentation import QuotePresentationInput


def build_quote_presentation_input(
    quote: FinalQuote,
) -> QuotePresentationInput:
    return QuotePresentationInput(
        registration=quote.vehicle.registration,
        make=quote.vehicle.make,
        model=quote.vehicle.model,
        operation=quote.work_item.operation.value,
        axle=quote.work_item.axle.value,
        part_types=[
            requirement.canonical_product_type
            for requirement in quote.work_item.part_requirements
        ],
        labour_hours=quote.labour_cost.hours,
        labour_price=quote.labour_cost.total_price,
        parts_price=quote.part_sale_price.sale_price,
        total_price=quote.total_price,
        currency=quote.labour_cost.currency,
        prices_include_vat=True,
    )
