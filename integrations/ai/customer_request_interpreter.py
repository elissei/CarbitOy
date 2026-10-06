from abc import ABC, abstractmethod

from domain.models.customer_request import CustomerRequestInterpretation


class CustomerRequestInterpreter(ABC):
    @abstractmethod
    def interpret(
        self,
        message: str,
    ) -> CustomerRequestInterpretation:
        raise NotImplementedError
