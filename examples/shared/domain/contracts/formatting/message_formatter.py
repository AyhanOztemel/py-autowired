from abc import ABC, abstractmethod


class IMessageFormatter(ABC):
    @abstractmethod
    def format_message(self, name: str) -> str:
        raise NotImplementedError
