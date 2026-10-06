from typing import Protocol

from domain.models.customer_request import CustomerRequestInterpretation
from integrations.ai.customer_request_interpreter import (
    CustomerRequestInterpreter,
)


class CustomerRequestClient(Protocol):
    def interpret_customer_request(
        self,
        message: str,
    ) -> dict:
        ...


class OpenAICustomerRequestInterpreter(
    CustomerRequestInterpreter
):
    def __init__(
        self,
        client: CustomerRequestClient,
    ) -> None:
        self.client = client

    def interpret(
        self,
        message: str,
    ) -> CustomerRequestInterpretation:
        data = self.client.interpret_customer_request(
            message
        )

        return CustomerRequestInterpretation.model_validate(
            data
        )
