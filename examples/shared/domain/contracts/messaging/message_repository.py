from abc import ABC, abstractmethod


class IMessageRepository(ABC):
    @abstractmethod
    def message_for(self, name: str) -> str:
        raise NotImplementedError
