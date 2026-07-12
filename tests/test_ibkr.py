"""IBKR provider unit tests — no gateway or network needed."""

import pytest

from conftest import clean_settings
from stock_trader.providers import get_provider
from stock_trader.providers.ibkr import IBKRProvider, assert_paper_accounts


def test_get_provider_ibkr_no_keys_needed():
    settings = clean_settings(provider="ibkr")
    assert settings.has_keys()  # auth lives in IB Gateway, not .env
    p = get_provider(settings)
    assert isinstance(p, IBKRProvider)
    assert p.mode_label == "PAPER"


def test_paper_account_guard():
    assert_paper_accounts(["DU1234567"])          # paper
    assert_paper_accounts(["DF1234567", "DU1"])   # paper variants
    with pytest.raises(RuntimeError, match="LIVE account"):
        assert_paper_accounts(["U9876543"])       # live
    with pytest.raises(RuntimeError, match="LIVE account"):
        assert_paper_accounts(["DU1", "U2"])      # mixed — still refuse
    with pytest.raises(RuntimeError):
        assert_paper_accounts([])                 # nothing — refuse


def test_market_clock_is_local_approximation():
    p = IBKRProvider(clean_settings(provider="ibkr"))
    # pure time-window logic, no connection required
    assert isinstance(p.market_is_open(), bool)
