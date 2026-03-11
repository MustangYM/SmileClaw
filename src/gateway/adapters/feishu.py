from gateway.adapters.base import ChannelAdapter
from gateway.models import InboundMessage


class FeishuAdapter(ChannelAdapter):

    def __init__(self, channel_name: str, config: dict):
        self.channel_name = channel_name
        self.config = config
        self._running = False

    def name(self) -> str:
        return self.channel_name

    def start(self):
        self._running = True

    def stop(self):
        self._running = False

    def poll_messages(self) -> list[InboundMessage]:
        return []

    def send_response(self, chat_id: str, text: str, thread_id: str | None = None, actions: list[str] | None = None) -> None:
        _ = (chat_id, text, thread_id, actions)
        return None
