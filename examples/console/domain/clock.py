from abc import ABC, abstractmethod


class IClock(ABC):
    """Tells the time. Only the contract, no code."""

    @abstractmethod
    def now(self) -> str: ...
