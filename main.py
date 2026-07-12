"""Entry point for the paper-trading bot.

Usage:
  python main.py serve          # web dashboard at http://127.0.0.1:8000
  python main.py run            # run the session loop headless (paper trading)
  python main.py step           # single evaluation pass, then exit
  python main.py scan           # print today's high-relative-volume watchlist
  python main.py bars SYMBOL    # print recent hourly candles + indicators
"""

import argparse
import logging

from stock_trader.bot import Bot
from stock_trader.config import Settings
from stock_trader.indicators import add_indicators
from stock_trader.providers import get_provider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Tiered MACD/volume paper-trading bot")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve_p = sub.add_parser("serve")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8000)
    sub.add_parser("run")
    sub.add_parser("step")
    sub.add_parser("scan")
    bars_p = sub.add_parser("bars")
    bars_p.add_argument("symbol")
    args = parser.parse_args()

    settings = Settings()

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("stock_trader.server:app", host=args.host, port=args.port)
    elif args.cmd == "run":
        from stock_trader.telegram import HeadlessRunner, maybe_start_bridge

        bot = Bot(settings)
        bridge = maybe_start_bridge(settings, HeadlessRunner(bot))
        if bridge:
            bot.on_event = bridge.send_event
        try:
            bot.run()
        finally:
            if bridge:
                bridge.stop()
    elif args.cmd == "step":
        Bot(settings).run(once=True)
    elif args.cmd == "scan":
        settings.require_keys()
        print(get_provider(settings).scan())
    elif args.cmd == "bars":
        settings.require_keys()
        bars = get_provider(settings).hourly_bars(args.symbol.upper())
        if bars.empty:
            print("no data")
        else:
            print(add_indicators(bars, settings.vmar_period).tail(15).to_string())


if __name__ == "__main__":
    main()
