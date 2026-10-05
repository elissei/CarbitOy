from abc import ABC, abstractmethod

from domain.models.quote_presentation import QuotePresentationInput
from domain.models.quote_presentation_output import QuotePresentationOutput


class QuotePresenter(ABC):
    @abstractmethod
    def present(
        self,
        presentation_input: QuotePresentationInput,
    ) -> QuotePresentationOutput:
        raise NotImplementedError
