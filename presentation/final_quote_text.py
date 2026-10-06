from domain.models.final_quote import FinalQuote


def format_final_quote(quote: FinalQuote) -> str:
    vehicle = quote.vehicle

    return (
        f"{vehicle.make} {vehicle.model} {vehicle.registration}\n\n"
        f"Varaosat: {quote.part_sale_price.sale_price:.2f} €\n"
        f"Työ: {quote.labour_cost.hours} h / "
        f"{quote.labour_cost.total_price:.2f} €\n\n"
        f"Yhteensä: {quote.total_price:.2f} € sis. ALV"
    )
