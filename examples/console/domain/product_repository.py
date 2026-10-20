from abc import ABC, abstractmethod


class IProductRepository(ABC):
    """What the application needs from storage. Only the contract, no code."""

    @abstractmethod
    def list_products(self) -> list[str]: ...
