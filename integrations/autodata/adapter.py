from abc import ABC, abstractmethod

from integrations.autodata.models import LabourTimeLookupResult, LabourTimeRequest


class AutodataLabourAdapter(ABC):
    """Looks up one labour time; it never logs in, orders or approves anything.

    Each call is stateless and replays the staff selections in the request.
    Failures are returned as lookup outcomes, not raised, so callers can store
    every attempt. Hours are present only for an unambiguous FOUND result.
    """

    @abstractmethod
    def lookup(self, request: LabourTimeRequest) -> LabourTimeLookupResult:
        raise NotImplementedError
