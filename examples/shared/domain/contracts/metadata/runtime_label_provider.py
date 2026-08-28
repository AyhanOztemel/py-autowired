from abc import ABC, abstractmethod


class IRuntimeLabelProvider(ABC):
    @abstractmethod
    def label(self) -> str:
        raise NotImplementedError
