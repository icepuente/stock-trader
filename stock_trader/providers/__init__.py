from ..config import Settings
from .base import Provider


def get_provider(settings: Settings) -> Provider:
    if settings.provider == "alpaca":
        from .alpaca import AlpacaProvider

        return AlpacaProvider(settings)
    if settings.provider == "bitget":
        from .bitget import BitgetProvider

        return BitgetProvider(settings)
    if settings.provider == "ibkr":
        from .ibkr import IBKRProvider

        return IBKRProvider(settings)
    if settings.provider == "sim":
        from .sim import SimProvider

        return SimProvider(settings)
    raise ValueError(
        f"unknown provider {settings.provider!r} — use 'alpaca', 'bitget', 'ibkr' or 'sim'"
    )
