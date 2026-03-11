import time
from pathlib import Path

from gateway.adapters.discord import DiscordAdapter
from gateway.adapters.feishu import FeishuAdapter
from gateway.adapters.telegram import TelegramAdapter
from gateway.config import load_gateway_config
from gateway.service import GatewayService
from gateway.storage import GatewayStore


def build_service(config_path: str | Path) -> GatewayService:
    config = load_gateway_config(config_path)
    root = Path(config_path).resolve().parents[1]
    store = GatewayStore(root / ".smileclaw" / "gateway.db")
    return GatewayService(config=config, store=store, workspace_root=root)


def build_adapters(config_path: str | Path):
    config = load_gateway_config(config_path)
    adapters = []

    raw_cfg = {}
    import yaml  # type: ignore
    raw_cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))

    for name, channel in config.channels.items():
        if not channel.enabled:
            continue
        raw_channel = (raw_cfg.get("channels", {}) or {}).get(name, {}) or {}
        adapter_cfg = dict(raw_channel)
        adapter_cfg["enabled"] = channel.enabled
        adapter_cfg["bot_token"] = channel.bot_token
        adapter_cfg["dm_policy"] = channel.dm_policy

        if name == "telegram":
            adapters.append(TelegramAdapter(name, adapter_cfg))
        elif name == "discord":
            adapters.append(DiscordAdapter(name, adapter_cfg))
        elif name == "feishu":
            adapters.append(FeishuAdapter(name, adapter_cfg))

    return adapters


def run_gateway(config_path: str | Path):
    service = build_service(config_path)
    adapters = build_adapters(config_path)

    for adapter in adapters:
        adapter.start()

    try:
        while True:
            for adapter in adapters:
                messages = adapter.poll_messages()
                for msg in messages:
                    response = service.handle_inbound(msg)
                    if response.error_code == "DUPLICATE_EVENT":
                        continue
                    actions = None
                    if response.error_code == "APPROVAL_PENDING":
                        actions = ["✅ 同意一次", "🟢 当前文件总是允许", "❌ 拒绝"]
                    adapter.send_response(
                        chat_id=msg.chat_id,
                        thread_id=msg.thread_id,
                        text=response.message,
                        actions=actions,
                    )
            time.sleep(1)
    finally:
        for adapter in adapters:
            adapter.stop()
