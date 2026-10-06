from domain.models.customer_request import CustomerRequestInterpretation
from integrations.ai.customer_request_interpreter import (
    CustomerRequestInterpreter,
)


class FakeCustomerRequestInterpreter(
    CustomerRequestInterpreter
):
    def __init__(
        self,
        interpretation: CustomerRequestInterpretation,
    ) -> None:
        self.interpretation = interpretation

    def interpret(
        self,
        message: str,
    ) -> CustomerRequestInterpretation:
        return self.interpretation
