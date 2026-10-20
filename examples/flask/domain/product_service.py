from abc import ABC, abstractmethod


class IProductService(ABC):
    """What the presentation layer needs. Only the contract, no code."""

    @abstractmethod
    def list_products(self) -> dict: ...
