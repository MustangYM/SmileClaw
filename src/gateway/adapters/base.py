from abc import ABC, abstractmethod

from gateway.models import InboundMessage


class ChannelAdapter(ABC):

    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def start(self):
        raise NotImplementedError

    @abstractmethod
    def stop(self):
        raise NotImplementedError

    @abstractmethod
    def poll_messages(self) -> list[InboundMessage]:
        raise NotImplementedError

    @abstractmethod
    def send_response(self, chat_id: str, text: str, thread_id: str | None = None, actions: list[str] | None = None) -> None:
        raise NotImplementedError
