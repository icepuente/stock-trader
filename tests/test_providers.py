"""Provider selection and key-requirement logic — no network needed."""

import pytest

from conftest import clean_settings
from stock_trader.providers import get_provider
from stock_trader.providers.alpaca import AlpacaProvider
from stock_trader.providers.bitget import BitgetProvider


def test_get_provider_alpaca():
    p = get_provider(clean_settings(provider="alpaca", api_key="k", secret_key="s"))
    assert isinstance(p, AlpacaProvider)
    assert p.mode_label == "PAPER"
    # paper endpoint is hardcoded on the trading client
    assert "paper" in str(p.trading._base_url).lower()


def test_get_provider_bitget_uses_sandbox():
    p = get_provider(clean_settings(
        provider="bitget",
        bitget_api_key="k", bitget_secret_key="s", bitget_passphrase="p",
    ))
    assert isinstance(p, BitgetProvider)
    assert p.mode_label == "DEMO"
    assert p.market_is_open() is True
    assert p.clock_info()["next_close"] == "24/7"
    # ccxt sandbox mode (Bitget demo trading) must be engaged
    assert p.x.options.get("sandboxMode") is True


def test_get_provider_unknown():
    with pytest.raises(ValueError, match="unknown provider"):
        get_provider(clean_settings(provider="kraken"))


def test_has_keys_is_provider_aware():
    assert not clean_settings(provider="alpaca").has_keys()
    assert clean_settings(provider="alpaca", api_key="k", secret_key="s").has_keys()
    assert not clean_settings(provider="bitget", api_key="k", secret_key="s").has_keys()
    assert clean_settings(
        provider="bitget",
        bitget_api_key="k", bitget_secret_key="s", bitget_passphrase="p",
    ).has_keys()


def test_require_keys_message_names_provider():
    with pytest.raises(SystemExit, match="BITGET"):
        clean_settings(provider="bitget").require_keys()
    with pytest.raises(SystemExit, match="ALPACA"):
        clean_settings(provider="alpaca").require_keys()
