"""Test helpers: Settings that ignore whatever is in the developer's .env."""

from stock_trader.config import Settings


def clean_settings(**overrides) -> Settings:
    base = dict(
        provider="alpaca",
        api_key="", secret_key="",
        bitget_api_key="", bitget_secret_key="", bitget_passphrase="",
        telegram_bot_token="", telegram_chat_id="",
        live_trading=False,
    )
    base.update(overrides)
    return Settings(**base)
